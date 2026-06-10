"""
安全工具 — 命令注入防护和安全执行。

"""

import re
import shlex
from typing import List, Optional

# Patterns for dangerous command injection
_FORBIDDEN_PATTERNS = [
    # Command chaining that can bypass tool restrictions
    (r'`[^`]*`', "backtick substitution"),
    (r'\$\([^)]*\)', "command substitution"),
    (r';\s*(rm\s+-rf|shutdown|reboot|mkfs)', "dangerous chained command"),
    (r'\|.*(rm\s+-rf|shutdown|reboot)', "dangerous piped command"),
]

# Commands that require explicit user confirmation
_RISKY_COMMANDS = [
    "rm -rf", "rm -r", "rmdir",
    "shutdown", "reboot", "halt",
    "mkfs", "dd if=", "format",
    ":(){ :|:& };:",  # fork bomb
    "chmod 777", "chmod -R",
    "git push --force", "git reset --hard",
    "> /dev/sda", "> /dev/hda",
]

# Git commands that modify published history (dangerous)
_GIT_DESTRUCTIVE_COMMANDS = [
    "push --force", "push -f",
    "reset --hard",
    "branch -D",
    "rebase -i",
    "commit --amend",
]


def detect_command_injection(command: str) -> Optional[str]:
    """检查命令注入尝试。

    Returns:
        如果检测到注入则返回描述，安全则返回 None。
    """
    for pattern, description in _FORBIDDEN_PATTERNS:
        if re.search(pattern, command):
            return f"检测到潜在的 {description}: {pattern}"
    return None


def is_risky_command(command: str) -> bool:
    """检查命令是否需要用户确认。"""
    cmd_lower = command.lower().strip()

    for risky in _RISKY_COMMANDS:
        if risky.lower() in cmd_lower:
            return True

    # Check git destructive commands
    if cmd_lower.startswith("git "):
        for destructive in _GIT_DESTRUCTIVE_COMMANDS:
            if destructive.lower() in cmd_lower:
                return True

    return False


def sanitize_command(command: str) -> str:
    """从命令中移除已知的危险模式。

    这是一个安全网，而不是完整的解决方案。
    """
    # Remove backtick substitutions
    sanitized = re.sub(r'`[^`]*`', '', command)
    # Remove $() command substitutions
    sanitized = re.sub(r'\$\([^)]*\)', '', sanitized)
    return sanitized.strip()


def validate_command_args(args: List[str]) -> bool:
    """验证命令参数不包含注入。"""
    for arg in args:
        if arg.startswith("-") or arg.startswith("--"):
            continue  # Allow flags
        if "$(" in arg or "`" in arg or ";" in arg:
            return False
    return True


def is_safe_path(path: str) -> bool:
    """检查文件系统路径是否安全（是否在工作目录内）。"""
    import os

    normalized = os.path.normpath(os.path.abspath(path))
    cwd = os.getcwd()

    # Must be within CWD or a standard temp directory
    if normalized.startswith(cwd):
        return True
    if normalized.startswith(os.path.join(os.sep, "tmp")):
        return True
    if normalized.startswith(os.path.join(os.sep, "var", "tmp")):
        return True

    return False


def parse_shell_command(command: str) -> Optional[List[str]]:
    """安全地将 shell 命令字符串解析为参数列表。"""
    try:
        if os.name == "nt":  # Windows
            # On Windows, simple split is safer than shlex for cmd commands
            return command.split()
        return shlex.split(command)
    except ValueError:
        return None


import os as _os  # imported here to avoid name collision
