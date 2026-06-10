"""
API 调用的上下文构建。
"""

import asyncio
import logging
import os
import subprocess
from datetime import date
from functools import lru_cache
from typing import Any, Dict, List, Optional

from AutoRUN_v1.utils.env_utils import get_platform

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _cached_get_git() -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        logger.debug("git rev-parse failed", exc_info=True)
        return False


def _get_git_branch() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        logger.debug("git branch failed", exc_info=True)
    return None


def _get_git_default_branch() -> Optional[str]:
    try:
        for branch in ["origin/main", "origin/master"]:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return branch.replace("origin/", "")
        return "main"
    except Exception:
        logger.debug("git default branch detection failed", exc_info=True)
        return "main"


def _get_git_status() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "status", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            status = result.stdout.strip()
            if len(status) > 2000:
                status = (
                    status[:2000]
                    + '\n... (truncated because it exceeds 2k characters. If you need more information, run "git status" using BashTool)'
                )
            return status or "(clean)"
    except Exception:
        logger.debug("git status failed", exc_info=True)
    return None


def _get_git_log() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "log", "--oneline", "-n", "5"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        logger.debug("git log failed", exc_info=True)
    return None


def _get_git_user() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        logger.debug("git config user.name failed", exc_info=True)
    return None


async def get_git_status_full() -> Optional[str]:
    if os.environ.get("NODE_ENV") == "test":
        return None
    is_git = _cached_get_git()
    if not is_git:
        return None
    loop = asyncio.get_event_loop()
    branch, main_branch, status, log, user_name = await asyncio.gather(
        loop.run_in_executor(None, _get_git_branch),
        loop.run_in_executor(None, _get_git_default_branch),
        loop.run_in_executor(None, _get_git_status),
        loop.run_in_executor(None, _get_git_log),
        loop.run_in_executor(None, _get_git_user),
    )
    lines = [
        "This is the git status at the start of the conversation. Note this is a snapshot and will not update during the conversation.",
        f"Current branch: {branch or 'unknown'}",
        f"Default branch (usually for PRs): {main_branch or 'main'}",
    ]
    if user_name:
        lines.append(f"Git user: {user_name}")
    lines.append(f"Status:\n{status or '(unknown)'}")
    lines.append(f"Recent commits:\n{log or '(none)'}")
    return "\n\n".join(lines)


async def get_user_context(autorun_md_content: Optional[str] = None) -> Dict[str, str]:
    context: Dict[str, str] = {
        "currentDate": f"Today is {date.today().isoformat()}.",
    }
    if autorun_md_content:
        context["autorunMd"] = autorun_md_content
    return context


async def get_system_context() -> Dict[str, str]:
    context: Dict[str, str] = {}
    git_status = await get_git_status_full()
    if git_status:
        context["gitStatus"] = git_status
    return context


def build_context_text(
    user_context: Dict[str, str],
    system_context: Optional[Dict[str, str]] = None,
    available_skills: Optional[List[str]] = None,
) -> str:
    """将 user/system context 字典序列化为 XML 标签文本。

    所有调用方统一使用此函数，避免 XML 拼接逻辑重复。
    """
    parts = []
    for key, value in sorted(user_context.items()):
        parts.append(f"<{key}>\n{value}\n</{key}>")
    if system_context:
        for key, value in sorted(system_context.items()):
            parts.append(f"<{key}>\n{value}\n</{key}>")
    if available_skills:
        parts.append(
            "<available_skills>\n" + "\n".join(available_skills) + "\n</available_skills>"
        )
    return "\n".join(parts)


def build_env_info(model: str, additional_working_dirs: Optional[list] = None) -> str:
    import platform as plat
    import os as _os
    is_git = _cached_get_git()
    cwd = os.getcwd()
    model_description = f"You are using model: {model}."
    shell = _os.environ.get("SHELL", "unknown")
    shell_name = "zsh" if "zsh" in shell else ("bash" if "bash" in shell else shell)
    platform_name = get_platform()
    if platform_name == "win32":
        shell_line = f"Shell: {shell_name} (use Unix shell syntax, not Windows -- e.g. /dev/null not NUL, forward slashes)"
    else:
        shell_line = f"Shell: {shell_name}"
    items = [
        f"Working directory: {cwd}",
        f"Is git repository: {'yes' if is_git else 'no'}",
    ]
    if additional_working_dirs:
        items.append("Additional working directories:")
        for d in additional_working_dirs:
            items.append(f"  - {d}")
    items.extend([
        f"Platform: {platform_name}",
        shell_line,
        f"OS version: {plat.system()} {plat.release()}",
        model_description,
        "AutoRUN is available as a CLI and Web application.",
    ])
    return "# Environment\nThe environment: \n" + "\n".join(f" - {item}" for item in items)
