"""
PowerShellTool — Execute PowerShell/cmd/pwsh commands on Windows.

Non-blocking execution: commands get a default 10s window. If they finish
within that time, result is returned immediately. If they exceed it, partial
output is captured (the process is NOT killed) and returned together with the
PID so the AI can decide the next step.
"""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


MAX_STREAM_SIZE = 300 * 1024
DEFAULT_TIMEOUT_MS = 10000

# ── Running process registry ─────────────────────────────────────────────
_running: Dict[int, dict] = {}


def _cleanup_finished():
    finished = [pid for pid, info in _running.items()
                if info["process"].returncode is not None]
    for pid in finished:
        _running.pop(pid, None)


def _detect_windows_shell() -> str:
    for shell in ["pwsh", "powershell"]:
        if shutil.which(shell) or shutil.which(shell + ".exe"):
            return shell
    windir = os.environ.get("WINDIR", "C:\\Windows")
    pwsh_paths = [
        os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "PowerShell", "7", "pwsh.exe"),
    ]
    for path in pwsh_paths:
        if os.path.exists(path):
            return "pwsh" if "7" in path else "powershell"
    return "cmd"


class PowerShellTool(Tool):
    """Non-blocking PowerShell/cmd/pwsh command executor."""

    @property
    def name(self) -> str:
        return "PowerShell"

    @property
    def description(self) -> str:
        return """Executes a given PowerShell, pwsh, or cmd command and returns its output.

The working directory persists between commands, but shell state does not. The shell is auto-detected (pwsh preferred, then powershell, then cmd).

## Non-blocking execution model
- Default timeout: **10 seconds**. Commands finishing within 10s return immediately.
- Commands exceeding 10s are **NOT killed** — partial output + PID is returned so AI can decide: check again, wait, or kill.
- For long tasks (training, builds): set `timeout: 0` for unlimited wait, or use `poll_interval` for periodic progress.
- Use `check_pid` to view latest output of a running process.
- Use `kill_pid` to terminate a running process.

## Current shell environment
- Platform: Windows
- Shell binary: Auto-detected (pwsh, powershell, or cmd)

### Platform-specific rules (CRITICAL)
- Uses PowerShell/pwsh/cmd natively — NOT Git Bash, NOT WSL
- stderr redirection: 2>$null (PowerShell) or 2>nul (cmd)
- pipe: use | (PowerShell pipeline or cmd pipe)
- DO NOT use Unix commands (ls, cat, grep, etc.)

### PowerShell-specific
- Cmdlets: Get-ChildItem, Select-Object, Where-Object, Set-Location, Get-Content, etc.
- Variables: $env:VAR (not $VAR)

### cmd-specific
- Builtins: dir, type, copy, move, del, mkdir, rmdir, echo, cls
- Variables: %VAR%

## Instructions
- If your command will create new directories or files, first verify the parent directory exists.
- Always quote file paths that contain spaces.
- Try to maintain the current working directory throughout the session.
- Specify timeout in ms (default 10000, 0 = no limit, max 600000).
- Use run_in_background for truly background tasks.
- Use poll_interval (ms) for periodic progress on long commands.
- Use '&&' to chain commands (cmd) or ';' (PowerShell).
- DO NOT use newlines to separate commands.
- Specify 'shell' parameter to force a specific shell.

## Git Safety Protocol
- NEVER update the git config
- NEVER run destructive git commands unless explicitly requested
- NEVER skip hooks (--no-verify, --no-gpg-sign) unless explicitly requested
- NEVER run force push to main/master
- Prefer creating NEW commits rather than amending
- IMPORTANT: Never use git commands with the -i flag"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The PowerShell/cmd command to execute",
                },
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does",
                },
                "shell": {
                    "type": "string",
                    "enum": ["pwsh", "powershell", "cmd"],
                    "description": "Force a specific shell (default: auto-detect)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in ms (default 10000, 0 = no limit)",
                    "minimum": 0,
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "For long-running commands (e.g. training): set this instead of timeout. Returns partial output every N ms. AI checks progress and decides to continue or stop via check_pid/kill_pid.",
                    "minimum": 300000,
                    "maximum": 86400000,
                },
                "check_pid": {
                    "type": "integer",
                    "description": "Check output of a previously started process by PID",
                },
                "kill_pid": {
                    "type": "integer",
                    "description": "Kill a previously started process by PID",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run command in background (return PID immediately)",
                },
                "dangerouslyDisableSandbox": {
                    "type": "boolean",
                    "description": "Set to true to override sandbox mode",
                },
            },
            "required": ["command"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        if args.get("check_pid") or args.get("kill_pid"):
            return True
        readonly_pwsh = (
            "Get-ChildItem ", "Get-Content ", "Get-Location", "Get-Command ",
            "Select-String ", "Select-Object ", "Where-Object ",
            "Get-Process ", "Get-Service ", "Get-Item ", "Get-Date",
            "Measure-Object ", "Compare-Object ", "Format-",
            "Write-Output ", "Write-Host ", "echo ",
            "ls ", "dir ", "type ", "pwd", "cat ",
        )
        cmd = args.get("command", "").strip()
        shell = args.get("shell")
        if shell == "cmd":
            cmd_readonly = ("dir ", "type ", "echo ", "cd ", "ver", "date ",
                          "time ", "whoami", "where ", "findstr ")
            return cmd.lower().startswith(cmd_readonly)
        return cmd.startswith(readonly_pwsh)

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        destructive_pwsh = [
            "Remove-Item", "rm ", "del ", "rmdir",
            "Clear-Content", "Clear-RecycleBin",
            "Stop-Process", "Stop-Service",
            "Disable-", "Unregister-", "Set-ExecutionPolicy",
        ]
        destructive_git = [
            "git reset --hard", "git push --force", "git branch -D",
            "git stash drop",
        ]
        cmd = args.get("command", "").strip()
        lower = cmd.lower()
        return any(p.lower() in lower for p in destructive_pwsh + destructive_git)

    def is_enabled(self) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        _cleanup_finished()

        check_pid = args.get("check_pid")
        if check_pid:
            return await self._check_pid(check_pid)

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
        shell = args.get("shell")
        cwd = context.cwd or os.getcwd()

        try:
            if run_in_background:
                return await self._run_background(command, description, cwd, shell)
            else:
                return await self._run_foreground(
                    command, description, cwd,
                    timeout_ms / 1000.0,
                    poll_interval_ms / 1000.0 if poll_interval_ms else None,
                    shell,
                )
        except Exception as e:
            return ToolResult(data=f"Command execution error: {e}", is_error=True)

    # ── foreground runner ──────────────────────────────────────────────

    async def _run_foreground(self, command: str, description: str,
                              cwd: str, timeout_s: float,
                              poll_interval_s: Optional[float],
                              shell: Optional[str] = None) -> ToolResult:
        cmdline = self._build_cmdline(command, shell)
        try:
            process = await asyncio.create_subprocess_exec(
                *cmdline,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
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
                    decoded = self._decode(line)
                    if label == "stdout":
                        accumulated_stdout += decoded
                    else:
                        accumulated_stderr += decoded
                except (ValueError, asyncio.CancelledError):
                    break

        stdout_task = asyncio.get_running_loop().create_task(_read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.get_running_loop().create_task(_read_stream(process.stderr, "stderr"))

        effective_timeout = None
        if poll_interval_s is not None:
            effective_timeout = poll_interval_s
        elif timeout_s > 0:
            effective_timeout = timeout_s

        try:
            if effective_timeout is not None:
                await asyncio.wait_for(process.wait(), timeout=effective_timeout)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                output = self._format_output(accumulated_stdout, accumulated_stderr)
                is_error = process.returncode != 0
                _running.pop(pid, None)
                return ToolResult(data=output, is_error=is_error)
            else:
                await process.wait()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                output = self._format_output(accumulated_stdout, accumulated_stderr)
                is_error = process.returncode != 0
                _running.pop(pid, None)
                return ToolResult(data=output, is_error=is_error)

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            stdout_task.cancel()
            stderr_task.cancel()
            try:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            except Exception:
                pass

            # Read remaining buffer
            try:
                r = await asyncio.wait_for(process.stdout.read(65536), timeout=0.5)
                if r:
                    accumulated_stdout += self._decode(r)
            except Exception:
                pass
            try:
                r = await asyncio.wait_for(process.stderr.read(65536), timeout=0.5)
                if r:
                    accumulated_stderr += self._decode(r)
            except Exception:
                pass

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
                f"  - Check progress: PowerShell(check_pid={pid})\n"
                f"  - Kill it: PowerShell(kill_pid={pid})\n"
                f"  - Wait and check later: PowerShell(check_pid={pid}, timeout=30000)\n"
            )
            return ToolResult(data=output + next_step, is_error=False)

    # ── check / kill ────────────────────────────────────────────────────

    async def _check_pid(self, pid: int) -> ToolResult:
        info = _running.get(pid)
        if not info:
            return ToolResult(
                data=f"No running process with PID {pid}.",
                is_error=True,
            )
        process = info["process"]
        if process.returncode is not None:
            elapsed = time.time() - info["start_time"]
            _running.pop(pid, None)
            return ToolResult(
                data=f"Process {pid} finished (exit: {process.returncode}, elapsed: {elapsed:.1f}s).",
                is_error=False,
            )

        accumulated = ""
        try:
            chunk = await asyncio.wait_for(process.stdout.read(65536), timeout=2.0)
            if chunk:
                accumulated += self._decode(chunk)
        except (asyncio.TimeoutError, Exception):
            pass
        try:
            chunk = await asyncio.wait_for(process.stderr.read(65536), timeout=2.0)
            if chunk:
                accumulated += "\n[stderr]\n" + self._decode(chunk)
        except (asyncio.TimeoutError, Exception):
            pass

        elapsed = time.time() - info["start_time"]
        header = f"[PID {pid}, running {elapsed:.1f}s — {info['command'][:80]}]\n"
        if accumulated.strip():
            return ToolResult(data=header + accumulated, is_error=False)
        return ToolResult(data=header + "(no new output since last check)", is_error=False)

    async def _kill_pid(self, pid: int) -> ToolResult:
        info = _running.pop(pid, None)
        if not info:
            return ToolResult(data=f"No tracked process with PID {pid}.", is_error=True)

        process = info["process"]
        if process.returncode is not None:
            elapsed = time.time() - info["start_time"]
            return ToolResult(
                data=f"Process {pid} already finished (exit: {process.returncode}, elapsed: {elapsed:.1f}s).",
                is_error=False,
            )

        try:
            process.terminate()
        except Exception:
            pass
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

    # ── background ──────────────────────────────────────────────────────

    async def _run_background(self, command: str, description: str,
                              cwd: str, shell: Optional[str] = None) -> ToolResult:
        cmdline = self._build_cmdline(command, shell)
        try:
            process = subprocess.Popen(
                cmdline,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return ToolResult(
                data=f"Command started in background (PID: {process.pid}): {description}",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(data=f"Failed to start background command: {e}", is_error=True)

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_cmdline(self, command: str, shell: Optional[str] = None) -> List[str]:
        sh = shell or _detect_windows_shell()
        binary = self._get_shell_binary(sh)
        if sh == "cmd":
            return [binary, "/c", command]
        else:
            return [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]

    def _get_shell_binary(self, shell: Optional[str] = None) -> str:
        sh = shell or _detect_windows_shell()
        if sh == "pwsh":
            for c in ["pwsh", "pwsh.exe"]:
                p = shutil.which(c)
                if p:
                    return p
            fallback = os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"),
                                    "PowerShell", "7", "pwsh.exe")
            return fallback if os.path.exists(fallback) else "pwsh.exe"
        elif sh == "powershell":
            for c in ["powershell", "powershell.exe"]:
                p = shutil.which(c)
                if p:
                    return p
            return os.path.join(os.environ.get("WINDIR", "C:\\Windows"),
                               "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        else:
            return os.environ.get("COMSPEC", "cmd.exe")

    @staticmethod
    def _decode(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                import locale
                return data.decode(locale.getpreferredencoding(False), errors="replace")
            except Exception:
                return data.decode("utf-8", errors="replace")

    @staticmethod
    def _format_output(stdout: str, stderr: str) -> str:
        stdout = PowerShellTool._truncate(stdout, MAX_STREAM_SIZE)
        stderr = PowerShellTool._truncate(stderr, MAX_STREAM_SIZE)
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
            process.kill()
        except (ProcessLookupError, OSError):
            pass
