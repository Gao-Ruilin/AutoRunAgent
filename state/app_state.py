"""
应用状态管理。

对应 src/state/AppState.tsx 和 src/bootstrap/state.ts — 提供
用于整个 REPL 的会话级全局状态的中央数据类。
"""

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from AutoRUN_v1.messages.types import Message

# 模块级共享的 disabled skills 存储（跨 get_app_state() 调用存活）
_shared_disabled_by_cwd: Dict[str, Set[str]] = {}
_shared_skills_lock = threading.RLock()


@dataclass
class AppState:
    """AutoRUN 会话的中央应用状态。

    跟踪消息、工具、权限和会话元数据。
    通过可重入锁实现线程安全。
    """

    # 会话标识
    session_id: str = ""
    cwd: str = field(default_factory=os.getcwd)
    project_root: Optional[str] = None
    
    # 多Agent 委托模式
    agent_pref: bool = True  # 默认启用多Agent委托

    # 消息
    messages: List[Message] = field(default_factory=list)

    # 工具
    enabled_tools: Set[str] = field(default_factory=set)
    tool_definitions: List[Dict[str, Any]] = field(default_factory=list)

    # 权限
    permission_mode: str = "bypass"  # default | accept_edits | bypass | plan
    allowed_tools: Set[str] = field(default_factory=set)
    deny_permissions: Set[str] = field(default_factory=set)

    # 计划模式
    plan_mode_active: bool = False
    plan_content: str = ""

    # 工作树
    worktree_active: bool = False
    worktree_path: Optional[str] = None
    original_cwd: Optional[str] = None

    # Model
    model: str = ""

    # Tasks (TaskCreate/TaskUpdate tool state)
    tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    task_counter: int = 0

    # Todos (TodoWrite tool state, legacy)
    todos: List[Dict[str, Any]] = field(default_factory=list)

    # Skills (per-project disabled state)
    disabled_skills: Set[str] = field(default_factory=set)
    _disabled_skills_by_cwd: Dict[str, Set[str]] = field(default_factory=dict, repr=False)

    def _get_disabled_skills(self) -> Set[str]:
        """Get disabled skills for current cwd from shared store.

        首次访问某目录时，自动禁用所有非内置 skill（用户/project skill），
        仅内置 skill 默认启用。用户可通过 WebUI 手动启用。
        """
        cwd = os.getcwd()
        with _shared_skills_lock:
            if cwd not in _shared_disabled_by_cwd:
                # 新目录首次访问：自动禁用非内置 skill
                try:
                    from AutoRUN_v1.skills.loader import discover_skills, clear_skills_cache
                    clear_skills_cache()
                    all_skills = discover_skills(refresh=True)
                    disabled = set()
                    for skill_name, skill_def in all_skills.items():
                        if skill_def.get("_source") != "bundled":
                            disabled.add(skill_name)
                    _shared_disabled_by_cwd[cwd] = disabled
                except Exception:
                    _shared_disabled_by_cwd[cwd] = set()
            return _shared_disabled_by_cwd[cwd]

    # File indexer
    indexer: Optional[Any] = None  # FileIndexer instance

    # Session metadata
    session_created_at: Optional[str] = None

    # MCP (simplified)
    mcp_connections: Dict[str, Any] = field(default_factory=dict)

    # User context
    user_context: Dict[str, str] = field(default_factory=dict)
    system_context: Dict[str, str] = field(default_factory=dict)

    # Lock
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def add_message(self, msg: Message) -> None:
        """Thread-safe message append."""
        with self._lock:
            self.messages.append(msg)

    def get_messages(self) -> List[Message]:
        """Thread-safe message read."""
        with self._lock:
            return list(self.messages)

    def clear_messages(self) -> None:
        """Thread-safe message clear. Saves conversation first."""
        # 清空前自动保存
        from AutoRUN_v1.services.conversations import save_conversation
        if self.messages:
            save_conversation(self)
        with self._lock:
            self.messages.clear()

    def set_messages(self, messages: List[Message]) -> None:
        """Thread-safe message replacement (used by compaction)."""
        with self._lock:
            self.messages = list(messages)

    def get_recent_messages(self, n: int = 20) -> List[Message]:
        """Get the most recent n messages."""
        with self._lock:
            return self.messages[-n:]

    def enable_tool(self, tool_name: str) -> None:
        """Enable a tool."""
        with self._lock:
            self.enabled_tools.add(tool_name)

    def disable_tool(self, tool_name: str) -> None:
        """Disable a tool."""
        with self._lock:
            self.enabled_tools.discard(tool_name)

    def is_tool_enabled(self, tool_name: str) -> bool:
        """Check if a tool is enabled."""
        with self._lock:
            return tool_name in self.enabled_tools

    def can_use_tool(self, tool_name: str, _args: Dict[str, Any] = None) -> bool:
        """Check if a tool can be used (permission-aware)."""
        with self._lock:
            if self.permission_mode == "bypass":
                return True
            if tool_name in self.deny_permissions:
                return False
            if self.allowed_tools:
                return tool_name in self.allowed_tools
            return self.is_tool_enabled(tool_name)

    def disable_skill(self, name: str) -> None:
        """Disable a skill by name (per-cwd, shared across all sessions)."""
        with _shared_skills_lock:
            ds = self._get_disabled_skills()
            ds.add(name)

    def enable_skill(self, name: str) -> None:
        """Enable a skill by name (per-cwd, shared across all sessions)."""
        with _shared_skills_lock:
            ds = self._get_disabled_skills()
            ds.discard(name)

    def is_skill_disabled(self, name: str) -> bool:
        """Check if a skill is disabled (per-cwd, shared across all sessions)."""
        with _shared_skills_lock:
            return name in self._get_disabled_skills()


def get_app_state() -> AppState:
    """Create a new AppState instance.

    Each call returns a fresh instance — no module-level singleton.
    Callers are responsible for holding the reference for their session.
    CLI creates one instance per REPL session. Web server creates one
    per WebSocket connection via _ws_states.
    """
    return AppState()
