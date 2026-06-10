"""
BriefTool — Conversation summary and compaction trigger.

Mirrors src/tools/BriefTool/ — generates summaries of conversations
to manage context window length by compacting old messages.
"""

import json
from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class BriefTool(Tool):
    """Summarize and compact conversation context."""

    @property
    def name(self) -> str:
        return "Brief"

    @property
    def description(self) -> str:
        return """总结对话历史以管理上下文长度。

此工具通过将较早的消息总结为简洁的摘要来压缩对话，
保留关键决策和上下文，同时释放上下文窗口空间。

在以下情况使用此工具：
- 对话接近上下文限制
- 需要总结到目前为止讨论的内容
- 想创建所做决策的检查点摘要
- 用户要求对话摘要

摘要应包含：
1. 关键决策及其理由
2. 重要的技术选择
3. 待处理的任务和后续步骤
4. 对话中了解到的用户偏好"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A concise summary of the conversation so far",
                },
                "key_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of key points or decisions made",
                },
                "token_count": {
                    "type": "integer",
                    "description": "Approximate token count of the messages being summarized",
                },
            },
            "required": ["summary"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        summary = args.get("summary", "").strip()
        key_points = args.get("key_points", [])
        token_count = args.get("token_count", 0)

        if not summary:
            return ToolResult(data="Error: summary is required", is_error=True)

        # Build compact summary message
        compact_msg = f"## Conversation Summary\n{summary}\n"

        if key_points:
            compact_msg += "\n### Key Points\n"
            for point in key_points:
                compact_msg += f"  - {point}\n"

        if token_count:
            compact_msg += f"\n*Approximately {token_count} tokens compacted.*\n"

        return ToolResult(
            data=compact_msg,
            is_error=False,
        )
