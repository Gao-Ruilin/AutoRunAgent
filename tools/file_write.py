"""
FileWriteTool — Create or overwrite files.

Mirrors src/tools/WriteTool/ — creates new files or completely overwrites
existing ones with content validation and path security checks.
"""

import os
from typing import Any, Dict

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class FileWriteTool(Tool):
    """Write (create or overwrite) files on the filesystem."""

    @property
    def name(self) -> str:
        return "Write"

    @property
    def description(self) -> str:
        return """将文件写入本地文件系统。

用法:
- 如果提供的路径已存在文件，此工具将覆盖现有文件。
- 如果是现有文件，必须先使用 Read 工具读取文件内容。
- 如果尝试写入尚未读取的文件，此工具将失败。
- 对于修改现有文件，优先使用 Edit 工具——它只发送差异。
- 只使用此工具创建新文件或完全重写文件。
- 除非用户明确要求，绝不创建文档文件（*.md）或 README 文件。
- 除非用户明确要求，否则不要使用表情符号。除非被要求，避免将表情符号写入文件。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to write (must be absolute, not relative)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        file_path = args.get("file_path", "")
        if os.path.exists(file_path):
            return True
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        if not file_path:
            return ToolResult(data="Error: file_path is required", is_error=True)

        # Resolve absolute path
        if not os.path.isabs(file_path):
            file_path = os.path.join(context.cwd or os.getcwd(), file_path)
        file_path = os.path.normpath(file_path)

        # Security: prevent writing to sensitive locations
        if self._is_sensitive_path(file_path):
            return ToolResult(
                data=f"Error: Writing to system-sensitive paths is not allowed: {file_path}",
                is_error=True,
            )

        # Security: prevent overwriting system files
        if os.path.exists(file_path):
            if not os.access(file_path, os.W_OK):
                return ToolResult(
                    data=f"Error: Permission denied: {file_path}",
                    is_error=True,
                )

        # Ensure parent directory exists
        parent_dir = os.path.dirname(file_path)
        if parent_dir and not os.path.exists(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as e:
                return ToolResult(
                    data=f"Error: Cannot create parent directory: {e}",
                    is_error=True,
                )

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            file_size = os.path.getsize(file_path)

            # 通知索引器文件已变更
            self._notify_indexer(file_path, state=context.state)

            return ToolResult(
                data=f"File written successfully: {file_path} ({file_size} bytes)",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(
                data=f"Error writing file: {e}",
                is_error=True,
            )

    @staticmethod
    def _is_sensitive_path(file_path: str) -> bool:
        """Prevent writing to sensitive system locations."""
        forbidden_dirs = [
            "/etc/", "/boot/", "/sys/", "/proc/",
            "/System/", "/Library/System/",
            "C:\\Windows\\", "C:\\WINDOWS\\",
        ]
        normalized = file_path.replace("\\", "/")
        return any(d in normalized for d in forbidden_dirs)
