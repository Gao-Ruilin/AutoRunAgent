"""
SendMessageTool — Send messages to sub-agents.

When a background agent is running (all Agent calls are background by default),
the Gatekeeper can use SendMessage to inject follow-up instructions.
The message will be delivered to the agent when it finishes its current turn,
triggering a follow-up iteration with the new instructions.
"""

from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult

# ── Pending message registry ─────────────────────────────────────────────
# session_id → list of {target, message, description} dicts
_pending_messages: Dict[str, List[Dict[str, str]]] = {}


def store_pending_message(session_id: str, target: str, message: str,
                           description: str = "") -> None:
    """Store a pending message for a background agent. Called by SendMessage tool."""
    if session_id not in _pending_messages:
        _pending_messages[session_id] = []
    _pending_messages[session_id].append({
        "target": target,
        "message": message,
        "description": description,
    })


def drain_pending_messages(session_id: str, agent_description: str) -> Optional[str]:
    """Collect pending messages for a specific agent (matched by description substring).
    Returns combined message, or None."""
    if session_id not in _pending_messages:
        return None
    pending = _pending_messages[session_id]
    # Match by target name in agent description
    matched = []
    remaining = []
    for p in pending:
        target = p.get("target", "")
        # Fuzzy match: target appears in description or vice versa
        if target and (target.lower() in agent_description.lower()
                       or any(w in target.lower() for w in agent_description.lower().split())):
            matched.append(p)
        else:
            remaining.append(p)
    if not matched:
        return None
    _pending_messages[session_id] = remaining
    if not remaining:
        del _pending_messages[session_id]
    return "\n\n---\n".join(
        f"[SendMessage — {p.get('target', 'agent')}]\n{p.get('message', '')}"
        for p in matched
    )


class SendMessageTool(Tool):
    """Send follow-up messages to background agents."""

    @property
    def name(self) -> str:
        return "SendMessage"

    @property
    def description(self) -> str:
        return """继续与之前启动的代理或任务的对话。

使用此工具：
- 向正在运行或已完成的代理发送后续指令
- 用额外的上下文继续代理的工作
- 要求代理改进或扩展其输出
- 恢复暂停的代理对话

指定代理 ID（或名称）以及要发送的消息。
代理将恢复并保留其完整上下文。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "The agent ID or name to send the message to",
                },
                "message": {
                    "type": "string",
                    "description": "The message/instruction to send to the agent",
                },
            },
            "required": ["to", "message"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        target = args.get("to", "").strip()
        message = args.get("message", "").strip()

        if not target:
            return ToolResult(data="Error: 'to' (agent ID/name) is required", is_error=True)
        if not message:
            return ToolResult(data="Error: 'message' is required", is_error=True)

        session_id = getattr(context.state, 'session_id', None) if context.state else None
        if session_id:
            store_pending_message(session_id, target, message)

        return ToolResult(
            data=f"Message queued for agent '{target}'. "
                 f"It will receive this after its current step completes.",
            is_error=False,
        )
