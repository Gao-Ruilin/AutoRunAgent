"""
GlobTool — Fast file pattern matching.

Mirrors src/tools/GlobTool/ — uses glob patterns to find files.
Sorts results by modification time for relevance.
"""

import glob as glob_module
import os
from typing import Any, Dict, List

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class GlobTool(Tool):
    """Find files by glob pattern matching."""

    @property
    def name(self) -> str:
        return "Glob"

    @property
    def description(self) -> str:
        return """快速的文件模式匹配工具，适用于任何规模的代码库。

- 支持 glob 模式，如 "**/*.js" 或 "src/**/*.ts"
- 返回匹配的文件路径，按修改时间排序
- 需要按名称模式查找文件时使用此工具
- 进行可能需要多轮 glob 和 grep 的开放式搜索时，请使用 Agent 工具"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against",
                },
                "path": {
                    "type": "string",
                    "description": "The directory to search in. If not specified, the current working directory will be used.",
                },
            },
            "required": ["pattern"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = args.get("pattern", "")
        search_path = args.get("path") or context.cwd or os.getcwd()

        if not pattern:
            return ToolResult(data="Error: pattern is required", is_error=True)

        if not os.path.isabs(search_path):
            search_path = os.path.join(context.cwd or os.getcwd(), search_path)

        search_path = os.path.normpath(search_path)

        if not os.path.exists(search_path):
            return ToolResult(
                data=f"Error: Directory not found: {search_path}",
                is_error=True,
            )

        try:
            full_pattern = os.path.join(search_path, pattern)
            matches = glob_module.glob(full_pattern, recursive=True)

            # Also handle ** patterns across directories
            if "**" in pattern:
                # Already handled by recursive=True
                pass

            # Filter out directories, keep only files
            file_matches = [m for m in matches if os.path.isfile(m)]

            # Sort by modification time (newest first)
            file_matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)

            # Limit results
            MAX_RESULTS = 500
            if len(file_matches) > MAX_RESULTS:
                result = "\n".join(file_matches[:MAX_RESULTS])
                result += f"\n... (truncated, {len(file_matches) - MAX_RESULTS} more files match)"
            elif file_matches:
                result = "\n".join(file_matches)
            else:
                result = "No files found matching pattern."

            return ToolResult(data=result, is_error=False)

        except Exception as e:
            return ToolResult(
                data=f"Error searching for files: {e}",
                is_error=True,
            )
