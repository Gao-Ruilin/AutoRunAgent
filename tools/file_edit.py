"""
FileEditTool — Edit files via exact string replacement.

Mirrors src/tools/EditTool/ — performs find-and-replace on files.
The primary editing tool; Write is for new files or full rewrites.
Supports replace_all mode for renaming across the file.
"""

import os
from typing import Any, Dict

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class FileEditTool(Tool):
    """Edit files by exact string replacement."""

    @property
    def name(self) -> str:
        return "Edit"

    @property
    def description(self) -> str:
        return """在文件中执行精确字符串替换。

用法:
- 在编辑之前，必须在对话中至少使用过一次 Read 工具。
- 如果尝试在没有读取文件的情况下进行编辑，此工具将报错。
- 从 Read 工具输出中编辑文本时，确保保留行号前缀之后出现的精确缩进（tabs/空格）。
- 行号前缀格式为: 行号 + tab。之后的所有内容是要匹配的实际文件内容。
- 始终优先编辑代码库中的现有文件。除非明确要求，绝不写入新文件。
- 除非用户明确要求，否则不要使用表情符号。除非被要求，避免在文件中添加表情符号。
- 如果 old_string 在文件中不是唯一的，编辑将失败。
- 可以提供带有更多上下文的更长字符串使其唯一，或使用 replace_all 替换 old_string 的所有实例。
- 使用 replace_all 在整个文件中替换和重命名字符串。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_string (default false)",
                    "default": False,
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)

        if not file_path:
            return ToolResult(data="Error: file_path is required", is_error=True)

        if old_string == new_string:
            return ToolResult(
                data="Error: old_string and new_string are identical",
                is_error=True,
            )

        # Resolve absolute path
        if not os.path.isabs(file_path):
            file_path = os.path.join(context.cwd or os.getcwd(), file_path)
        file_path = os.path.normpath(file_path)

        if not os.path.exists(file_path):
            return ToolResult(
                data=f"Error: File not found: {file_path}",
                is_error=True,
            )

        if not os.access(file_path, os.W_OK):
            return ToolResult(
                data=f"Error: Permission denied (not writable): {file_path}",
                is_error=True,
            )

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

        except UnicodeDecodeError:
            return ToolResult(
                data=f"Error: Cannot edit binary file: {file_path}",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                data=f"Error reading file: {e}",
                is_error=True,
            )

        # Check if old_string exists
        if old_string not in original_content:
            return ToolResult(
                data=f"Error: old_string not found in file. The text to replace was not found.",
                is_error=True,
            )

        # Check uniqueness if not replace_all
        if not replace_all:
            count = original_content.count(old_string)
            if count > 1:
                return ToolResult(
                    data=f"Error: old_string is not unique in the file. Found {count} occurrences. "
                         "Use replace_all=True to replace all occurrences, or provide more context "
                         "to make the match unique.",
                    is_error=True,
                )

        # Perform replacement
        if replace_all:
            new_content = original_content.replace(old_string, new_string)
            replacement_count = original_content.count(old_string)
        else:
            new_content = original_content.replace(old_string, new_string, 1)
            replacement_count = 1

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult(
                data=f"Error writing file: {e}",
                is_error=True,
            )

        # 通知索引器文件已变更
        self._notify_indexer(file_path, state=context.state)

        return ToolResult(
            data=f"File edited successfully. {replacement_count} replacement(s) made in {file_path}.",
            is_error=False,
        )
