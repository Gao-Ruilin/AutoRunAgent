"""
Tool registry — assembles and exports all available tools.

Mirrors src/tools.ts — provides functions to get the full tool list,
filtered by feature flags and user type.
"""

from typing import Any, Callable, Dict, List, Optional, Set

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult, build_tool, tool_to_api_schema

# ── Tool imports ──────────────────────────────────────────────────────────────

from AutoRUN_v1.tools.bash_tool import BashTool
from AutoRUN_v1.tools.file_read import FileReadTool
from AutoRUN_v1.tools.file_write import FileWriteTool
from AutoRUN_v1.tools.file_edit import FileEditTool
from AutoRUN_v1.tools.glob_tool import GlobTool
from AutoRUN_v1.tools.grep_tool import GrepTool
from AutoRUN_v1.tools.web_fetch import WebFetchTool
from AutoRUN_v1.tools.web_search import WebSearchTool
from AutoRUN_v1.tools.agent_tool import AgentTool
from AutoRUN_v1.tools.task_tool import (
    TaskCreateTool,
    TaskListTool,
    TaskGetTool,
    TaskUpdateTool,
    TaskOutputTool,
    TaskStopTool,
)
from AutoRUN_v1.tools.ask_tool import AskUserQuestionTool
from AutoRUN_v1.tools.notebook_tool import NotebookEditTool
from AutoRUN_v1.tools.skill_tool import SkillTool, SkillToggleTool
from AutoRUN_v1.tools.plan_mode import EnterPlanModeTool, ExitPlanModeTool

from AutoRUN_v1.tools.brief_tool import BriefTool
from AutoRUN_v1.tools.send_message import SendMessageTool
from AutoRUN_v1.tools.workflow import WorkflowTool
from AutoRUN_v1.tools.worktree import EnterWorktreeTool, ExitWorktreeTool
from AutoRUN_v1.tools.tool_search import ToolSearchTool
from AutoRUN_v1.tools.powershell_tool import PowerShellTool
from AutoRUN_v1.tools.verify_plan import VerifyPlanExecutionTool
from AutoRUN_v1.tools.lsp_tool import LSPTool
from AutoRUN_v1.tools.ocr import OcrTool
from AutoRUN_v1.tools.skill_manager import SkillManageTool
from AutoRUN_v1.tools.agent_manager import AgentManageTool
from AutoRUN_v1.tools.ssh_tool import SSHBashTool, SSHReadTool, SSHWriteTool, SSHEditTool
from AutoRUN_v1.tools.connection_tool import ConnectionTool


# ── Tool registry ─────────────────────────────────────────────────────────────

# All tool classes
ALL_TOOL_CLASSES: List[type] = [
    BashTool,
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    GlobTool,
    GrepTool,
    WebFetchTool,
    WebSearchTool,
    AgentTool,
    TaskCreateTool,
    TaskListTool,
    TaskGetTool,
    TaskUpdateTool,
    TaskOutputTool,
    TaskStopTool,
    AskUserQuestionTool,
    NotebookEditTool,
    SkillTool,
    EnterPlanModeTool,
    ExitPlanModeTool,
    BriefTool,
    SendMessageTool,
    WorkflowTool,
    EnterWorktreeTool,
    ExitWorktreeTool,
    ToolSearchTool,
    PowerShellTool,
    VerifyPlanExecutionTool,
    LSPTool,
    OcrTool,
    SkillManageTool,
    SkillToggleTool,
    AgentManageTool,
    SSHBashTool,
    SSHReadTool,
    SSHWriteTool,
    SSHEditTool,
    ConnectionTool,
]


def get_tools(enabled_tools: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """Get the full list of tool definitions as API-compatible dicts.

    Args:
        enabled_tools: Optional set of tool names to include.
                       If None, all tools are included.

    Returns:
        List of tool definition dicts with 'name', 'description',
        'input_schema', and 'call_fn' keys.
    """
    tools = []

    for tool_cls in ALL_TOOL_CLASSES:
        tool_instance = tool_cls()

        if enabled_tools is not None and tool_instance.name not in enabled_tools:
            continue

        if not tool_instance.is_enabled():
            continue

        tools.append({
            "name": tool_instance.name,
            "description": tool_instance.description,
            "input_schema": tool_instance.input_schema,
            "call_fn": tool_instance.call,
            "tool_instance": tool_instance,
        })

    return tools


def get_tool_api_schemas(enabled_tools: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """Get tool definitions in Anthropic API schema format.

    Returns:
        List of tool schemas with 'name', 'description', 'input_schema' keys.
    """
    return [tool_to_api_schema(t) for t in get_tools(enabled_tools)]


def find_tool_by_name(name: str) -> Optional[Tool]:
    """Find a tool instance by name."""
    for tool_cls in ALL_TOOL_CLASSES:
        tool_instance = tool_cls()
        if tool_instance.name == name:
            return tool_instance
        if name in tool_instance.aliases:
            return tool_instance
    return None


# ── Exports ───────────────────────────────────────────────────────────────────

__all__ = [
    # Base
    "Tool",
    "ToolContext",
    "ToolResult",
    "build_tool",
    "tool_to_api_schema",
    # Individual tools
    "BashTool",
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GlobTool",
    "GrepTool",
    "WebFetchTool",
    "WebSearchTool",
    "AgentTool",
    "TaskCreateTool",
    "TaskListTool",
    "TaskGetTool",
    "TaskUpdateTool",
    "TaskOutputTool",
    "TaskStopTool",
    "AskUserQuestionTool",
    "NotebookEditTool",
    "SkillTool",
    "EnterPlanModeTool",
    "ExitPlanModeTool",
    "BriefTool",
    "SendMessageTool",
    "WorkflowTool",
    "EnterWorktreeTool",
    "ExitWorktreeTool",
    "ToolSearchTool",
    "PowerShellTool",
    "VerifyPlanExecutionTool",
    "LSPTool",
    "OcrTool",
    "SkillManageTool",
    "SkillToggleTool",
    "AgentManageTool",
    "SSHBashTool",
    "SSHReadTool",
    "SSHWriteTool",
    "SSHEditTool",
    "ConnectionTool",
    # Registry
    "ALL_TOOL_CLASSES",
    "get_tools",
    "get_tool_api_schemas",
    "find_tool_by_name",
]
