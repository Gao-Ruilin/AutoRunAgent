"""
AgentTool — Sub-agent execution for complex multi-step tasks.

Mirrors src/tools/AgentTool/ — spawns a sub-agent with access to
all tools to perform autonomous work and return results.

Agent types are dynamically loaded from the agent registry
(~/.autorun/agents/ and ./.autorun/agents/). No agent types are hardcoded.
"""

import asyncio
import json
import os
import uuid
from typing import Any, Dict, List, Optional

from AutoRUN_v1.api.client import get_client
from AutoRUN_v1.context import build_context_text, build_env_info, get_system_context, get_user_context
from AutoRUN_v1.messages.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from AutoRUN_v1.messages.utils import normalize_messages_for_api
from AutoRUN_v1.prompts.system_prompt import get_default_agent_prompt, get_system_prompt
from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult
from AutoRUN_v1.utils.config import get_model




# ── Background agent task registry ──────────────────────────────────────
# session_id → list of {task, description, agent_type, model_override, context, prompt}
_background_tasks: Dict[str, List[Dict[str, Any]]] = {}
# session_id → list of completed result strings
_background_results: Dict[str, List[str]] = {}
# session_id → {description: result_str} — 按描述索引的结果，用于 TaskOutput 精准查找
_background_results_by_desc: Dict[str, Dict[str, str]] = {}
# session_id → callback function called when agent completes
_on_agent_done_callbacks: Dict[str, Any] = {}

# ── Auto-trigger callback registry ─────────────────────────────────────
# session_id → list of async callable(session_id, description, result_str)
# Multiple callbacks per session (e.g. server.py registers both auto-trigger and output forwarder)
_on_agent_done_callbacks: Dict[str, List[Any]] = {}


def register_agent_done_callback(session_id: str, callback):
    """注册 Agent 完成回调。支持多个回调共存。"""
    if session_id not in _on_agent_done_callbacks:
        _on_agent_done_callbacks[session_id] = []
    _on_agent_done_callbacks[session_id].append(callback)


def unregister_agent_done_callbacks(session_id: str):
    """移除指定 session 的所有回调。"""
    _on_agent_done_callbacks.pop(session_id, None)


# ── Agent stream callback registry ───────────────────────────────────────
# session_id → list of callable(session_id, description, event_dict)
# event_dict keys: event_type (text_delta/tool_use/tool_result/thinking_delta),
#                   text, tool_name, tool_input, tool_result, is_error
_stream_callbacks: Dict[str, List[Any]] = {}


def register_stream_callback(session_id: str, callback):
    """注册子 Agent 流式事件回调。"""
    if session_id not in _stream_callbacks:
        _stream_callbacks[session_id] = []
    _stream_callbacks[session_id].append(callback)


def unregister_stream_callbacks(session_id: str):
    """移除指定 session 的流式回调。"""
    _stream_callbacks.pop(session_id, None)


def _notify_stream(session_id: str, description: str, event: Dict[str, Any]):
    """通知所有流式回调。非阻塞，异常被记录到日志。"""
    _logger = logging.getLogger(__name__)
    cbs = _stream_callbacks.get(session_id, []) + _stream_callbacks.get("default", [])
    for cb in cbs:
        try:
            cb(session_id, description, event)
        except Exception:
            _logger.warning(
                "Stream callback failed for session %s", session_id, exc_info=True
            )


def _store_background_task(session_id: Optional[str], description: str, task: asyncio.Task,
                           agent_type: str = "", model_override: Optional[str] = None,
                           context: Any = None, original_prompt: str = ""):
    """Register a background agent task and set up result collection with follow-up support."""
    if not session_id:
        session_id = "default"
    if session_id not in _background_tasks:
        _background_tasks[session_id] = []
    entry = {
        "task": task,
        "description": description,
        "agent_type": agent_type,
        "model_override": model_override,
        "context": context,
        "prompt": original_prompt,
    }
    _background_tasks[session_id].append(entry)

    # ── 超时提醒：5分钟后若 Agent 仍在运行，通知门控Agent ──
    AGENT_REMINDER_DELAY = 300  # 5 分钟
    async def _reminder():
        await asyncio.sleep(AGENT_REMINDER_DELAY)
        if not task.done() and not task.cancelled():
            # 检查 Agent 是否仍在运行中
            for sess in (session_id, "default"):
                entries = _background_tasks.get(sess, [])
                for e in entries:
                    if e["description"] == description:
                        # Agent 仍在运行 — 通知门控Agent
                        reminder_msg = (
                            f"[Agent 提醒] '{description}' 已运行超过 {AGENT_REMINDER_DELAY // 60} 分钟，仍在执行中。"
                        )
                        # 将提醒存入结果，TaskOutput 可获取
                        if sess not in _background_results:
                            _background_results[sess] = []
                        _background_results[sess].append(reminder_msg)
                        if sess not in _background_results_by_desc:
                            _background_results_by_desc[sess] = {}
                        _background_results_by_desc[sess][description] = reminder_msg
                        # 通知回调
                        cbs = _on_agent_done_callbacks.get(sess, [])
                        import logging as _lg
                        for cb in cbs:
                            try:
                                cb(sess, description, reminder_msg)
                            except Exception:
                                _lg.getLogger(__name__).debug(
                                    "Reminder callback failed for %s", sess, exc_info=True
                                )
                        break

    asyncio.ensure_future(_reminder())

    def _on_done(t: asyncio.Task):
        from AutoRUN_v1.tools.send_message import drain_pending_messages
        succeeded = False
        result = None
        result_str = ""
        try:
            result = t.result()
            succeeded = True
        except asyncio.CancelledError:
            result_str = f"[Agent 已取消: {description}]"
        except Exception as e:
            import traceback as _tb
            result_str = f"[Agent 错误: {description}]\n{e}\n\nTraceback:\n{_tb.format_exc()}"
        finally:
            # Always remove task from active list
            if session_id in _background_tasks:
                _background_tasks[session_id] = [e for e in _background_tasks[session_id] if e["task"] is not t]
            if session_id in _background_tasks and not _background_tasks[session_id]:
                del _background_tasks[session_id]

        # Always store result (even cancelled/error) so gatekeeper can see it
        if session_id not in _background_results:
            _background_results[session_id] = []
        if session_id not in _background_results_by_desc:
            _background_results_by_desc[session_id] = {}
        if not result_str:
            if result:
                result_str = f"[Agent 结果: {description}]\n{result}"
            else:
                result_str = f"[Agent 结果: {description}]\n(Agent 返回了空结果)"
        _background_results[session_id].append(result_str)
        _background_results_by_desc[session_id][description] = result_str

        # ── Check for pending SendMessage follow-ups ──
        follow_up = None
        try:
            follow_up = drain_pending_messages(session_id, description)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "drain_pending_messages failed for session %s, desc=%s", session_id, description, exc_info=True
            )
        if follow_up and result:
            try:
                combined = (
                    f"你之前的输出:\n---\n{result[:2000]}\n---\n\n"
                    f"用户/门控Agent 发来了新的指示。请根据此调整你的工作:\n\n{follow_up}"
                )
                tool = AgentTool()
                fu_task = asyncio.get_running_loop().create_task(
                    tool._run_agent(agent_type, description + " (跟进)",
                                    combined, model_override, context)
                )
                _store_background_task(session_id, description + " (跟进)", fu_task,
                                       agent_type, model_override, context, combined)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Follow-up agent creation failed for session %s, desc=%s",
                    session_id, description, exc_info=True
                )

        # ── Auto-trigger: notify ALL registered callbacks ──
        for sid in (session_id, "default"):
            cbs = _on_agent_done_callbacks.get(sid, [])
            for cb in cbs:
                if cb and result_str:
                    try:
                        cb(sid, description, result_str)
                    except Exception:
                        import logging, traceback as _cb_tb
                        logging.getLogger(__name__).warning(
                            "Agent done callback failed for session %s, desc=%s: %s",
                            sid, description, _cb_tb.format_exc()
                        )

    task.add_done_callback(_on_done)


def drain_background_results(session_id: str) -> Optional[str]:
    """Collect and clear completed background agent results for a session.
    Returns combined result text, or None if nothing ready."""
    results = _background_results.pop(session_id, [])
    _background_results_by_desc.pop(session_id, None)  # 同步清理
    # Also check fallback "default" session
    fallback = _background_results.pop("default", [])
    _background_results_by_desc.pop("default", None)
    results.extend(fallback)
    if not results:
        return None
    return "\n\n---\n".join(results)


def drain_background_result_by_desc(session_id: str, description: str) -> Optional[str]:
    """按 description 查找并取回特定 Agent 的结果（不清理其他结果）。"""
    # 尝试指定 session
    by_desc = _background_results_by_desc.get(session_id, {})
    result = by_desc.pop(description, None)
    if result:
        # 也从主列表移除
        lst = _background_results.get(session_id, [])
        if lst and result in lst:
            lst.remove(result)
        return result
    # fallback to "default"
    by_desc = _background_results_by_desc.get("default", {})
    result = by_desc.pop(description, None)
    if result:
        lst = _background_results.get("default", [])
        if lst and result in lst:
            lst.remove(result)
        return result
    return None


def _get_available_agent_types() -> Dict[str, str]:
    """从 agent_registry 动态加载 Agent 类型。

    始终包含一个 fallback 'general-purpose'。
    """
    types = {
        "general-purpose": "通用代理，用于研究复杂问题、搜索代码和执行多步骤任务。",
    }
    try:
        from AutoRUN_v1.services.agent_registry import discover_agents
        agents = discover_agents()
        for name, agent_def in agents.items():
            types[name] = agent_def.get("description", "")
    except Exception:
        pass
    return types


class AgentTool(Tool):
    """启动一个子代理来自主处理复杂的多步骤任务。

    代理类型从 ~/.autorun/agents/ 和 ./.autorun/agents/ 动态加载。
    子代理可使用全部工具，无工具限制。
    """

    @property
    def name(self) -> str:
        return "Agent"

    @property
    def description(self) -> str:
        agent_types = _get_available_agent_types()
        type_lines = "\n".join(f"- {name}: {desc}" for name, desc in sorted(agent_types.items()))

        return f"""启动一个新的代理来自主处理复杂的多步骤任务。

Agent 工具启动专门的代理（子进程），自动处理复杂任务。每种代理类型有特定的能力和系统提示词。
子代理可使用全部工具，无工具限制。

可用的代理类型:
{type_lines}

使用 Agent 工具时，指定 subagent_type 参数来选择代理类型。如果不指定或类型未找到，使用 general-purpose。

何时不使用 Agent 工具:
- 如果你想读取特定文件路径，请改用 Read 工具或 Glob 工具
- 如果你在搜索特定的类定义，请改用 Glob 工具
- 如果你在特定文件或 2-3 个文件内搜索代码，请改用 Read 工具
- 与上述代理描述无关的其他任务

用法说明:
- 始终包含一个简短描述（3-5 个词）概括代理将要做什么
- 尽可能同时启动多个代理以最大化性能
- 代理的输出通常应该被信任
- 清楚地告诉代理你期望它编写代码还是只做研究
- 如果代理描述中提到应主动使用它，那么你应该尽量在用户要求之前就使用它"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        agent_types = _get_available_agent_types()

        schema: Dict[str, Any] = {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "The type of specialized agent to use for this task. Choose from the available agent types listed in the tool description.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this agent. If not specified, uses the agent's configured model or the default.",
                },
            },
            "required": ["description", "prompt"],
        }

        # 动态设置 subagent_type 的 enum 提示（可选，不强制）
        agent_names = list(agent_types.keys())
        if agent_names:
            schema["properties"]["subagent_type"]["enum"] = agent_names

        return schema

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        description = args.get("description", "Agent task")
        prompt = args.get("prompt", "")
        agent_type = args.get("subagent_type", "general-purpose")
        model_override = args.get("model")

        if not prompt:
            return ToolResult(data="Error: prompt is required", is_error=True)

        # 所有 Agent 始终后台运行 — 门控Agent 永不阻塞
        import asyncio
        session_id = getattr(context.state, 'session_id', None) if context.state else None
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run_agent(agent_type, description, prompt, model_override, context)
        )
        _store_background_task(session_id, description, task,
                               agent_type=agent_type,
                               model_override=model_override,
                               context=context,
                               original_prompt=prompt)
        return ToolResult(
            data=f"Agent '{description}' 已启动 (后台运行)。使用 TaskOutput(task_id='{description}') 获取结果。",
            is_error=False,
        )

    async def _run_agent(self, agent_type: str, description: str, prompt: str,
                         model_override: Optional[str], context: ToolContext) -> str:
        """Execute the agent with a full multi-turn tool loop."""
        client = get_client()

        # 确定模型：优先使用 override，其次使用 Agent 模板配置，最后使用默认
        model = model_override or get_model()
        agent_system_prompt = None

        # 尝试从注册中心获取 Agent 模板
        try:
            from AutoRUN_v1.services.agent_registry import get_agent
            agent_def = get_agent(agent_type)
            if agent_def:
                agent_system_prompt = agent_def.get("system_prompt", "")
                if not model_override and agent_def.get("model"):
                    model = agent_def["model"]
        except Exception:
            pass

        # 使用 Agent 模板的 system_prompt，否则使用默认的
        if agent_system_prompt:
            agent_system = agent_system_prompt
        else:
            agent_system = get_default_agent_prompt()

        env_info = build_env_info(model)
        full_system_prompt = agent_system + "\n" + env_info

        # 构建初始消息
        user_context = await get_user_context()
        context_text = build_context_text(user_context)
        full_input = f"{context_text}\n\n任务描述: {description}\n\n任务:\n{prompt}"

        messages = [{"role": "user", "content": full_input}]
        all_responses: List[str] = []

        # 获取全部工具定义（子Agent无工具限制）
        from AutoRUN_v1.tools import get_tools
        all_tool_defs = get_tools()
        agent_tools = [
            {"name": t["name"], "description": t.get("description", ""),
             "input_schema": t.get("input_schema", {}), "call_fn": t.get("call_fn")}
            for t in all_tool_defs
        ]

        # 获取 session_id 用于流式通知
        session_id = getattr(context.state, 'session_id', None) if context.state else None

        turn_count = 0
        last_error = None
        try:
         while True:
            turn_count += 1

            # ── 流式 API 调用 ──
            assistant_text = ""
            reasoning_content = ""
            tool_use_blocks: List[Dict[str, Any]] = []
            current_tool_use: Optional[Dict[str, Any]] = None
            current_input_json = ""

            api_succeeded = False
            last_api_error = None
            max_api_retries = 2  # initial attempt + 1 retry

            for api_attempt in range(max_api_retries):
                try:
                    async for event in client.stream_message(
                        messages=messages,
                        system_prompt=full_system_prompt,
                        model=model,
                        tools=agent_tools,
                    ):
                        event_type = event.get("type", "")

                        if event_type == "text_delta":
                            text = event.get("text", "")
                            assistant_text += text
                            if session_id and text:
                                _notify_stream(session_id, description, {
                                    "event_type": "text_delta",
                                    "text": text,
                                })

                        elif event_type == "tool_use_start":
                            if current_tool_use is None:
                                current_tool_use = event.get("content_block", {})
                                current_input_json = ""

                        elif event_type == "input_json_delta":
                            current_input_json += event.get("partial_json", "")

                        elif event_type == "content_block_stop":
                            if current_tool_use:
                                try:
                                    tool_input = json.loads(current_input_json) if current_input_json else {}
                                except json.JSONDecodeError:
                                    tool_input = {}
                                tool_name = current_tool_use.get("name", "")
                                tool_block = {
                                    "id": current_tool_use.get("id", ""),
                                    "name": tool_name,
                                    "input": tool_input,
                                }
                                tool_use_blocks.append(tool_block)
                                if session_id:
                                    _notify_stream(session_id, description, {
                                        "event_type": "tool_use",
                                        "tool_name": tool_name,
                                        "tool_input": tool_input,
                                    })
                                current_tool_use = None
                                current_input_json = ""

                        elif event_type == "thinking_delta":
                            reasoning_content += event.get("thinking", "")

                        elif event_type == "thinking_start":
                            reasoning_content = ""

                        elif event_type == "error":
                            assistant_text += f"\n[Error: {event.get('error')}]"

                    # Success — exit retry loop
                    api_succeeded = True
                    break

                except Exception as e:
                    last_api_error = e
                    import traceback, logging
                    logger = logging.getLogger(__name__)
                    if api_attempt < max_api_retries - 1:
                        logger.warning(
                            f"AGENT_API_RETRY: desc={description}, attempt={api_attempt+1}/{max_api_retries}, "
                            f"error={e}\n{traceback.format_exc()}"
                        )
                        # Reset state for retry
                        assistant_text = ""
                        reasoning_content = ""
                        tool_use_blocks = []
                        current_tool_use = None
                        current_input_json = ""
                    else:
                        logger.error(
                            f"AGENT_API_ERROR: desc={description}, all {max_api_retries} attempts failed. "
                            f"Last error: {e}\n{traceback.format_exc()}"
                        )

            if not api_succeeded:
                error_msg = f"[Agent 中断: API调用全部失败 (重试{max_api_retries}次)。最后错误: {last_api_error}]"
                all_responses.append(error_msg)
                # Return structured error so gatekeeper knows this agent failed
                import logging
                logging.getLogger(__name__).error(
                    f"AGENT_INTERRUPTED: desc={description}, turns={turn_count}, error={last_api_error}"
                )
                return "\n".join(all_responses).strip()

            # 记录响应文本
            if assistant_text:
                all_responses.append(assistant_text)

            # 如果没有工具调用，停止前先尝试 XML 解析 (DeepSeek 会输出 XML 格式的工具调用)
            if not tool_use_blocks:
                from AutoRUN_v1.utils.xml_tool_parser import parse_and_strip_xml
                content_dicts = [{"type": "text", "text": assistant_text}] if assistant_text else []
                xml_blocks, xml_warnings = parse_and_strip_xml(content_dicts)
                if xml_blocks:
                    tool_use_blocks = xml_blocks
                    # 从 assistant_text 中移除 XML 标签
                    import re
                    assistant_text = re.sub(r'<tool_calls?\b[^>]*>.*?</tool_calls?\b[^>]*>', '', assistant_text, flags=re.DOTALL).strip()
                    # 通知 XML 解析出的工具调用
                    if session_id:
                        for xb in xml_blocks:
                            xname = xb.get("name", "")
                            xinput = xb.get("input", {})
                            _notify_stream(session_id, description, {
                                "event_type": "tool_use",
                                "tool_name": xname,
                                "tool_input": xinput,
                            })
                else:
                    break

            if not tool_use_blocks:
                break

            # ── 执行工具 ──
            tool_results: List[Dict[str, Any]] = []

            # 添加助手消息（必须包含 reasoning_content 以兼容 DeepSeek thinking mode）
            assistant_content: List[Dict[str, Any]] = []
            if reasoning_content:
                assistant_content.append({"type": "thinking", "thinking": reasoning_content})
            if assistant_text:
                assistant_content.append({"type": "text", "text": assistant_text})
            for tb in tool_use_blocks:
                # Handle both ToolUseBlock objects and dicts
                if hasattr(tb, 'id'):
                    assistant_content.append({"type": "tool_use", "id": tb.id,
                                              "name": tb.name, "input": tb.input})
                else:
                    assistant_content.append({"type": "tool_use", "id": tb["id"],
                                              "name": tb["name"], "input": tb["input"]})
            messages.append({"role": "assistant", "content": assistant_content})

            # 执行每个工具
            for tb in tool_use_blocks:
                tool_result = await self._execute_single_tool(tb, agent_tools, context)
                tool_results.append(tool_result)
                # 通知流式回调
                if session_id:
                    tb_name = tb.name if hasattr(tb, 'name') else tb.get("name", "")
                    result_content = str(tool_result.get("content", "")) if isinstance(tool_result, dict) else str(tool_result)
                    is_error = tool_result.get("is_error", False) if isinstance(tool_result, dict) else False
                    _notify_stream(session_id, description, {
                        "event_type": "tool_result",
                        "tool_name": tb_name,
                        "text": result_content if len(result_content) <= 2000 else result_content[:2000] + "...",
                        "is_error": is_error,
                    })

            # 添加工具结果消息
            messages.append({"role": "user", "content": tool_results})

        except asyncio.CancelledError:
            import logging
            logging.getLogger(__name__).warning(f"AGENT_CANCELLED: desc={description}, turns={turn_count}")
            return f"[Agent 已取消: {description}] (执行了 {turn_count} 轮后门控Agent 取消)"
        except Exception as e:
            import traceback, logging
            logging.getLogger(__name__).error(
                f"AGENT_FATAL: desc={description}, turns={turn_count}, error={e}\n{traceback.format_exc()}"
            )
            return f"[Agent 致命错误: {description}]\n错误: {e}"

        import logging
        result = "\n\n".join(all_responses).strip()
        # If no text was captured but tools were executed, build a tool summary
        if not result and turn_count > 1:
            tool_summary = f"(Agent 执行了 {turn_count} 轮工具调用，无文本输出。最后消息角色: {messages[-1]['role'] if messages else 'none'})"
            result = tool_summary
        logging.getLogger(__name__).warning(f"AGENT_DONE: desc={description}, turns={turn_count}, all_responses={len(all_responses)}, result_len={len(result)}")
        return result

    async def _execute_single_tool(self, tool_block: Dict[str, Any],
                                    tool_definitions: List[Dict[str, Any]],
                                    context: ToolContext) -> Dict[str, Any]:
        """执行单个工具并返回结果块。"""
        # Handle both ToolUseBlock objects and dicts
        if hasattr(tool_block, 'name'):
            tool_name = tool_block.name
            tool_input = tool_block.input
            tool_id = tool_block.id
        else:
            tool_name = tool_block["name"]
            tool_input = tool_block["input"]
            tool_id = tool_block["id"]

        # 查找工具定义
        tool_def = None
        for t in tool_definitions:
            if t.get("name") == tool_name:
                tool_def = t
                break

        if tool_def is None:
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": f"Unknown tool: {tool_name}",
                "is_error": True,
            }

        try:
            if "call_fn" in tool_def:
                result = await tool_def["call_fn"](tool_input, context)
                if isinstance(result, ToolResult):
                    return {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(result.data),
                        "is_error": result.is_error,
                    }
                elif isinstance(result, str):
                    return {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    }
                else:
                    return {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps(result),
                    }
            else:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Tool '{tool_name}' has no call_fn configured.",
                    "is_error": True,
                }
        except Exception as e:
            import traceback as _tb, logging
            logging.getLogger(__name__).error(
                f"Tool execution error: tool={tool_name}, error={e}\n{_tb.format_exc()}"
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": f"Tool execution error: {e}\n\nTraceback:\n{_tb.format_exc()}",
                "is_error": True,
            }
