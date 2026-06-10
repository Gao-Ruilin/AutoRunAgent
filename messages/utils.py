"""
消息工具函数。

对应 src/utils/messages.ts — 消息管道的标准化、转换和工厂函数。
"""

from typing import Any, Dict, List, Optional

from AutoRUN_v1.context import build_context_text
from .types import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_assistant_api_error_message,
    create_user_message,
)


def _has_tool_use_blocks(msg: Message) -> bool:
    """检查消息是否包含 tool_use 内容块。"""
    content = msg.content
    if not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, ToolUseBlock):
            return True
        if isinstance(item, dict) and item.get("type") == "tool_use":
            return True
    return False


def _get_tool_use_ids(msg: Message) -> set:
    """获取消息中所有 tool_use block 的 ID 集合。"""
    ids = set()
    content = msg.content
    if not isinstance(content, list):
        return ids
    for item in content:
        if isinstance(item, ToolUseBlock):
            ids.add(item.id)
        elif isinstance(item, dict) and item.get("type") == "tool_use":
            ids.add(item.get("id", ""))
    return ids


def _get_tool_result_ids(msg: Message) -> set:
    """获取消息中所有 tool_result block 的 tool_use_id 集合。"""
    ids = set()
    content = msg.content
    if not isinstance(content, list):
        return ids
    for item in content:
        if isinstance(item, ToolResultBlock):
            ids.add(item.tool_use_id)
        elif isinstance(item, dict) and item.get("type") == "tool_result":
            ids.add(item.get("tool_use_id", ""))
    return ids


def _strip_tool_use_from_assistant(msg: Message) -> Message:
    """从助手消息中移除所有 tool_use block，仅保留文本内容。"""
    content = msg.content
    if not isinstance(content, list):
        return msg
    filtered = [
        item for item in content
        if not (
            isinstance(item, ToolUseBlock) or
            (isinstance(item, dict) and item.get("type") == "tool_use")
        )
    ]
    # Keep at least one text block if content became empty
    if not filtered:
        filtered = [TextBlock(text="")]
    new_msg = AssistantMessage(
        content=filtered,
        uuid=msg.uuid,
        model=getattr(msg, 'model', None),
        stop_reason=getattr(msg, 'stop_reason', None),
        usage=getattr(msg, 'usage', None),
        is_api_error_message=getattr(msg, 'is_api_error_message', False),
        api_error=getattr(msg, 'api_error', None),
    )
    return new_msg


def _repair_unpaired_tool_uses(messages: List[Message]) -> List[Message]:
    """修复未配对的 tool_use/tool_result 消息序列。

    当 AssistantMessage 包含 tool_use block 但下一个消息不是
    包含对应 tool_result 的 UserMessage 时（例如用户中断工具执行），
    移除未配对的 tool_use block 以确保 API 不会收到格式错误。
    """
    if not messages:
        return messages

    repaired = list(messages)
    i = 0
    while i < len(repaired):
        msg = repaired[i]
        if msg.type == "assistant" and _has_tool_use_blocks(msg):
            tool_use_ids = _get_tool_use_ids(msg)
            if not tool_use_ids:
                i += 1
                continue

            # Check if next message is a user message with matching tool_results
            next_idx = i + 1
            paired = False
            if next_idx < len(repaired) and repaired[next_idx].type == "user":
                result_ids = _get_tool_result_ids(repaired[next_idx])
                if tool_use_ids == result_ids:
                    paired = True
                elif result_ids:
                    # Partial match — some tool results present, some missing
                    paired = tool_use_ids.issubset(result_ids)

            if not paired:
                # Strip tool_use blocks from this assistant message
                repaired[i] = _strip_tool_use_from_assistant(msg)

        i += 1

    return repaired


def normalize_messages_for_api(messages: List[Message]) -> List[Dict[str, Any]]:
    """将内部 Message 对象转换为 API 提供商格式。

    过滤掉 system/attachment/progress 消息，将剩余的
    user/assistant 消息转换为 API 的内容块格式。

    同时修复未配对的 tool_use/tool_result 序列（例如中断后的残留）。
    """
    # Repair unpaired tool_use blocks before conversion
    repaired = _repair_unpaired_tool_uses(messages)

    api_messages: List[Dict[str, Any]] = []
    for msg in repaired:
        if msg.type == "user":
            api_messages.append(_normalize_user_message(msg))
        elif msg.type == "assistant":
            api_messages.append(_normalize_assistant_message(msg))
        # system, attachment, progress are not sent to API
    return api_messages


def _normalize_user_message(msg: Message) -> Dict[str, Any]:
    """将用户消息转换为 API 格式。"""
    content = _serialize_content(msg.content)
    return {"role": "user", "content": content}


def _normalize_assistant_message(msg: Message) -> Dict[str, Any]:
    """将助手消息转换为 API 格式。"""
    content = _serialize_content(msg.content)
    return {"role": "assistant", "content": content}


def _serialize_content(content: Any) -> Any:
    """将消息内容序列化为 API 兼容格式。"""
    if content is None:
        return [{"type": "text", "text": ""}]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result = []
        for item in content:
            if hasattr(item, 'to_dict'):
                result.append(item.to_dict())
            elif isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str):
                result.append({"type": "text", "text": item})
        return result
    return content


def normalize_content_from_api(content: Any) -> List[Any]:
    """将 API 响应内容块标准化为内部对象。"""
    if isinstance(content, str):
        return [TextBlock(text=content)]
    if isinstance(content, list):
        result = []
        for item in content:
            if isinstance(item, dict):
                block_type = item.get("type", "")
                if block_type == "text":
                    result.append(TextBlock(text=item.get("text", "")))
                elif block_type == "tool_use":
                    result.append(ToolUseBlock(
                        id=item.get("id", ""),
                        name=item.get("name", ""),
                        input=item.get("input", {}),
                    ))
                elif block_type == "tool_result":
                    result.append(ToolResultBlock(
                        tool_use_id=item.get("tool_use_id", ""),
                        content=item.get("content", ""),
                        is_error=item.get("is_error", False),
                    ))
                else:
                    result.append(item)
            else:
                result.append(item)
        return result
    return [TextBlock(text=str(content))]


def get_messages_after_compact_boundary(messages: List[Message]) -> List[Message]:
    """查找最后一个压缩边界之后的消息。

    压缩边界是一个系统消息，标记了压缩发生的位置。
    之前的所有内容已被总结，不应发送给 API。
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.type == "system" and msg.is_compact_summary:
            return messages[i:]
    return messages


def prepend_user_context(messages: List[Dict[str, Any]],
                          user_context: Dict[str, str]) -> List[Dict[str, Any]]:
    """将用户上下文作为用户消息添加到消息列表前面。"""
    context_text = build_context_text(user_context)
    if context_text:
        return [
            {"role": "user", "content": context_text}
        ] + messages
    return messages


def strip_signature_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从消息中移除思考签名块（用于模型回退）。"""
    result = []
    for msg in messages:
        content = msg.get("content", [])
        if isinstance(content, list):
            filtered = [
                block for block in content
                if not (isinstance(block, dict) and
                        block.get("type") in ("thinking", "redacted_thinking"))
            ]
            result.append({**msg, "content": filtered})
        else:
            result.append(msg)
    return result


def create_tool_use_summary_message(summary: str, tool_use_ids: List[str]) -> Message:
    """创建工具调用摘要系统消息。"""
    return SystemMessage(
        content=summary,
        meta={"tool_use_ids": tool_use_ids, "is_tool_use_summary": True},
    )


def create_microcompact_boundary_message(trigger: str,
                                           tokens_freed: int,
                                           deleted_tokens: int,
                                           deleted_tool_ids: List[str],
                                           kept_tool_ids: List[str]) -> Message:
    """创建微压缩边界系统消息。"""
    return SystemMessage(
        content=f"[Microcompact: freed {tokens_freed} tokens, deleted {deleted_tokens} cache tokens]",
        meta={
            "compact_metadata": {
                "trigger": trigger,
                "tokens_freed": tokens_freed,
                "deleted_tokens": deleted_tokens,
                "deleted_tool_ids": deleted_tool_ids,
                "kept_tool_ids": kept_tool_ids,
            }
        },
        is_compact_summary=True,
    )
