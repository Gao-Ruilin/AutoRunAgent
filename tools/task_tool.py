"""
TaskCreateTool — Task management for complex workflows.

Mirrors src/tools/TaskTool/ — creates, updates, and lists tasks
to help manage multi-step implementation work.
"""

from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


STATUS_VALUES = ["pending", "in_progress", "completed", "deleted"]


def _get_state(context: ToolContext) -> Optional[Any]:
    """Get AppState from tool context, or None if unavailable."""
    return getattr(context, 'state', None)


def get_all_tasks(state: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return all current tasks (excluding deleted). Used by REST API.

    Args:
        state: AppState instance with tasks dict. Falls back to empty list if None.
    """
    if state is None or not hasattr(state, 'tasks'):
        return []
    return [
        t for t in state.tasks.values()
        if t.get("status") != "deleted"
    ]


# 所有任务/待办相关工具的名称集合
TASK_TOOL_NAMES = {
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    "TaskStop",
}


def get_all_tasks_for_display(state: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return unified task list for UI display (merges v2 tasks + legacy todos).

    Returns list of dicts with: id, label, status.
    Sorted: in_progress first, then pending, then completed.
    Excludes deleted/cancelled items.
    """
    items: List[Dict[str, Any]] = []
    if state is None:
        return items

    # V2 tasks (TaskCreate/TaskUpdate)
    if hasattr(state, 'tasks'):
        for tid, t in state.tasks.items():
            status = t.get("status", "pending")
            if status == "deleted":
                continue
            items.append({
                "id": tid,
                "label": t.get("subject", tid),
                "status": status,
            })

    # Sort: in_progress first, then pending, then completed
    order = {"in_progress": 0, "pending": 1, "completed": 2}
    items.sort(key=lambda t: order.get(t["status"], 9))
    return items


class TaskCreateTool(Tool):
    """Create and manage structured task lists."""

    @property
    def name(self) -> str:
        return "TaskCreate"

    @property
    def description(self) -> str:
        return """在当前编码会话中创建结构化任务列表以跟踪进度。

## 何时使用此工具

在以下场景中主动使用此工具：
- 复杂的多步骤任务 — 当任务需要 3 个或更多不同步骤时
- 非平凡且复杂的任务 — 需要仔细规划的任务
- 用户明确请求待办列表 — 当要求跟踪任务时
- 用户提供多个任务 — 编号或逗号分隔的要完成事项列表
- 收到新指令后 — 立即将用户需求捕获为任务

## 何时不使用

以下情况跳过使用此工具：
- 只有一个简单的任务
- 任务很琐碎，跟踪没有任何好处
- 任务可以在不到 3 个简单步骤内完成
- 任务是纯粹对话或信息性的

## 任务字段
- subject: 简短的、可操作的标题，使用祈使形式
- description: 需要做什么
- activeForm（可选）: 当状态为 in_progress 时在旋转指示器中显示的现在进行时形式

所有任务创建时状态为 pending。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "A brief title for the task",
                },
                "description": {
                    "type": "string",
                    "description": "What needs to be done",
                },
                "activeForm": {
                    "type": "string",
                    "description": "Present continuous form shown in spinner when in_progress",
                },
                "metadata": {
                    "type": "object",
                    "description": "Arbitrary metadata to attach to the task",
                },
            },
            "required": ["subject", "description"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        state = _get_state(context)
        if state is None:
            return ToolResult(data="Error: session state unavailable", is_error=True)

        subject = args.get("subject", "").strip()
        description = args.get("description", "")

        if not subject:
            return ToolResult(data="Error: subject is required", is_error=True)

        state.task_counter += 1
        task_id = str(state.task_counter)

        task = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "activeForm": args.get("activeForm", subject),
            "metadata": args.get("metadata", {}),
            "blocks": [],
            "blockedBy": [],
            "owner": "",
        }

        state.tasks[task_id] = task

        return ToolResult(
            data=f"Task #{task_id} created: {subject} (status: pending)",
            is_error=False,
        )


class TaskListTool(Tool):
    """List all tasks and their statuses."""

    @property
    def name(self) -> str:
        return "TaskList"

    @property
    def description(self) -> str:
        return """列出任务列表中的所有任务。

## 何时使用此工具
- 查看有哪些可处理的任务
- 检查整体进度
- 查找被阻塞且需要解决依赖关系的任务
- 完成任务后，检查是否有新解除阻塞的工作

## 输出
返回每个任务的摘要，包括 id、subject、status、owner 和 blockedBy。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        state = _get_state(context)
        if state is None or not state.tasks:
            return ToolResult(data="No tasks in the task list.", is_error=False)

        tasks = state.tasks
        lines = []
        for task_id in sorted(tasks.keys(), key=int):
            task = tasks[task_id]
            status = task["status"]
            if status == "deleted":
                continue

            status_icon = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(status, "[?]")

            line = f"#{task['id']} {status_icon} {task['subject']}"
            if task["blockedBy"]:
                line += f" [blocked by: {', '.join(task['blockedBy'])}]"
            lines.append(line)

        return ToolResult(
            data="\n".join(lines) if lines else "No active tasks.",
            is_error=False,
        )


class TaskGetTool(Tool):
    """Get full details of a specific task."""

    @property
    def name(self) -> str:
        return "TaskGet"

    @property
    def description(self) -> str:
        return """通过 ID 从任务列表中检索任务。

## 何时使用此工具
- 在开始工作前需要完整的描述和上下文时
- 了解任务依赖关系（它阻塞哪些任务，被哪些任务阻塞）
- 被分配任务后，获取完整需求

## 输出
返回完整任务详情，包括 subject、description、status、blocks 和 blockedBy。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "taskId": {
                    "type": "string",
                    "description": "The ID of the task to retrieve",
                },
            },
            "required": ["taskId"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        state = _get_state(context)
        if state is None:
            return ToolResult(data="Error: session state unavailable", is_error=True)

        task_id = args.get("taskId", "")
        tasks = state.tasks

        if task_id not in tasks:
            return ToolResult(
                data=f"Error: Task #{task_id} not found.",
                is_error=True,
            )

        task = tasks[task_id]
        return ToolResult(
            data=f"Task #{task['id']}: {task['subject']}\n"
                 f"Status: {task['status']}\n"
                 f"Description: {task['description']}\n"
                 f"Blocks: {task['blocks']}\n"
                 f"Blocked by: {task['blockedBy']}\n"
                 f"Owner: {task['owner'] or '(unassigned)'}",
            is_error=False,
        )


class TaskUpdateTool(Tool):
    """Update a task's status, subject, or dependencies."""

    @property
    def name(self) -> str:
        return "TaskUpdate"

    @property
    def description(self) -> str:
        return """更新任务列表中的任务。

## 何时使用此工具

当完成工作时将任务标记为已解决。
删除不再相关的任务。
当需求变化时更新任务详情。
设置任务依赖关系。

## 状态流程
状态推进: pending → in_progress → completed
使用 deleted 永久删除任务。

重要：只有在完全完成任务时才将其标记为 completed。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "taskId": {
                    "type": "string",
                    "description": "The ID of the task to update",
                },
                "status": {
                    "type": "string",
                    "enum": STATUS_VALUES,
                    "description": "New status for the task",
                },
                "subject": {
                    "type": "string",
                    "description": "New subject for the task",
                },
                "description": {
                    "type": "string",
                    "description": "New description for the task",
                },
                "addBlocks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task IDs that this task blocks",
                },
                "addBlockedBy": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task IDs that block this task",
                },
            },
            "required": ["taskId"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        state = _get_state(context)
        if state is None:
            return ToolResult(data="Error: session state unavailable", is_error=True)

        task_id = args.get("taskId", "")
        tasks = state.tasks

        if task_id not in tasks:
            return ToolResult(
                data=f"Error: Task #{task_id} not found.",
                is_error=True,
            )

        task = tasks[task_id]
        changes = []

        if "status" in args:
            old_status = task["status"]
            new_status = args["status"]
            task["status"] = new_status
            changes.append(f"Status: {old_status} → {new_status}")

        if "subject" in args:
            task["subject"] = args["subject"]
            changes.append(f"Subject updated")

        if "description" in args:
            task["description"] = args["description"]
            changes.append(f"Description updated")

        if "addBlocks" in args:
            for block_id in args["addBlocks"]:
                if block_id not in task["blocks"]:
                    task["blocks"].append(block_id)

        if "addBlockedBy" in args:
            for block_id in args["addBlockedBy"]:
                if block_id not in task["blockedBy"]:
                    task["blockedBy"].append(block_id)

        return ToolResult(
            data=f"Task #{task_id} updated: {'; '.join(changes)}",
            is_error=False,
        )


class TaskOutputTool(Tool):
    """Retrieve output from background Agent or task.

Results are indexed by Agent description — use the description as task_id for retrieval."""

    @property
    def name(self) -> str:
        return "TaskOutput"

    @property
    def description(self) -> str:
        return """从后台 Agent 或任务中检索输出。

- 接受 task_id 参数来标识任务（Agent 的 description 或任务 ID）
- 精准匹配 — 先按 task_id 查找对应 Agent 的结果
- 找到则返回该 Agent 的完整输出；未找到则返回所有待处理结果
- 如果 Agent 仍在运行，返回运行状态及描述
- task_id 留空时返回所有已完成的后台结果"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to get output from. Can be Agent description, task ID, etc. Leave empty to drain all results.",
                },
                "block": {
                    "type": "boolean",
                    "description": "Whether to wait for completion",
                    "default": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max wait time in ms",
                    "minimum": 0,
                    "maximum": 600000,
                },
            },
            "required": ["task_id", "block", "timeout"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        task_id = args.get("task_id", "")
        timeout = args.get("timeout", 30000)
        block = args.get("block", True)

        from AutoRUN_v1.tools.agent_tool import (
            drain_background_results, drain_background_result_by_desc,
            _background_tasks,
        )

        # 确定 session_id
        session_id = getattr(context.state, 'session_id', None) if context.state else None
        session_id = session_id or "default"

        # 1. 如果指定了 task_id，精准按 description 查找
        if task_id:
            result = drain_background_result_by_desc(session_id, task_id)
            if result:
                return ToolResult(data=result, is_error=False)
            # 也尝试模糊匹配运行中的任务
            running = _background_tasks.get(session_id, [])
            matching = [e for e in running if task_id.lower() in e.get("description", "").lower()]
            if matching:
                descs = [e.get("description", "?") for e in matching]
                return ToolResult(
                    data=f"Agent 仍在运行中 — 匹配 '{task_id}': {', '.join(descs)}。稍后再检查结果。",
                    is_error=False,
                )

        # 2. 批量获取所有已完成结果
        result = drain_background_results(session_id)
        if result:
            return ToolResult(data=result, is_error=False)

        # 3. 检查是否有还在运行的任务
        running = _background_tasks.get(session_id, [])
        if running:
            descs = [e.get("description", "?") for e in running]
            return ToolResult(
                data=f"Agent 仍在运行中 ({len(running)}个): {', '.join(descs)}。完成后结果自动加入对话。",
                is_error=False,
            )

        # 4. 也检查 fallback "default" session
        if session_id != "default":
            result = drain_background_results("default")
            if result:
                return ToolResult(data=result, is_error=False)
            running = _background_tasks.get("default", [])
            if running:
                descs = [e.get("description", "?") for e in running]
                return ToolResult(
                    data=f"Agent 仍在运行中 ({len(running)}个): {', '.join(descs)}。",
                    is_error=False,
                )

        return ToolResult(
            data=f"未找到 Agent 结果。所有 Agent 已完成且结果已被取走，或无 Agent 运行。",
            is_error=False,
        )


class TaskStopTool(Tool):
    """Stop a running background task."""

    @property
    def name(self) -> str:
        return "TaskStop"

    @property
    def description(self) -> str:
        return """通过 ID 停止正在运行的后台任务。

- 接受 task_id 参数来标识要停止的任务
- 返回成功或失败状态
- 当需要终止长时间运行的任务时使用此工具"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to stop",
                },
            },
            "required": ["task_id"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        state = _get_state(context)
        if state is None:
            return ToolResult(data="Error: session state unavailable", is_error=True)

        task_id = args.get("task_id", "")
        tasks = state.tasks
        if task_id in tasks:
            tasks[task_id]["status"] = "completed"
            return ToolResult(data=f"Task #{task_id} stopped.", is_error=False)
        return ToolResult(
            data=f"Task #{task_id} not found.",
            is_error=True,
        )
