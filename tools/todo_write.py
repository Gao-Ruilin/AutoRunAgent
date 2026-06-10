"""
TodoWriteTool — Legacy todo list management.

Mirrors src/tools/TodoWriteTool/ — the original task tracking tool
before TaskCreate v2. Kept for backward compatibility.
"""

from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


def _get_state(context: ToolContext) -> Optional[Any]:
    """Get AppState from tool context, or None if unavailable."""
    return getattr(context, 'state', None)


class TodoWriteTool(Tool):
    """Write and manage a structured todo list (legacy format)."""

    @property
    def name(self) -> str:
        return "TodoWrite"

    @property
    def description(self) -> str:
        return """使用此工具为当前编码会话创建和管理结构化任务列表。这有助于跟踪进度、组织复杂任务并向用户展示你的周全。

## 何时使用此工具
在以下情况主动使用：
1. 复杂的多步骤任务（3+ 个不同步骤）
2. 需要仔细规划的非平凡和复杂任务
3. 用户明确请求待办列表
4. 用户提供多个任务（编号/逗号分隔）
5. 收到新指令后 — 将需求捕获为待办事项

## 何时不使用
以下情况跳过：
1. 单一、简单的任务
2. 没有组织益处的琐碎任务
3. 可在 < 3 个简单步骤内完成的任务
4. 纯粹对话/信息性的任务

## 任务状态
- pending: 尚未开始
- in_progress: 正在进行中（一次只能一个）
- completed: 成功完成
- cancelled: 不再需要

重要：
- 完成后立即标记（不要批量处理）
- 一次只有一个任务处于 in_progress
- 即使在合并模式下也始终包含 'content' 字段
- 批量更新：当完成一个任务并开始另一个时，在一次调用中同时更新两者"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Task description",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Task status",
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Present continuous form (e.g., 'Running tests')",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        state = _get_state(context)
        if state is None:
            return ToolResult(data="Error: session state unavailable", is_error=True)

        new_todos = args.get("todos", [])

        if not new_todos:
            return ToolResult(data="Error: todos array is required", is_error=True)

        state.todos = new_todos

        # Build display
        status_icons = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
            "cancelled": "[-]",
        }

        lines = ["## Todo List"]
        for t in state.todos:
            icon = status_icons.get(t.get("status", "pending"), "[?]")
            content = t.get("content", "")
            lines.append(f"  {icon} {content}")

        return ToolResult(data="\n".join(lines), is_error=False)


def get_todos(state: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Get the current todo list from state, or fallback to empty."""
    if state is not None and hasattr(state, 'todos'):
        return state.todos
    return []
