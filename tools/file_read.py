"""
FileReadTool — Read files from the filesystem.

Mirrors src/tools/ReadTool/ — reads any file type with line numbering,
truncation for large files, and basic binary detection.
"""

import os
from typing import Any, Dict, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


MAX_LINES = 2000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB limit


class FileReadTool(Tool):
    """Read files and display their contents with line numbers."""

    @property
    def name(self) -> str:
        return "Read"

    @property
    def description(self) -> str:
        return """从本地文件系统读取文件。可以使用此工具直接访问任何文件。

假设此工具能够读取机器上的所有文件。如果用户提供了文件路径，假设该路径有效。读取不存在的文件是可以的——会返回错误。

用法:
- file_path 参数必须是绝对路径，不能是相对路径
- 默认情况下，从文件开头读取最多 2000 行
- 可以选择指定行偏移量和限制（对于长文件特别方便）
- 结果使用 cat -n 格式返回，行号从 1 开始
- 此工具可以读取图片（PNG、JPG 等）——内容会以视觉化方式呈现
- 此工具可以读取 PDF 文件（.pdf）。对于大型 PDF，使用 pages 参数指定页码范围
- 此工具可以读取 Jupyter notebooks（.ipynb 文件）并返回所有单元格及其输出
- 此工具只能读取文件，不能读取目录"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": "The line number to start reading from. Only provide if the file is too large to read at once.",
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "The number of lines to read.",
                    "exclusiveMinimum": 0,
                },
                "pages": {
                    "type": "string",
                    "description": "Page range for PDF files (e.g., \"1-5\", \"3\", \"10-20\"). Only applicable to PDF files.",
                },
            },
            "required": ["file_path"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = args.get("file_path", "")
        if not file_path:
            return ToolResult(data="Error: file_path is required", is_error=True)

        # Resolve path
        if not os.path.isabs(file_path):
            file_path = os.path.join(context.cwd or os.getcwd(), file_path)
        file_path = os.path.normpath(file_path)

        # Security: prevent reading sensitive system files
        if self._is_sensitive_path(file_path):
            return ToolResult(
                data=f"Error: Reading system-sensitive files is not allowed: {file_path}",
                is_error=True,
            )

        if not os.path.exists(file_path):
            return ToolResult(
                data=f"Error: File not found: {file_path}",
                is_error=True,
            )

        if os.path.isdir(file_path):
            return ToolResult(
                data=f"Error: Path is a directory, not a file: {file_path}",
                is_error=True,
            )

        try:
            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                return ToolResult(
                    data=f"Error: File too large ({file_size} bytes, max {MAX_FILE_SIZE} bytes)",
                    is_error=True,
                )

            # Check for binary files
            if self._is_binary(file_path):
                return ToolResult(
                    data=f"[Binary file detected: {os.path.basename(file_path)} ({file_size} bytes)]",
                    is_error=False,
                )

            offset = args.get("offset", 0)
            limit = args.get("limit", MAX_LINES)
            limit = min(limit, MAX_LINES)

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)

            if offset and offset > 0:
                start = min(offset, total_lines)
            else:
                start = 0

            end = min(start + limit, total_lines)
            selected_lines = lines[start:end]

            # Format with line numbers (cat -n style)
            formatted = []
            for i, line in enumerate(selected_lines, start=start + 1):
                formatted.append(f"{i}\t{line.rstrip()}")

            result = "\n".join(formatted)

            if end < total_lines:
                result += f"\n... (truncated, {total_lines - end} more lines)"

            return ToolResult(data=result, is_error=False)

        except UnicodeDecodeError:
            return ToolResult(
                data=f"[Binary file detected: {os.path.basename(file_path)} ({file_size} bytes)]",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(
                data=f"Error reading file: {e}",
                is_error=True,
            )

    @staticmethod
    def _is_binary(file_path: str) -> bool:
        """Quick check if a file is likely binary."""
        binary_extensions = {
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
            ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
            ".exe", ".dll", ".so", ".dylib", ".bin",
            ".mp3", ".mp4", ".avi", ".mov", ".wav",
            ".ttf", ".otf", ".woff", ".woff2",
            ".pyc", ".pyo", ".class",
        }
        ext = os.path.splitext(file_path)[1].lower()
        if ext in binary_extensions:
            return True

        # Fallback: check first 1024 bytes for null bytes
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
            return b"\x00" in chunk
        except Exception:
            return False

    @staticmethod
    def _is_sensitive_path(file_path: str) -> bool:
        """Prevent reading sensitive system files."""
        sensitive_patterns = [
            "/etc/shadow",
            "/etc/passwd",
            "/etc/sudoers",
            "/proc/",
            "/sys/",
            ".ssh/id_rsa",
            ".ssh/id_ed25519",
        ]
        normalized = file_path.lower()
        return any(pattern in normalized for pattern in sensitive_patterns)
