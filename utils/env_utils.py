"""
环境和平台工具。

对应 src/utils/envUtils.ts — 平台检测、shell 检测、环境变量辅助函数。
"""

import os
import platform
import sys
from typing import Optional


def is_env_truthy(value: Optional[str]) -> bool:
    """检查环境变量是否为真值（'1', 'true', 'yes'）。"""
    if value is None:
        return False
    return value.lower() in ("1", "true", "yes", "on")


def get_platform() -> str:
    """获取当前平台标识符。"""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "win32"
    return system


def get_shell() -> str:
    """从环境变量获取当前 shell。"""
    shell_path = os.environ.get("SHELL", "")
    if not shell_path and sys.platform == "win32":
        comspec = os.environ.get("COMSPEC", "")
        if "cmd" in comspec.lower():
            return "cmd"
        return "powershell"

    if "zsh" in shell_path:
        return "zsh"
    elif "bash" in shell_path:
        return "bash"
    elif "fish" in shell_path:
        return "fish"
    return shell_path or "unknown"


def get_cwd() -> str:
    """获取当前工作目录。"""
    return os.getcwd()


def get_home_dir() -> str:
    """获取用户主目录。"""
    return os.path.expanduser("~")


def get_autorun_config_dir() -> str:
    """获取 AutoRUN 配置目录 (~/.autorun/)。"""
    return os.path.join(get_home_dir(), ".autorun")


def is_windows() -> bool:
    """检查是否在 Windows 上运行。"""
    return sys.platform == "win32"


def is_macos() -> bool:
    """检查是否在 macOS 上运行。"""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """检查是否在 Linux 上运行。"""
    return sys.platform.startswith("linux")
