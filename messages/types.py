"""
AutoRUN 对话系统的消息类型定义。

对应 src/types/message.ts — 定义对话循环中使用的所有消息类型:
UserMessage, AssistantMessage, SystemMessage 等。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

# ── Content block types ─────────────────────────────────────────────────────

class TextBlock(object):
    """消息中的文本内容块。"""
    def __init__(self, text: str, type: str = "text"):
        self.type = type
        self.text = text

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "text": self.text}


class ToolUseBlock(object):
    """助手消息中的 tool_use 内容块。"""
    def __init__(self,
                 id: str,
                 name: str,
                 input: Dict[str, Any],
                 type: str = "tool_use"):
        self.type = type
        self.id = id
        self.name = name
        self.input = input

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


class ToolResultBlock(object):
    """用户消息中的 tool_result 内容块。"""
    def __init__(self,
                 tool_use_id: str,
                 content: Union[str, List[Dict[str, Any]]],
                 is_error: bool = False,
                 type: str = "tool_result"):
        self.type = type
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "tool_use_id": self.tool_use_id,
            "content": self.content,
            "is_error": self.is_error,
        }


class ThinkingBlock(object):
    """助手消息中的 thinking 内容块（DeepSeek extended thinking）。"""
    def __init__(self, thinking: str, type: str = "thinking",
                 signature: str = ""):
        self.type = type
        self.thinking = thinking
        self.signature = signature

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"type": self.type, "thinking": self.thinking}
        if self.signature:
            result["signature"] = self.signature
        return result


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock, Dict[str, Any]]
MessageContent = Union[str, List[ContentBlock]]

# ── Message types ───────────────────────────────────────────────────────────

MessageType = str  # 'user' | 'assistant' | 'system' | 'attachment' | 'progress'


class Message(object):
    """基础消息类型，带有 discriminant `type` 字段。"""
    def __init__(self,
                 type: MessageType,
                 content: MessageContent = None,
                 uuid: Optional[str] = None,
                 is_meta: bool = False,
                 is_compact_summary: bool = False,
                 message: Optional[Dict[str, Any]] = None,
                 **kwargs):
        self.type = type
        self.uuid = uuid or str(uuid4())
        self.content = content
        self.is_meta = is_meta
        self.is_compact_summary = is_compact_summary
        self.message = message or {}
        self.meta = kwargs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "uuid": self.uuid,
            "content": self._serialize_content(),
            "is_meta": self.is_meta,
            "is_compact_summary": self.is_compact_summary,
            "message": self.message,
            **self.meta,
        }

    def _serialize_content(self) -> Any:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            return [
                item.to_dict() if hasattr(item, 'to_dict') else item
                for item in self.content
            ]
        return self.content

    def get_text(self) -> Optional[str]:
        """从内容中提取纯文本。"""
        if self.content is None:
            return None
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            texts = []
            for item in self.content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                elif hasattr(item, 'text'):
                    texts.append(item.text)
            return "\n".join(texts) if texts else None
        return None

    def get_tool_use_blocks(self) -> List[ToolUseBlock]:
        """从助手内容中提取 tool_use 块。"""
        blocks = []
        items = self.content if isinstance(self.content, list) else []
        for item in items:
            if isinstance(item, ToolUseBlock):
                blocks.append(item)
            elif isinstance(item, dict) and item.get("type") == "tool_use":
                blocks.append(ToolUseBlock(
                    id=item["id"],
                    name=item["name"],
                    input=item.get("input", {}),
                ))
        return blocks

    def get_tool_result_blocks(self) -> List[ToolResultBlock]:
        """从用户内容中提取 tool_result 块。"""
        blocks = []
        items = self.content if isinstance(self.content, list) else []
        for item in items:
            if isinstance(item, ToolResultBlock):
                blocks.append(item)
            elif isinstance(item, dict) and item.get("type") == "tool_result":
                blocks.append(ToolResultBlock(
                    tool_use_id=item["tool_use_id"],
                    content=item.get("content", ""),
                    is_error=item.get("is_error", False),
                ))
        return blocks


class UserMessage(Message):
    """来自用户的消息。"""
    def __init__(self,
                 content: MessageContent = None,
                 uuid: Optional[str] = None,
                 is_meta: bool = False,
                 tool_use_result: Any = None,
                 **kwargs):
        super().__init__(
            type="user",
            content=content,
            uuid=uuid,
            is_meta=is_meta,
            **kwargs
        )
        self.tool_use_result = tool_use_result

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if self.tool_use_result is not None:
            result["tool_use_result"] = self.tool_use_result
        return result


class AssistantMessage(Message):
    """来自助手的消息。"""
    def __init__(self,
                 content: MessageContent = None,
                 uuid: Optional[str] = None,
                 model: str = None,
                 stop_reason: Optional[str] = None,
                 usage: Optional[Dict[str, Any]] = None,
                 is_api_error_message: bool = False,
                 api_error: Optional[str] = None,
                 **kwargs):
        super().__init__(
            type="assistant",
            content=content,
            uuid=uuid,
            **kwargs
        )
        self.model = model
        self.stop_reason = stop_reason
        self.usage = usage or {}
        self.is_api_error_message = is_api_error_message
        self.api_error = api_error

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if self.model:
            result["model"] = self.model
        if self.stop_reason:
            result["stop_reason"] = self.stop_reason
        if self.usage:
            result["usage"] = self.usage
        if self.is_api_error_message:
            result["is_api_error_message"] = self.is_api_error_message
        if self.api_error:
            result["api_error"] = self.api_error
        return result


class SystemMessage(Message):
    """系统级消息（信息、警告、压缩边界等）。"""
    def __init__(self,
                 content: str = None,
                 level: str = "info",
                 uuid: Optional[str] = None,
                 **kwargs):
        super().__init__(
            type="system",
            content=content,
            uuid=uuid,
            **kwargs
        )
        self.level = level

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        result["level"] = self.level
        return result


class AttachmentMessage(Message):
    """附件消息（memory、文件变更、技能等）。"""
    def __init__(self,
                 attachment_type: str,
                 attachment_data: Dict[str, Any] = None,
                 uuid: Optional[str] = None,
                 **kwargs):
        super().__init__(
            type="attachment",
            content=None,
            uuid=uuid,
            attachment={"type": attachment_type, **(attachment_data or {})},
            **kwargs
        )
        self.attachment_type = attachment_type
        self.attachment_data = attachment_data or {}

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        result["attachment_type"] = self.attachment_type
        result["attachment_data"] = self.attachment_data
        return result


class ProgressMessage(Message):
    """工具执行过程中的进度更新。"""
    def __init__(self,
                 data: Any,
                 tool_use_id: Optional[str] = None,
                 uuid: Optional[str] = None,
                 **kwargs):
        super().__init__(
            type="progress",
            content=None,
            uuid=uuid,
            **kwargs
        )
        self.data = data
        self.tool_use_id = tool_use_id

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        result["data"] = self.data
        if self.tool_use_id:
            result["tool_use_id"] = self.tool_use_id
        return result


# ── Stream events ───────────────────────────────────────────────────────────

class StreamEvent:
    """查询流产生的事件的包装器。"""
    def __init__(self, type: str, data: Any = None):
        self.type = type
        self.data = data

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": self.data}


class RequestStartEvent(StreamEvent):
    """当新 API 请求开始时发出。"""
    def __init__(self):
        super().__init__(type="stream_request_start")


# ── Deserialization ──────────────────────────────────────────────────────────

def _dict_to_content_blocks(content: Any) -> MessageContent:
    """将序列化的 content 列表还原为 content block 对象。"""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = []
        for item in content:
            if not isinstance(item, dict):
                blocks.append(item)
                continue
            bt = item.get("type", "")
            if bt == "text":
                blocks.append(TextBlock(text=item.get("text", "")))
            elif bt == "tool_use":
                blocks.append(ToolUseBlock(
                    id=item.get("id", ""),
                    name=item.get("name", ""),
                    input=item.get("input", {}),
                ))
            elif bt == "tool_result":
                blocks.append(ToolResultBlock(
                    tool_use_id=item.get("tool_use_id", ""),
                    content=item.get("content", ""),
                    is_error=item.get("is_error", False),
                ))
            elif bt == "thinking":
                blocks.append(ThinkingBlock(
                    thinking=item.get("thinking", ""),
                    signature=item.get("signature", ""),
                ))
            else:
                blocks.append(item)
        return blocks
    return content


def message_from_dict(data: Dict[str, Any]) -> Message:
    """从字典反序列化创建 Message 对象。

    根据 type 字段分派到正确的子类。
    """
    msg_type = data.get("type", "user")
    kwargs: Dict[str, Any] = {
        "uuid": data.get("uuid"),
        "is_meta": data.get("is_meta", False),
        "is_compact_summary": data.get("is_compact_summary", False),
        "message": data.get("message"),
    }
    # Pass through any extra meta keys
    known_keys = {
        "type", "uuid", "content", "is_meta", "is_compact_summary",
        "message", "model", "stop_reason", "usage", "is_api_error_message",
        "api_error", "level", "tool_use_result", "attachment_type",
        "attachment_data", "data", "tool_use_id",
    }
    for k, v in data.items():
        if k not in known_keys:
            kwargs[k] = v

    content = _dict_to_content_blocks(data.get("content"))

    if msg_type == "user":
        return UserMessage(
            content=content,
            tool_use_result=data.get("tool_use_result"),
            **kwargs
        )
    elif msg_type == "assistant":
        return AssistantMessage(
            content=content,
            model=data.get("model"),
            stop_reason=data.get("stop_reason"),
            usage=data.get("usage"),
            is_api_error_message=data.get("is_api_error_message", False),
            api_error=data.get("api_error"),
            **kwargs
        )
    elif msg_type == "system":
        return SystemMessage(
            content=content if isinstance(content, str) else (
                data.get("content") if isinstance(data.get("content"), str) else None
            ),
            level=data.get("level", "info"),
            **kwargs
        )
    elif msg_type == "attachment":
        return AttachmentMessage(
            attachment_type=data.get("attachment_type", ""),
            attachment_data=data.get("attachment_data"),
            **kwargs
        )
    elif msg_type == "progress":
        return ProgressMessage(
            data=data.get("data"),
            tool_use_id=data.get("tool_use_id"),
            **kwargs
        )
    else:
        return Message(
            type=msg_type,
            content=content,
            **kwargs
        )


# ── Factory functions ───────────────────────────────────────────────────────

def create_user_message(content: Union[str, List[Dict[str, Any]]],
                        is_meta: bool = False,
                        tool_use_result: Any = None) -> UserMessage:
    """从字符串或内容块创建 UserMessage。"""
    msg_content: MessageContent
    if isinstance(content, str):
        msg_content = [TextBlock(text=content)]
    else:
        msg_content = content
    return UserMessage(content=msg_content, is_meta=is_meta, tool_use_result=tool_use_result)


def create_assistant_api_error_message(content: str,
                                        error: str = "invalid_request") -> AssistantMessage:
    """创建一个错误 AssistantMessage。"""
    return AssistantMessage(
        content=[TextBlock(text=content)],
        is_api_error_message=True,
        api_error=error,
    )


def create_system_message(content: str, level: str = "info") -> SystemMessage:
    """创建一个 SystemMessage。"""
    return SystemMessage(content=content, level=level)


def create_user_interruption_message(tool_use: bool = False) -> UserMessage:
    """创建一个中断 UserMessage。"""
    return UserMessage(
        content=[TextBlock(text="[Request interrupted by user]")],
        is_meta=True,
    )


def create_attachment_message(attachment_type: str,
                               attachment_data: Dict[str, Any] = None) -> AttachmentMessage:
    """创建一个 AttachmentMessage。"""
    return AttachmentMessage(
        attachment_type=attachment_type,
        attachment_data=attachment_data,
    )
