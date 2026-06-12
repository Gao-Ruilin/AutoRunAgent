"""
SSH Tools — Execute commands and manipulate files on remote servers via SSH.

Provides four tools:
- SSHBashTool: Execute shell commands on a remote host
- SSHReadTool: Read files from a remote host
- SSHWriteTool: Write files to a remote host
- SSHEditTool: Edit files on a remote host via precise string replacement

All tools require an 'ssh_config' parameter referencing a saved SSH configuration.
"""

import logging
from typing import Any, Dict

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult
from AutoRUN_v1.utils.config import get_ssh_config_decrypted, get_ssh_configs
from AutoRUN_v1.utils.ssh_client import SSHClient, get_ssh_client

logger = logging.getLogger(__name__)

MAX_STREAM_SIZE = 300 * 1024
DEFAULT_TIMEOUT_MS = 10000


def _resolve_ssh_config(args: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and decrypt SSH config from tool args.

    Returns:
        Dict with 'ok' and config fields, or 'error'.
    """
    config_name = args.get("ssh_config", "").strip()
    if not config_name:
        # Try to use the first available config
        configs = get_ssh_configs()
        if not configs:
            return {"error": "No SSH configurations saved. Use the settings panel to add one."}
        config_name = configs[0].get("name", "")
        if not config_name:
            return {"error": "No valid SSH config name found."}

    cfg = get_ssh_config_decrypted(config_name)
    if cfg is None:
        available = [c.get("name", "") for c in get_ssh_configs()]
        return {
            "error": f"SSH config '{config_name}' not found. Available: {', '.join(available) if available else 'none'}"
        }

    return {"ok": True, "config": cfg}


def _ensure_connected(client: SSHClient, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure SSH connection is established, connect if not."""
    host = cfg["host"]
    port = cfg.get("port", 22)
    user = cfg.get("user", "root")
    auth_type = cfg.get("auth_type", "password")

    if client.is_connected(host, port):
        return {"ok": True}

    connect_args: Dict[str, Any] = {
        "name": cfg.get("name", "unnamed"),
        "host": host,
        "port": port,
        "user": user,
    }

    if auth_type == "password":
        connect_args["password"] = cfg.get("password", "")
    elif auth_type == "key":
        connect_args["key_path"] = cfg.get("key_path")
        connect_args["passphrase"] = cfg.get("passphrase")

    result = client.connect(**connect_args)
    if not result.get("ok"):
        return {"ok": False, "error": f"SSH connection failed: {result.get('error', 'Unknown error')}"}

    return {"ok": True}


# ── SSHBashTool ─────────────────────────────────────────────────────────────

class SSHBashTool(Tool):
    """Execute shell commands on a remote server via SSH."""

    @property
    def name(self) -> str:
        return "SSHBash"

    @property
    def description(self) -> str:
        return """在远程服务器上通过 SSH 执行 shell 命令。

需要先通过设置面板配置 SSH 连接信息，然后在调用时通过 `ssh_config` 参数指定使用哪个配置。

## 使用说明
- `ssh_config`: SSH 配置名称（必填）。在设置面板 → SSH 连接中管理。
- `command`: 要执行的 shell 命令（必填）。
- `timeout`: 超时时间（毫秒），默认 10000（10秒）。设为 0 表示无超时。

## 示例
- SSHBash(ssh_config="my-server", command="ls -la /var/log")
- SSHBash(ssh_config="dev-box", command="docker ps", timeout=5000)

## 注意事项
- 命令会在远程服务器的默认 shell 中执行
- 命令的工作目录是远程用户的 home 目录
- 长时间运行的命令请设置合适的 timeout
- 密码和密钥不会出现在日志中
- 需要先通过 AutoRUN 设置面板配置 SSH 连接"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ssh_config": {
                    "type": "string",
                    "description": "SSH 配置名称。在设置面板 → SSH 连接中查看已保存的配置名称。",
                },
                "command": {
                    "type": "string",
                    "description": "要在远程服务器上执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（毫秒），默认 10000 = 10秒，0 = 无限制",
                    "minimum": 0,
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
        # Resolve SSH config
        resolved = _resolve_ssh_config(args)
        if "error" in resolved:
            return ToolResult(data=resolved["error"], is_error=True)

        cfg = resolved["config"]
        host = cfg["host"]
        port = cfg.get("port", 22)
        command = args.get("command", "").strip()

        if not command:
            return ToolResult(data="(no command provided)", is_error=False)

        timeout_ms = args.get("timeout", DEFAULT_TIMEOUT_MS)
        timeout_s = timeout_ms / 1000.0 if timeout_ms > 0 else SSHClient.COMMAND_TIMEOUT

        client = get_ssh_client()

        # Ensure connection
        conn_result = _ensure_connected(client, cfg)
        if not conn_result.get("ok"):
            return ToolResult(
                data=f"SSH connection to {host}:{port} failed: {conn_result.get('error', 'Unknown error')}",
                is_error=True,
            )

        # Execute command
        result = client.exec_command(host, command, port, timeout=timeout_s)

        if not result.get("ok"):
            return ToolResult(
                data=f"Remote command failed: {result.get('error', 'Unknown error')}\n[stderr]\n{result.get('stderr', '')}",
                is_error=True,
            )

        stdout = self._truncate(result.get("stdout", ""), MAX_STREAM_SIZE)
        stderr = self._truncate(result.get("stderr", ""), MAX_STREAM_SIZE)
        exit_code = result.get("exit_code", 0)

        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        output = "\n".join(parts) if parts else "(no output)"

        if exit_code != 0:
            output = f"[exit code: {exit_code}]\n{output}"
            return ToolResult(data=output, is_error=True)

        return ToolResult(data=output, is_error=False)

    @staticmethod
    def _truncate(text: str, max_size: int) -> str:
        if len(text) <= max_size:
            return text
        return (
            text[:max_size]
            + f"\n... [truncated at {max_size} bytes, total was {len(text)} bytes]"
        )


# ── SSHReadTool ─────────────────────────────────────────────────────────────

class SSHReadTool(Tool):
    """Read files from a remote server via SFTP."""

    @property
    def name(self) -> str:
        return "SSHRead"

    @property
    def description(self) -> str:
        return """从远程服务器通过 SSH/SFTP 读取文件内容。

## 使用说明
- `ssh_config`: SSH 配置名称（必填）。
- `file_path`: 远程文件的绝对路径（必填）。
- `offset`: 起始行号（可选）。
- `limit`: 读取的最大行数（可选）。

## 示例
- SSHRead(ssh_config="my-server", file_path="/var/log/syslog", limit=100)
- SSHRead(ssh_config="dev-box", file_path="/etc/nginx/nginx.conf")

## 注意事项
- 需要远程文件有读取权限
- 支持文本文件读取，二进制文件会尝试 UTF-8 解码"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ssh_config": {
                    "type": "string",
                    "description": "SSH 配置名称",
                },
                "file_path": {
                    "type": "string",
                    "description": "远程文件的绝对路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（从 1 开始），不指定则从头读取",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取的行数，默认读取全部",
                    "minimum": 1,
                },
            },
            "required": ["file_path"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        resolved = _resolve_ssh_config(args)
        if "error" in resolved:
            return ToolResult(data=resolved["error"], is_error=True)

        cfg = resolved["config"]
        host = cfg["host"]
        port = cfg.get("port", 22)
        file_path = args.get("file_path", "").strip()

        if not file_path:
            return ToolResult(data="No file path provided.", is_error=True)

        client = get_ssh_client()

        conn_result = _ensure_connected(client, cfg)
        if not conn_result.get("ok"):
            return ToolResult(
                data=f"SSH connection failed: {conn_result.get('error')}",
                is_error=True,
            )

        result = client.read_file(host, file_path, port)

        if not result.get("ok"):
            return ToolResult(
                data=f"Failed to read remote file: {result.get('error', 'Unknown error')}",
                is_error=True,
            )

        content = result.get("content", "")

        # Apply offset/limit if specified
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None or limit is not None:
            lines = content.split("\n")
            start = (offset - 1) if offset else 0
            end = start + limit if limit else len(lines)
            content = "\n".join(lines[start:end])

        content = self._truncate(content)
        return ToolResult(data=content, is_error=False)

    @staticmethod
    def _truncate(text: str, max_size: int = 500 * 1024) -> str:
        if len(text) <= max_size:
            return text
        return (
            text[:max_size]
            + f"\n... [truncated at {max_size} bytes, total was {len(text)} bytes]"
        )


# ── SSHWriteTool ────────────────────────────────────────────────────────────

class SSHWriteTool(Tool):
    """Write files to a remote server via SFTP."""

    @property
    def name(self) -> str:
        return "SSHWrite"

    @property
    def description(self) -> str:
        return """将内容写入远程服务器上的文件（通过 SSH/SFTP）。

## 使用说明
- `ssh_config`: SSH 配置名称（必填）。
- `file_path`: 远程文件的绝对路径（必填）。
- `content`: 要写入的内容（必填）。

## 示例
- SSHWrite(ssh_config="my-server", file_path="/tmp/test.txt", content="Hello World")

## 注意事项
- 如果父目录不存在，会自动创建
- 如果文件已存在，会被覆盖
- 需要写权限"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ssh_config": {
                    "type": "string",
                    "description": "SSH 配置名称",
                },
                "file_path": {
                    "type": "string",
                    "description": "远程文件的绝对路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["file_path", "content"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        resolved = _resolve_ssh_config(args)
        if "error" in resolved:
            return ToolResult(data=resolved["error"], is_error=True)

        cfg = resolved["config"]
        host = cfg["host"]
        port = cfg.get("port", 22)
        file_path = args.get("file_path", "").strip()
        content = args.get("content", "")

        if not file_path:
            return ToolResult(data="No file path provided.", is_error=True)

        client = get_ssh_client()

        conn_result = _ensure_connected(client, cfg)
        if not conn_result.get("ok"):
            return ToolResult(
                data=f"SSH connection failed: {conn_result.get('error')}",
                is_error=True,
            )

        result = client.write_file(host, file_path, content, port)

        if not result.get("ok"):
            return ToolResult(
                data=f"Failed to write remote file: {result.get('error', 'Unknown error')}",
                is_error=True,
            )

        return ToolResult(
            data=f"File written successfully: {result.get('path', file_path)} ({len(content)} bytes)",
            is_error=False,
        )


# ── SSHEditTool ─────────────────────────────────────────────────────────────

class SSHEditTool(Tool):
    """Edit remote files via precise string replacement."""

    @property
    def name(self) -> str:
        return "SSHEdit"

    @property
    def description(self) -> str:
        return """在远程服务器上通过精确字符串替换编辑文件。

## 使用说明
- `ssh_config`: SSH 配置名称（必填）。
- `file_path`: 远程文件的绝对路径（必填）。
- `old_string`: 要替换的文本（必须与文件中的内容精确匹配，必填）。
- `new_string`: 替换后的文本（必填）。
- `replace_all`: 如果为 true，替换所有匹配项（可选，默认 false）。

## 示例
- SSHEdit(ssh_config="my-server", file_path="/etc/hosts", old_string="127.0.0.1 localhost", new_string="127.0.0.1 localhost myapp")

## 注意事项
- old_string 必须在文件中唯一，除非使用 replace_all
- 建议先用 SSHRead 查看文件内容，确保 old_string 精确匹配
- 修改重要配置文件前建议先备份"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ssh_config": {
                    "type": "string",
                    "description": "SSH 配置名称",
                },
                "file_path": {
                    "type": "string",
                    "description": "远程文件的绝对路径",
                },
                "old_string": {
                    "type": "string",
                    "description": "要替换的文本（精确匹配）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项（默认 false，仅替换第一个）",
                    "default": False,
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        resolved = _resolve_ssh_config(args)
        if "error" in resolved:
            return ToolResult(data=resolved["error"], is_error=True)

        cfg = resolved["config"]
        host = cfg["host"]
        port = cfg.get("port", 22)
        file_path = args.get("file_path", "").strip()
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)

        if not file_path:
            return ToolResult(data="No file path provided.", is_error=True)
        if old_string == new_string:
            return ToolResult(data="old_string and new_string are identical, no change needed.", is_error=False)

        client = get_ssh_client()

        conn_result = _ensure_connected(client, cfg)
        if not conn_result.get("ok"):
            return ToolResult(
                data=f"SSH connection failed: {conn_result.get('error')}",
                is_error=True,
            )

        # Read the remote file
        read_result = client.read_file(host, file_path, port)
        if not read_result.get("ok"):
            return ToolResult(
                data=f"Failed to read remote file: {read_result.get('error', 'Unknown error')}",
                is_error=True,
            )

        content = read_result.get("content", "")

        if replace_all:
            occurrences = content.count(old_string)
            if occurrences == 0:
                return ToolResult(
                    data=f"old_string not found in remote file: {file_path}",
                    is_error=True,
                )
            new_content = content.replace(old_string, new_string)
        else:
            if old_string not in content:
                return ToolResult(
                    data=f"old_string not found in remote file: {file_path}",
                    is_error=True,
                )
            if content.count(old_string) > 1:
                return ToolResult(
                    data=f"old_string appears multiple times in {file_path}. Use replace_all=true to replace all occurrences, or use a more specific string.",
                    is_error=True,
                )
            new_content = content.replace(old_string, new_string, 1)

        # Write back
        write_result = client.write_file(host, file_path, new_content, port)
        if not write_result.get("ok"):
            return ToolResult(
                data=f"Failed to write remote file: {write_result.get('error', 'Unknown error')}",
                is_error=True,
            )

        if replace_all:
            return ToolResult(
                data=f"File edited successfully: {file_path} ({occurrences} replacements)",
                is_error=False,
            )
        return ToolResult(
            data=f"File edited successfully: {file_path} (1 replacement)",
            is_error=False,
        )
