"""
Tool base class and interfaces.

Mirrors src/Tool.ts — defines the Tool interface and supporting types
for the tool execution system.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional


class ToolContext(object):
    """Context passed to tool call functions.

    Mirrors src/Tool.ts:ToolUseContext — carries session state,
    permissions, and options needed during tool execution.
    """

    def __init__(self,
                 cwd: Optional[str] = None,
                 tools: Optional[List[Any]] = None,
                 abort_signal: Optional[Any] = None,
                 permission_mode: str = "default",
                 state: Optional[Any] = None,
                 **kwargs):
        self.cwd = cwd
        self.tools = tools or []
        self.abort_signal = abort_signal
        self.permission_mode = permission_mode
        self.state = state
        self.extra = kwargs

    @property
    def is_interactive(self) -> bool:
        return self.permission_mode != "non-interactive"


class ToolResult(object):
    """Result from a tool call."""

    def __init__(self,
                 data: Any,
                 is_error: bool = False,
                 new_messages: Optional[List[Any]] = None):
        self.data = data
        self.is_error = is_error
        self.new_messages = new_messages or []


class Tool(ABC):
    """Abstract base class for all tools.

    Mirrors src/Tool.ts:Tool interface. Each tool must define:
    - name: unique identifier
    - description: for the API prompt
    - input_schema: JSON Schema for parameters
    - call(): the execution function
    """

    # ── Identity ────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name (e.g., 'Bash', 'Read', 'Write')."""
        ...

    @property
    def aliases(self) -> List[str]:
        """Alternative names for backward compatibility."""
        return []

    @property
    def search_hint(self) -> Optional[str]:
        """One-line capability phrase for ToolSearch keyword matching."""
        return None

    # ── Schema ──────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description shown in the API prompt."""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema for the tool's input parameters."""
        ...

    # ── Execution ───────────────────────────────────────────────────────

    @abstractmethod
    async def call(self,
                   args: Dict[str, Any],
                   context: ToolContext) -> ToolResult:
        """Execute the tool with the given arguments."""
        ...

    # ── Capability checks ───────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """Whether this tool is available in the current environment."""
        return True

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        """Whether this invocation only reads (no side effects)."""
        return False

    def is_concurrency_safe(self, args: Dict[str, Any]) -> bool:
        """Whether this tool can run concurrently with others."""
        return False

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        """Whether this invocation performs irreversible operations."""
        return False

    # ── Permissions ─────────────────────────────────────────────────────

    async def check_permissions(self,
                                 args: Dict[str, Any],
                                 context: ToolContext) -> Dict[str, Any]:
        """Check if this tool invocation is allowed.

        Returns a dict with 'behavior' ('allow'/'deny'/'ask') and
        optionally 'updated_input'.
        """
        return {"behavior": "allow", "updated_input": args}

    async def validate_input(self,
                              args: Dict[str, Any],
                              context: ToolContext) -> Dict[str, Any]:
        """Validate input arguments.

        Returns {'result': True} or {'result': False, 'message': str}.
        """
        return {"result": True}

    # ── Indexer notification ──────────────────────────────────────────────

    @staticmethod
    def _notify_indexer(file_path: str, state: Any = None) -> None:
        """通知索引器文件已变更（由 Write/Edit 工具调用）。"""
        try:
            idx = getattr(state, "indexer", None) if state else None
            if idx:
                idx.notify_file_changed(file_path)
        except Exception:
            pass

    # ── API representation ──────────────────────────────────────────────

    def to_api_schema(self) -> Dict[str, Any]:
        """Convert to API provider tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def user_facing_name(self, args: Optional[Dict[str, Any]] = None) -> str:
        """Human-readable name for this tool invocation."""
        return self.name

    def get_activity_description(self,
                                  args: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Human-readable present-tense activity description."""
        return None

    def get_tool_use_summary(self,
                              args: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Short summary for compact display."""
        return None


# ── Tool defaults (mirrors buildTool in Tool.ts) ───────────────────────

def build_tool(name: str,
               description: str,
               input_schema: Dict[str, Any],
               call_fn: Callable,
               **kwargs) -> Dict[str, Any]:
    """Build a tool definition dict with sensible defaults.

    This is a lightweight alternative to the full Tool class for
    simple tools. More complex tools should subclass Tool directly.

    Mirrors src/Tool.ts:buildTool.
    """
    tool: Dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "call_fn": call_fn,
        "is_enabled": kwargs.pop("is_enabled", True),
        "is_read_only": kwargs.pop("is_read_only", False),
        "is_concurrency_safe": kwargs.pop("is_concurrency_safe", False),
        "is_destructive": kwargs.pop("is_destructive", False),
    }
    tool.update(kwargs)
    return tool


def tool_to_api_schema(tool_def: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a tool definition dict to API provider format."""
    return {
        "name": tool_def["name"],
        "description": tool_def.get("description", ""),
        "input_schema": tool_def.get("input_schema", {
            "type": "object",
            "properties": {},
        }),
    }
