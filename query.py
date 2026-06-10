"""
core loop of AutoRUN

对应 src/query.ts — 实现主流式循环:
1. 发送消息到 AI 提供商 API
2. 接收包含 tool_use 块的流式响应
3. 执行工具并返回结果
4. 处理压缩、token 预算、回退和停止钩子

产生流式事件，包括带有原因说明的最终 'terminal' 事件。
"""

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Tuple, Union

from AutoRUN_v1.api.client import get_client
from AutoRUN_v1.messages.types import (
    AssistantMessage,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_user_interruption_message,
    create_assistant_api_error_message,
    create_system_message,
    create_user_message,
)
from AutoRUN_v1.messages.utils import (
    normalize_messages_for_api,
    normalize_content_from_api,
    get_messages_after_compact_boundary,
    prepend_user_context,
)
from AutoRUN_v1.services.conversations import save_conversation as _auto_save_conversation

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

def _auto_save(app_state=None) -> None:
    """保存当前对话状态。"""
    if app_state is None:
        from AutoRUN_v1.state.app_state import get_app_state
        app_state = get_app_state()
    if app_state.messages:
        _auto_save_conversation(app_state)


# ── Main query function ─────────────────────────────────────────────────────

async def run_query(
    messages: List[Message],
    system_prompt: Union[str, List[str]],
    user_context: Dict[str, str],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    can_use_tool: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    state: Optional[Any] = None,
    full_context: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """运行主查询循环。

    Args:
        messages: 对话的初始消息。
        system_prompt: 完整系统提示词（字符串或列表）。
        user_context: 用户上下文字典（日期、AUTORUN.md 等）。
        tools: 工具定义列表（带有可选的 'call_fn'）。
        model: 要使用的模型 ID。
        can_use_tool: 可选的可调用对象 (tool_name, args) -> bool。
        full_context: 完整的上下文文本（含 system_context、skills），
            将作为首条用户消息注入 API 调用，但不存入对话历史。

    Yields:
        带有 type 字段的字典:
        - 'stream_request_start': 新的 API 请求
        - 'assistant': 助手消息（内容 + usage）
        - 'user': 用户消息（工具结果）
        - 'error': API/执行错误
        - 'attachment': 附件/信号消息
        - 'terminal': 带有 'reason' 键的最终事件。之后不再有事件。
    """
    client = get_client()
    if isinstance(system_prompt, list):
        system_prompt = "\n".join(system_prompt or [])
    elif system_prompt is None:
        system_prompt = ""

    turn_count = 0

    while True:
        turn_count += 1

        # Yield request start signal
        yield {"type": "stream_request_start"}

        # Get messages after compact boundary
        active_messages = get_messages_after_compact_boundary(messages)
        api_messages = normalize_messages_for_api(active_messages)

        # Prepend context on first turn only (full context from QueryEngine OR user_context fallback)
        if turn_count == 1 and full_context:
            api_messages_with_context = [
                {"role": "user", "content": full_context}
            ] + api_messages
        elif turn_count == 1:
            api_messages_with_context = prepend_user_context(api_messages, user_context)
        else:
            api_messages_with_context = api_messages

        # ── Streaming API call ──────────────────────────────────────────
        assistant_content: List[Any] = []
        tool_use_blocks: List[ToolUseBlock] = []
        current_text = ""
        current_tool_use: Optional[Dict[str, Any]] = None
        current_input_json = ""
        current_thinking = ""
        current_thinking_signature = ""
        usage: Dict[str, Any] = {}
        stop_reason: Optional[str] = None
        error_occurred = False
        error_message = ""

        def _flush_text():
            nonlocal current_text
            if current_text:
                assistant_content.append(TextBlock(text=current_text))
                current_text = ""

        def _flush_thinking():
            nonlocal current_thinking, current_thinking_signature
            if current_thinking:
                assistant_content.append(ThinkingBlock(
                    thinking=current_thinking,
                    signature=current_thinking_signature,
                ))
                current_thinking = ""
                current_thinking_signature = ""

        try:
            async for event in client.stream_message(
                messages=api_messages_with_context,
                system_prompt=system_prompt,
                tools=tools,
                model=model,
            ):
                event_type = event.get("type", "")

                if event_type == "text_delta":
                    _flush_thinking()
                    current_text += event.get("text", "")
                    # Strip XML tool calls from streaming text before sending to UI
                    clean_text = current_text
                    if "<tool_calls" in clean_text or "<tool_call" in clean_text:
                        import re
                        clean_text = re.sub(r'<tool_calls?\b[^>]*>.*?</tool_calls?\b[^>]*>', '', clean_text, flags=re.DOTALL)
                    yield {
                        "type": "assistant",
                        "uuid": "",
                        "content": [{"type": "text", "text": clean_text}],
                        "is_partial": True,
                    }

                elif event_type == "tool_use_start":
                    _flush_thinking()
                    if current_text:
                        assistant_content.append(TextBlock(text=current_text))
                        current_text = ""
                    current_tool_use = event.get("content_block", {})
                    current_input_json = ""

                elif event_type == "input_json_delta":
                    current_input_json += event.get("partial_json", "")

                elif event_type == "content_block_stop":
                    if current_tool_use:
                        # Complete the tool_use block
                        try:
                            tool_input = json.loads(current_input_json) if current_input_json else {}
                        except json.JSONDecodeError:
                            tool_input = {}

                        tool_block = ToolUseBlock(
                            id=current_tool_use.get("id", ""),
                            name=current_tool_use.get("name", ""),
                            input=tool_input,
                        )
                        tool_use_blocks.append(tool_block)
                        assistant_content.append(tool_block)
                        current_tool_use = None
                        current_input_json = ""
                    # Flush thinking block if any
                    _flush_thinking()

                elif event_type == "text_block_start":
                    _flush_thinking()
                    # New text block starting
                    pass

                elif event_type == "message_delta":
                    stop_reason = event.get("stop_reason")
                    usage = event.get("usage", {})

                elif event_type == "message_stop":
                    pass

                elif event_type == "thinking_delta":
                    current_thinking += event.get("thinking", "")

                elif event_type == "thinking_start":
                    _flush_text()
                    current_thinking = ""

                elif event_type == "signature_delta":
                    current_thinking_signature += event.get("signature", "")

                elif event_type == "retry_attempt":
                    # API 重试中 — 透传给 UI 显示但不作为错误
                    yield event

                elif event_type == "error":
                    error_occurred = True
                    error_message = event.get("error", "Unknown error")
                    yield event

        except Exception as e:
            error_occurred = True
            error_message = str(e)
            yield {"type": "error", "error": error_message}

        if error_occurred:
            yield {
                "type": "assistant",
                "uuid": "",
                "is_api_error_message": True,
                "content": [{"type": "text", "text": error_message}],
            }
            _auto_save(state)
            yield {"type": "terminal", "reason": "model_error"}
            return

        # Flush any remaining thinking + text to assistant_content
        _flush_thinking()
        if current_text:
            assistant_content.append(TextBlock(text=current_text))

        # Yield the complete assistant message (non-partial)
        yield {
            "type": "assistant",
            "uuid": "",
            "content": [
                block.to_dict() if hasattr(block, 'to_dict') else block
                for block in assistant_content
            ],
            "stop_reason": stop_reason,
            "usage": usage,
        }

        # If no structured tool_use blocks, scan text for XML <tool_calls>
        # (DeepSeek outputs tool calls as XML embedded in text)
        xml_warnings: List[str] = []
        if not tool_use_blocks:
            from AutoRUN_v1.utils.xml_tool_parser import parse_and_strip_xml
            xml_blocks, xml_warnings = parse_and_strip_xml(assistant_content)
            if xml_blocks:
                tool_use_blocks = xml_blocks
                # parse_and_strip_xml already cleaned text blocks in-place
                # (XML tags removed). Preserve text blocks and append
                # tool_use blocks so the complete event carries both.
                assistant_content.extend(xml_blocks)

        # Debug: log extraction result
        import os as _os2
        try:
            with open(_os2.path.expanduser("~/.autorun_debug_query.log"), "a", encoding="utf-8") as _f:
                _f.write(f"tool_use_blocks count: {len(tool_use_blocks)}\n")
                for tb in tool_use_blocks:
                    _f.write(f"  - {tb.name}: {json.dumps(tb.input, ensure_ascii=False)[:200]}\n")
                for w in xml_warnings:
                    _f.write(f"  WARNING: {w}\n")
        except Exception:
            logger.debug("Failed to write tool_use debug log", exc_info=True)

        # If no tool_use blocks, report warnings and exit
        if not tool_use_blocks:
            if xml_warnings:
                # XML tool call tags were detected but parsing failed — report to user
                warning_text = (
                    "模型返回了 XML 格式的工具调用，但解析失败。"
                    "这通常是模型输出格式不标准导致的。\n\n"
                    "解析警告:\n" + "\n".join(f"  - {w}" for w in xml_warnings)
                )
                yield {
                    "type": "error",
                    "error": warning_text,
                    "is_tool_parse_error": True,
                }
            _auto_save(state)
            yield {"type": "terminal", "reason": "completed"}
            return

        # ── Execute tools ───────────────────────────────────────────────
        tool_result_blocks: List[ToolResultBlock] = []
        for tool_block in tool_use_blocks:
            can_execute = True
            if can_use_tool:
                result = can_use_tool(tool_block.name, tool_block.input)
                if asyncio.iscoroutine(result):
                    can_execute = await result
                else:
                    can_execute = result

            if not can_execute:
                result = ToolResultBlock(
                    tool_use_id=tool_block.id,
                    content="Tool use denied by permission policy.",
                    is_error=True,
                )
            else:
                result = await _execute_tool(tool_block, tools or [], state=state)

            tool_result_blocks.append(result)

        # Yield single user event with ALL tool results (DeepSeek requires
        # all tool_results in one user message immediately after assistant)
        from uuid import uuid4 as _uuid4
        yield {
            "type": "user",
            "uuid": str(_uuid4()),
            "content": [r.to_dict() for r in tool_result_blocks],
        }

        # Build single user message with all tool_results
        tool_results: List[Message] = [
            UserMessage(
                content=tool_result_blocks,
                tool_use_result="\n".join(
                    str(r.content) for r in tool_result_blocks
                ),
            )
        ]

        # Append to messages for next turn
        messages = messages + [
            AssistantMessage(content=assistant_content),
        ] + tool_results



async def _execute_tool(tool_block: ToolUseBlock,
                         tool_definitions: List[Dict[str, Any]],
                         state: Optional[Any] = None) -> ToolResultBlock:
    """执行单个工具并返回其结果块。"""
    tool_name = tool_block.name
    tool_input = tool_block.input

    # Find matching tool definition
    tool_def = None
    for t in tool_definitions:
        if t.get("name") == tool_name:
            tool_def = t
            break

    if tool_def is None:
        return ToolResultBlock(
            tool_use_id=tool_block.id,
            content="Unknown tool: {0}".format(tool_name),
            is_error=True,
        )

    try:
        # 如果可用，通过 call_fn 执行
        if "call_fn" in tool_def:
            from AutoRUN_v1.tools.base import ToolContext, ToolResult as ToolResultCls
            context = ToolContext(cwd=os.getcwd(), state=state)
            result = await tool_def["call_fn"](tool_input, context)
            if isinstance(result, ToolResultCls):
                return ToolResultBlock(
                    tool_use_id=tool_block.id,
                    content=str(result.data),
                    is_error=result.is_error,
                )
            elif isinstance(result, str):
                return ToolResultBlock(
                    tool_use_id=tool_block.id,
                    content=result,
                )
            else:
                return ToolResultBlock(
                    tool_use_id=tool_block.id,
                    content=json.dumps(result),
                )
        else:
            return ToolResultBlock(
                tool_use_id=tool_block.id,
                content="Tool '{0}' has no call_fn configured.".format(tool_name),
                is_error=True,
            )
    except Exception as e:
        return ToolResultBlock(
            tool_use_id=tool_block.id,
            content="Tool execution error: {0}".format(e),
            is_error=True,
        )


async def run_simple_query(prompt: str,
                            system_prompt: Union[str, List[str]],
                            user_context: Dict[str, str],
                            tools: Optional[List[Dict[str, Any]]] = None,
                            model: Optional[str] = None) -> str:
    """运行简单查询并返回最终的文本响应。

    为简单用例提供的 run_query 便捷封装。
    """
    user_msg = create_user_message(prompt)
    full_response_parts: List[str] = []

    async for event in run_query(
        messages=[user_msg],
        system_prompt=system_prompt,
        user_context=user_context,
        tools=tools,
        model=model,
    ):
        if event.get("type") == "terminal":
            break
        elif event.get("type") == "assistant" and not event.get("is_partial"):
            content = event.get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    full_response_parts.append(block.get("text", ""))
        elif event.get("type") == "error":
            full_response_parts.append("[Error: {0}]".format(event.get("error")))

    return "".join(full_response_parts)
