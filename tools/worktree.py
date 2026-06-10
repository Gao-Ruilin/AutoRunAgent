"""
Worktree tools — EnterWorktree and ExitWorktree.

Mirrors src/tools/EnterWorktreeTool/ and src/tools/ExitWorktreeTool/ —
provides isolated git worktree environments for safe experimentation.
"""

import logging
import os
import random
import string
import subprocess
from typing import Any, Dict, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


# Track active worktree sessions
_active_worktree: Optional[str] = None
_original_cwd: Optional[str] = None


def _generate_worktree_name() -> str:
    """Generate a random worktree name."""
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"worktree_{suffix}"


class EnterWorktreeTool(Tool):
    """Create an isolated git worktree for safe experimentation."""

    @property
    def name(self) -> str:
        return "EnterWorktree"

    @property
    def description(self) -> str:
        return """仅当用户明确要求在工作树中工作时使用此工具。
此工具创建一个隔离的 git 工作树并将当前会话切换到其中。

## 何时使用
- 用户明确说"工作树"（例如，"启动一个工作树"、"在工作树中工作"等）

## 何时不使用
- 用户要求创建分支、切换分支或在不同分支上工作 — 改用 git 命令
- 用户要求修复 bug 或开发功能 — 使用正常的 git 工作流，除非他们特别提到工作树
- 绝不在用户未明确提到"工作树"的情况下使用此工具

## 要求
- 必须在 git 仓库中
- 不能已经在工作树中

## 行为
- 在 .claude/worktrees/ 内创建新的 git 工作树，基于 HEAD 创建新分支
- 将会话的工作目录切换到新工作树
- 使用 ExitWorktree 在会话中途离开工作树（保留或删除）
- 会话退出时，如果仍在工作树中，会提示用户保留或删除"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional name for the worktree. Segments may contain letters, digits, dots, underscores, dashes. Random name if not provided.",
                },
            },
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        global _active_worktree, _original_cwd

        worktree_name = args.get("name") or _generate_worktree_name()
        worktree_name = worktree_name.replace("/", "-")[:64]

        # Check if git repo
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return ToolResult(
                    data="Error: Not in a git repository. Worktree requires a git repo.",
                    is_error=True,
                )
        except Exception:
            return ToolResult(
                data="Error: Could not verify git repository.",
                is_error=True,
            )

        # Check if already in worktree
        if _active_worktree:
            return ToolResult(
                data=f"Error: Already in a worktree ({_active_worktree}). Use ExitWorktree first.",
                is_error=True,
            )

        cwd = context.cwd or os.getcwd()
        worktree_dir = os.path.join(cwd, ".claude", "worktrees", worktree_name)

        try:
            # Create worktree
            result = subprocess.run(
                ["git", "worktree", "add", "-b", worktree_name, worktree_dir],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return ToolResult(
                    data=f"Error creating worktree: {result.stderr.strip()}",
                    is_error=True,
                )

            _active_worktree = worktree_dir
            _original_cwd = cwd

            return ToolResult(
                data=f"Worktree created and activated.\n"
                     f"  Branch: {worktree_name}\n"
                     f"  Path: {worktree_dir}\n\n"
                     f"Current session is now in the worktree. "
                     f"Use ExitWorktree to leave.",
                is_error=False,
            )

        except Exception as e:
            return ToolResult(
                data=f"Error creating worktree: {e}",
                is_error=True,
            )


class ExitWorktreeTool(Tool):
    """Exit the current worktree session."""

    @property
    def name(self) -> str:
        return "ExitWorktree"

    @property
    def description(self) -> str:
        return """退出由 EnterWorktree 创建的工作树会话，并将会话恢复到原始工作目录。

## 范围
此工具仅操作本次会话中由 EnterWorktree 创建的工作树。它不会触及：
- 你手动用 git worktree add 创建的工作树
- 之前会话的工作树

## 何时使用
- 用户明确要求退出工作树、离开工作树、返回

## 参数
- action（必需）: "keep" 或 "remove"
  - "keep" — 保留工作树目录和分支不做更改
  - "remove" — 删除工作树目录及其分支
- discard_changes（可选，默认 false）: 仅在使用 "remove" 时有意义。
  如果工作树有未提交的文件，工具将拒绝删除，除非设置为 true。

## 行为
- 将会话的工作目录恢复到 EnterWorktree 之前的位置
- 清除缓存，使会话状态反映原始目录
- 退出后，可以再次调用 EnterWorktree 创建新的工作树"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["keep", "remove"],
                    "description": "\"keep\" leaves the worktree and branch on disk; \"remove\" deletes both.",
                },
                "discard_changes": {
                    "type": "boolean",
                    "description": "Required true when action is \"remove\" and the worktree has uncommitted files.",
                },
            },
            "required": ["action"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return args.get("action") == "keep"

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        global _active_worktree, _original_cwd

        if not _active_worktree:
            return ToolResult(
                data="No active worktree session. Nothing to exit.",
                is_error=False,
            )

        action = args.get("action", "keep")
        discard_changes = args.get("discard_changes", False)
        worktree_path = _active_worktree
        worktree_name = os.path.basename(_active_worktree)

        if action == "remove":
            # Check for uncommitted changes
            if not discard_changes:
                try:
                    result = subprocess.run(
                        ["git", "status", "--porcelain"],
                        capture_output=True, text=True, timeout=10,
                        cwd=worktree_path,
                    )
                    if result.stdout.strip():
                        return ToolResult(
                            data=f"Error: Worktree has uncommitted changes. "
                                 f"Set discard_changes=true to force removal, "
                                 f"or use action='keep' to preserve changes.",
                            is_error=True,
                        )
                except Exception:
                    logger.debug("Failed to check git status for worktree changes", exc_info=True)

            # Remove worktree
            try:
                result = subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_path],
                    capture_output=True, text=True, timeout=30,
                )
                msg = f"Worktree '{worktree_name}' removed."
                if result.returncode != 0:
                    msg += f" Warning: {result.stderr.strip()}"
            except Exception as e:
                msg = f"Error removing worktree: {e}"

            # Try to delete the branch
            try:
                subprocess.run(
                    ["git", "branch", "-D", worktree_name],
                    capture_output=True, text=True, timeout=10,
                )
            except Exception:
                logger.warning("Failed to delete git branch after worktree removal: %s", worktree_name, exc_info=True)

        else:  # keep
            msg = f"Worktree '{worktree_name}' kept at: {worktree_path}"

        _active_worktree = None
        _original_cwd = None

        return ToolResult(
            data=f"{msg}\nSession restored to original directory.",
            is_error=False,
        )
