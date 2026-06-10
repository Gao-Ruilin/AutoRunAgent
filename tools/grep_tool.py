"""
GrepTool — Content search using regular expressions.

Mirrors src/tools/GrepTool/ — provides regex-based code search.
Supports context lines, file type filtering, and output modes.
"""

import os
import re
from typing import Any, Dict, List, Tuple

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class GrepTool(Tool):
    """Search file contents with regular expressions."""

    @property
    def name(self) -> str:
        return "Grep"

    @property
    def description(self) -> str:
        return """基于 ripgrep 构建的强大搜索工具。

用法:
- 始终使用 Grep 进行搜索任务。绝不作为 Bash 命令调用 grep 或 rg。
- 支持完整的正则表达式语法（例如 "log.*Error", "function\\s+\\w+"）
- 使用 glob 参数过滤文件（例如 "*.js", "**/*.tsx"）或 type 参数
- 输出模式: "content" 显示匹配行, "files_with_matches" 仅显示文件路径（默认）, "count" 显示匹配计数
- 对需要多轮搜索的任务使用 Agent 工具
- 模式语法: 使用 ripgrep（不是 grep）——字面大括号需要转义
- 多行匹配: 默认模式仅在单行内匹配。跨行模式使用 multiline: true"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to current working directory.",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. \"*.js\", \"*.{ts,tsx}\")",
                },
                "type": {
                    "type": "string",
                    "description": "File type to search. Common types: js, py, rust, go, java, etc.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode: \"content\" shows matching lines, \"files_with_matches\" shows file paths, \"count\" shows match counts",
                },
                "-A": {
                    "type": "integer",
                    "description": "Number of lines to show after each match",
                },
                "-B": {
                    "type": "integer",
                    "description": "Number of lines to show before each match",
                },
                "-C": {
                    "type": "integer",
                    "description": "Number of lines to show before and after each match (context)",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search",
                },
                "-n": {
                    "type": "boolean",
                    "description": "Show line numbers in output. Defaults to true for content mode.",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Limit output to first N lines/entries",
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline mode where . matches newlines and patterns can span lines",
                },
            },
            "required": ["pattern"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = args.get("pattern", "")
        search_path = args.get("path") or context.cwd or os.getcwd()
        glob_filter = args.get("glob")
        file_type = args.get("type")
        output_mode = args.get("output_mode", "files_with_matches")
        after_context = args.get("-A", 0)
        before_context = args.get("-B", 0)
        context_lines = args.get("-C", 0)
        case_insensitive = args.get("-i", False)
        show_line_numbers = args.get("-n", True)
        head_limit = args.get("head_limit", 250)
        is_multiline = args.get("multiline", False)

        if not pattern:
            return ToolResult(data="Error: pattern is required", is_error=True)

        if not os.path.isabs(search_path):
            search_path = os.path.join(context.cwd or os.getcwd(), search_path)
        search_path = os.path.normpath(search_path)

        if not os.path.exists(search_path):
            return ToolResult(
                data=f"Error: Path not found: {search_path}",
                is_error=True,
            )

        # Compile regex
        try:
            flags = 0
            if case_insensitive:
                flags |= re.IGNORECASE
            if is_multiline:
                flags |= re.DOTALL
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(
                data=f"Error: Invalid regex pattern: {e}",
                is_error=True,
            )

        # Collect files to search
        files_to_search = self._collect_files(search_path, glob_filter, file_type)

        if not files_to_search:
            return ToolResult(data="No files found to search.", is_error=False)

        # Search files
        all_matches: List[Tuple[str, int, str]] = []  # (file_path, line_num, line_content)
        file_counts: Dict[str, int] = {}

        for file_path in files_to_search:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

                raw_content = "".join(lines)

                if is_multiline:
                    # Multiline mode: search across the whole content
                    for match in compiled.finditer(raw_content):
                        line_num = raw_content[:match.start()].count("\n") + 1
                        all_matches.append((file_path, line_num, match.group()))
                        file_counts[file_path] = file_counts.get(file_path, 0) + 1
                else:
                    # Line-by-line search
                    for i, line in enumerate(lines, 1):
                        if compiled.search(line):
                            all_matches.append((file_path, i, line.rstrip("\n")))
                            file_counts[file_path] = file_counts.get(file_path, 0) + 1

            except (UnicodeDecodeError, PermissionError, OSError):
                continue

        # Format output
        if output_mode == "files_with_matches":
            matched_files = sorted(set(f for f, _, _ in all_matches))
            if head_limit and len(matched_files) > head_limit:
                result = "\n".join(matched_files[:head_limit])
                result += f"\n... (truncated, {len(matched_files) - head_limit} more files)"
            else:
                result = "\n".join(matched_files) if matched_files else "No matches found."

        elif output_mode == "count":
            lines_output = [
                f"{path}: {count}"
                for path, count in sorted(file_counts.items(),
                                          key=lambda x: x[1], reverse=True)
            ]
            if head_limit and len(lines_output) > head_limit:
                lines_output = lines_output[:head_limit]
            result = "\n".join(lines_output) if lines_output else "No matches found."

        else:  # content mode
            effective_context = context_lines or max(before_context, after_context)
            if effective_context:
                # Collect unique files
                matched_files_set = set(f for f, _, _ in all_matches)
                result_parts = []
                count = 0
                for file_path in sorted(matched_files_set):
                    if head_limit and count >= head_limit:
                        result_parts.append(f"... (truncated at {head_limit} lines)")
                        break
                    file_matches = [(ln, lc) for f, ln, lc in all_matches if f == file_path]
                    result_parts.append(f"{file_path}:")
                    for line_num, content in file_matches[:head_limit - count if head_limit else len(file_matches)]:
                        if show_line_numbers:
                            result_parts.append(f"  {line_num}: {content}")
                        else:
                            result_parts.append(f"  {content}")
                        count += 1
                result = "\n".join(result_parts)
            else:
                lines_output = []
                for file_path, line_num, content in all_matches[:head_limit if head_limit else len(all_matches)]:
                    if show_line_numbers:
                        lines_output.append(f"{file_path}:{line_num}: {content}")
                    else:
                        lines_output.append(f"{file_path}: {content}")
                result = "\n".join(lines_output) if lines_output else "No matches found."

        return ToolResult(data=result, is_error=False)

    @staticmethod
    def _collect_files(search_path: str, glob_filter: str = None,
                       file_type: str = None) -> List[str]:
        """Collect files to search based on path and filters."""
        files = []

        # File type to extension mapping
        type_extensions = {
            "py": [".py"],
            "js": [".js"],
            "ts": [".ts", ".tsx"],
            "jsx": [".jsx"],
            "tsx": [".tsx"],
            "rust": [".rs"],
            "go": [".go"],
            "java": [".java"],
            "rb": [".rb"],
            "php": [".php"],
            "c": [".c", ".h"],
            "cpp": [".cpp", ".cc", ".cxx", ".hpp", ".h"],
            "css": [".css"],
            "html": [".html", ".htm"],
            "json": [".json"],
            "yaml": [".yaml", ".yml"],
            "md": [".md"],
            "toml": [".toml"],
        }

        exts = None
        if file_type and file_type in type_extensions:
            exts = type_extensions[file_type]

        if os.path.isfile(search_path):
            return [search_path]

        for root, dirs, filenames in os.walk(search_path):
            # Skip hidden directories and common ignores
            dirs[:] = [d for d in dirs if not d.startswith(".")
                      and d not in ("node_modules", "__pycache__", ".git", ".venv", "vendor")]

            for filename in filenames:
                if filename.startswith("."):
                    continue

                # Check file type filter
                if exts:
                    if not any(filename.endswith(ext) for ext in exts):
                        continue

                # Check glob filter
                if glob_filter:
                    from fnmatch import fnmatch
                    if not fnmatch(filename, glob_filter):
                        continue

                files.append(os.path.join(root, filename))

        return files
