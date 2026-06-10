"""
BashTool — Execute shell commands in a sandboxed environment.

Non-blocking execution: commands get a default 10s window. If they finish
within that time, result is returned immediately. If they exceed it, partial
output is captured (the process is NOT killed) and returned together with the
PID so the AI can decide the next step: check again, wait longer, or kill.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


MAX_STREAM_SIZE = 300 * 1024
DEFAULT_TIMEOUT_MS = 10000  # 10 seconds

# ── Running process registry (shared across tool instances) ──────────────
_running: Dict[int, dict] = {}


def _cleanup_finished():
    """Remove finished processes from registry."""
    finished = [pid for pid, info in _running.items()
                if info["process"].returncode is not None]
    for pid in finished:
        _running.pop(pid, None)


class BashTool(Tool):
    """Non-blocking bash/shell command executor."""

    @property
    def name(self) -> str:
        return "Bash"

    @property
    def description(self) -> str:
        return """执行给定的 bash 命令并返回其输出。

工作目录在命令之间保持，但 shell 状态不保持。shell 环境从用户配置文件初始化（bash 或 zsh）。

## 非阻塞执行模型
- 默认超时 **10 秒**。10 秒内完成的命令直接返回完整输出。
- 超出 10 秒的命令**不会被杀死**——返回已产生的部分输出 + PID，AI 可决定下一步。
- 对于长时间任务（训练脚本、大型编译等）：设置 `timeout: 0` 可无限等待，结合 `poll_interval` 周期性获取进度。
- 使用 `check_pid` 查看正在运行的进程的最新输出。
- 使用 `kill_pid` 终止正在运行的进程。

## 当前 shell 环境
- 平台: 当前操作系统平台
- Shell 二进制: 检测到的 shell

### 平台特定规则（重要）
- 在 Windows 上使用 Git Bash 时，避免使用 PowerShell/cmd 语法。
- 在所有平台上使用 POSIX 路径。
- stderr 抑制: 2>/dev/null（不是 2>nul）

## 指令
- 如果命令将创建新目录或文件，首先验证父目录是否存在。
- 始终用引号括起包含空格的文件路径。
- 尽量在整个会话期间保持当前工作目录。
- 可以指定可选的超时时间（毫秒，最多 600000ms / 10 分钟）。设为 0 表示无超时。
- 使用 run_in_background 来运行长时间命令（立即返回 PID）。
- 对于需要监控进度的长时间命令，设置 poll_interval（毫秒）来周期性获取输出。
- 发出多个独立命令时，并行调用。
- 对于互相依赖的命令，使用 '&&' 链接它们。
- 仅当需要顺序运行命令但不关心前面的命令是否失败时，使用 ';'。
- 不要使用换行符分隔命令。

## Git 安全协议
- 绝不更新 git config
- 除非明确要求，绝不运行破坏性 git 命令
- 除非明确要求，绝不跳过 hooks（--no-verify, --no-gpg-sign）
- 绝不 force push 到 main/master
- 优先创建新提交而不是修订（amend）
- 重要: 绝不使用带 -i 标志的 git 命令，因为它们需要交互式输入"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute",
                },
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in ms (default 10000 = 10s, 0 = no limit)",
                    "minimum": 0,
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "For long-running commands (e.g. training): set this instead of timeout. Returns partial output every N ms. AI checks progress and decides to continue or stop via check_pid/kill_pid. Combined with timeout=0 for indefinite run.",
                    "minimum": 300000,
                    "maximum": 86400000,
                },
                "check_pid": {
                    "type": "integer",
                    "description": "Check output of a previously started process by PID. Returns latest output without killing.",
                },
                "kill_pid": {
                    "type": "integer",
                    "description": "Kill a previously started process by PID. Returns final output.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true to run this command in the background (return PID immediately)",
                },
                "dangerouslyDisableSandbox": {
                    "type": "boolean",
                    "description": "Set to true to override sandbox mode",
                },
            },
            "required": ["command"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        readonly_prefixes = ("ls ", "cat ", "head ", "tail ", "find ", "grep ",
                           "echo ", "pwd", "which ", "whoami", "date", "uname",
                           "wc ", "sort ", "uniq ", "cut ", "tr ")
        cmd = args.get("command", "").strip()
        if cmd.startswith(readonly_prefixes):
            return True
        # check_pid and kill_pid are read-only relative to the command
        if args.get("check_pid") or args.get("kill_pid"):
            return True
        return False

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        destructive_patterns = [
            "rm ", "rmdir", "git reset --hard", "git push --force",
            "git branch -D", "git stash drop", "> /dev/", "dd if=",
            "mkfs.", "shutdown", "reboot", ":(){ :|:& };:",
        ]
        cmd = args.get("command", "").strip()
        return any(p in cmd for p in destructive_patterns)

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        _cleanup_finished()

        # ── check_pid ──
        check_pid = args.get("check_pid")
        if check_pid:
            return await self._check_pid(check_pid)

        # ── kill_pid ──
        kill_pid = args.get("kill_pid")
        if kill_pid:
            return await self._kill_pid(kill_pid)

        command = args.get("command", "").strip()
        if not command:
            return ToolResult(data="", is_error=False)

        timeout_ms = args.get("timeout", DEFAULT_TIMEOUT_MS)
        poll_interval_ms = args.get("poll_interval")
        run_in_background = args.get("run_in_background", False)
        description = args.get("description", "Running command")
        cwd = context.cwd or os.getcwd()

        try:
            if run_in_background:
                return await self._run_background(command, description, cwd)
            else:
                return await self._run_foreground(
                    command, description, cwd,
                    timeout_ms / 1000.0,
                    poll_interval_ms / 1000.0 if poll_interval_ms else None,
                )
        except Exception as e:
            return ToolResult(data=f"Command execution error: {e}", is_error=True)

    # ── foreground runner (non-blocking on timeout) ─────────────────────

    async def _run_foreground(self, command: str, description: str,
                              cwd: str, timeout_s: float,
                              poll_interval_s: Optional[float]) -> ToolResult:
        """Run a command with incremental output capture.

        On timeout: return partial output + PID, keep process alive.
        On completion: return full output.
        """
        try:
            kwargs = dict(
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = await asyncio.create_subprocess_shell(command, **kwargs)
        except Exception as e:
            return ToolResult(data=f"Failed to start process: {e}", is_error=True)

        pid = process.pid
        start_time = time.time()
        accumulated_stdout = ""
        accumulated_stderr = ""

        async def _read_stream(stream, label):
            nonlocal accumulated_stdout, accumulated_stderr
            while True:
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    if label == "stdout":
                        accumulated_stdout += decoded
                    else:
                        accumulated_stderr += decoded
                except (ValueError, asyncio.CancelledError):
                    break

        # Start readers
        loop = asyncio.get_running_loop()
        stdout_task = loop.create_task(_read_stream(process.stdout, "stdout"))
        stderr_task = loop.create_task(_read_stream(process.stderr, "stderr"))

        effective_timeout = None
        if poll_interval_s is not None:
            effective_timeout = poll_interval_s
        elif timeout_s > 0:
            effective_timeout = timeout_s
        poll_remaining = poll_interval_s

        try:
            if effective_timeout is not None:
                await asyncio.wait_for(process.wait(), timeout=effective_timeout)
                # Completed within the window
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                output = self._format_output(accumulated_stdout, accumulated_stderr)
                is_error = process.returncode != 0
                _running.pop(pid, None)
                return ToolResult(data=output, is_error=is_error)
            else:
                # No effective timeout — wait indefinitely
                await process.wait()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                output = self._format_output(accumulated_stdout, accumulated_stderr)
                is_error = process.returncode != 0
                _running.pop(pid, None)
                return ToolResult(data=output, is_error=is_error)

        except asyncio.TimeoutError:
            # Timeout reached — don't kill, capture partial output
            elapsed = time.time() - start_time

            # Cancel readers to flush pending data
            stdout_task.cancel()
            stderr_task.cancel()
            try:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            except Exception:
                pass

            # Also try to read any remaining buffered data
            try:
                remaining_stdout = await asyncio.wait_for(
                    process.stdout.read(65536), timeout=0.5)
                if remaining_stdout:
                    accumulated_stdout += remaining_stdout.decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                remaining_stderr = await asyncio.wait_for(
                    process.stderr.read(65536), timeout=0.5)
                if remaining_stderr:
                    accumulated_stderr += remaining_stderr.decode("utf-8", errors="replace")
            except Exception:
                pass

            # Store for later checking
            _running[pid] = {
                "process": process,
                "start_time": start_time,
                "command": command,
                "description": description,
            }

            output = self._format_output(accumulated_stdout, accumulated_stderr)
            next_step = (
                f"\n\n---\n"
                f"[PROCESS STILL RUNING — PID: {pid}, elapsed: {elapsed:.1f}s]\n"
                f"Command: {command}\n"
                f"The process was not killed. You can:\n"
                f"  - Check progress: Bash(check_pid={pid})\n"
                f"  - Kill it: Bash(kill_pid={pid})\n"
                f"  - Wait and check later: Bash(check_pid={pid}, timeout=30000)\n"
            )
            return ToolResult(data=output + next_step, is_error=False)

    # ── check running process ───────────────────────────────────────────

    async def _check_pid(self, pid: int) -> ToolResult:
        """Read latest output from a running process."""
        info = _running.get(pid)
        if not info:
            return ToolResult(
                data=f"No running process with PID {pid}. It may have finished or was never tracked.",
                is_error=True,
            )

        process = info["process"]
        if process.returncode is not None:
            elapsed = time.time() - info["start_time"]
            _running.pop(pid, None)
            return ToolResult(
                data=f"Process {pid} has already finished (exit code: {process.returncode}, elapsed: {elapsed:.1f}s).",
                is_error=False,
            )

        # Read available output without blocking
        accumulated = ""
        try:
            # Try reading stdout
            chunk = await asyncio.wait_for(process.stdout.read(65536), timeout=2.0)
            if chunk:
                accumulated += chunk.decode("utf-8", errors="replace")
        except (asyncio.TimeoutError, Exception):
            pass

        try:
            chunk = await asyncio.wait_for(process.stderr.read(65536), timeout=2.0)
            if chunk:
                accumulated += "\n[stderr]\n" + chunk.decode("utf-8", errors="replace")
        except (asyncio.TimeoutError, Exception):
            pass

        elapsed = time.time() - info["start_time"]
        header = f"[PID {pid}, running {elapsed:.1f}s — {info['command'][:80]}]\n"
        if accumulated.strip():
            return ToolResult(data=header + accumulated, is_error=False)
        return ToolResult(data=header + "(no new output since last check)", is_error=False)

    # ── kill running process ────────────────────────────────────────────

    async def _kill_pid(self, pid: int) -> ToolResult:
        """Kill a running process and return whatever output it produced."""
        info = _running.pop(pid, None)
        if not info:
            return ToolResult(
                data=f"No tracked process with PID {pid}.",
                is_error=True,
            )

        process = info["process"]
        if process.returncode is not None:
            elapsed = time.time() - info["start_time"]
            return ToolResult(
                data=f"Process {pid} already finished (exit code: {process.returncode}, elapsed: {elapsed:.1f}s).",
                is_error=False,
            )

        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, OSError):
            pass

        # Wait briefly for graceful exit, then force kill
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                pass

        elapsed = time.time() - info["start_time"]
        return ToolResult(
            data=f"Process {pid} killed after {elapsed:.1f}s.\nCommand: {info['command']}",
            is_error=False,
        )

    # ── background runner ───────────────────────────────────────────────

    async def _run_background(self, command: str, description: str,
                              cwd: str) -> ToolResult:
        """Run a command in the background, returning immediately."""
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=None if sys.platform == "win32" else os.setsid,
            )
            return ToolResult(
                data=f"Command started in background (PID: {process.pid}): {description}",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(data=f"Failed to start background command: {e}", is_error=True)

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _format_output(stdout: str, stderr: str) -> str:
        stdout = BashTool._truncate(stdout, MAX_STREAM_SIZE)
        stderr = BashTool._truncate(stderr, MAX_STREAM_SIZE)
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        return "\n".join(parts) if parts else "(no output)"

    @staticmethod
    def _truncate(text: str, max_size: int) -> str:
        if len(text) <= max_size:
            return text
        return (
            text[:max_size]
            + f"\n... [truncated at {max_size} bytes, total was {len(text)} bytes]"
        )

    @staticmethod
    def _kill_process(process):
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
