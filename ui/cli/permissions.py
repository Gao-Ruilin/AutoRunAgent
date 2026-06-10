"""
权限交互 — 工具执行前的用户确认（prompt_toolkit Application 兼容）。

参照 src/components/permissions/ — 在全屏 Application 中使用
run_in_executor + input() 获取 y/n 确认。
"""

import asyncio
from typing import Any, Dict, List, Optional

# 高风险命令清单
RISKY_COMMANDS = [
    "rm -rf", "rm -r",
    "git push --force", "git push -f",
    "git reset --hard", "git branch -D",
    "shutdown", "reboot", "halt",
    "mkfs", "dd if=",
    "chmod 777", "chmod -R 777",
    "> /dev/sda", "> /dev/hda",
    "dropdb", "DROP TABLE", "DROP DATABASE",
    "format", "diskpart",
]

ALWAYS_ALLOWED = frozenset({
    "FileReadTool", "FileRead", "Read",
    "GlobTool", "Glob",
    "GrepTool", "Grep",
    "WebSearchTool", "WebSearch",
    "WebFetchTool", "WebFetch",
    "TaskList", "TaskGet",
    "ToolSearch",
    "Skill", "ListSkills",
})


class PermissionHandler:
    """通过 executor 线程进行工具权限确认。

    使用 run_in_executor + input() — 查询协程被挂起时
    Application 事件循环继续运行，终端渲染保持响应。
    """

    def __init__(self):
        self._app = None

    async def ask_yes_no(self, prompt: str, default: Optional[bool] = None) -> bool:
        if default is True:
            hint = " [Y/n]"
        elif default is False:
            hint = " [y/N]"
        else:
            hint = " [y/n]"

        full_prompt = f"\n{'─' * 50}\n{prompt}{hint}: "

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None, lambda: input(full_prompt)
            )
        except (EOFError, KeyboardInterrupt):
            return False

        response = (response or "").strip().lower()
        if not response:
            if default is not None:
                return default
            return False
        return response in ("y", "yes", "是", "允许")

    async def prompt_tool_permission(self, tool_name: str,
                                      tool_input: Dict[str, Any],
                                      is_sensitive: bool = False) -> bool:
        lines = [f"\n{'─' * 50}",
                  f"工具权限请求: {tool_name}"]
        for key, val in tool_input.items():
            val_str = str(val)
            if len(val_str) > 80:
                val_str = val_str[:80] + "..."
            marker = " [敏感]" if (is_sensitive or key in (
                "command", "cmd", "code", "script")) else ""
            lines.append(f"  {key}: {val_str}{marker}")
        lines.append(f"{'─' * 50}")

        full_text = "\n".join(lines)

        if is_sensitive:
            return await self.ask_yes_no(
                f"{full_text}\n此操作可能具有危险性。是否允许执行？",
                default=False,
            )
        return await self.ask_yes_no(
            f"{full_text}\n是否允许执行此工具？",
            default=True,
        )

    @staticmethod
    def check_sensitive_command(tool_input: Dict[str, Any]) -> bool:
        for key in ("command", "cmd", "code", "script", "args"):
            value = str(tool_input.get(key, "")).lower()
            for risky in RISKY_COMMANDS:
                if risky.lower() in value:
                    return True
        for key in ("file_path", "path", "notebook_path"):
            value = str(tool_input.get(key, "")).lower()
            if any(d in value for d in (
                "/etc/shadow", "/etc/passwd", ".env", "credentials", "id_rsa")):
                return True
        return False

    @staticmethod
    def is_tool_always_allowed(tool_name: str) -> bool:
        return tool_name in ALWAYS_ALLOWED

    @staticmethod
    def is_tool_destructive(tool_name: str) -> bool:
        destructive = frozenset({
            "BashTool", "Bash",
            "FileWriteTool", "FileWrite",
            "FileEditTool", "FileEdit", "Edit",
            "NotebookEditTool", "NotebookEdit",
            "PowerShellTool", "PowerShell",
            "EnterWorktree", "ExitWorktree",
        })
        return tool_name in destructive


_permission_handler: Optional[PermissionHandler] = None


def get_permission_handler() -> PermissionHandler:
    global _permission_handler
    if _permission_handler is None:
        _permission_handler = PermissionHandler()
    return _permission_handler
