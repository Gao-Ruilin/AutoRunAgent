"""
ToolSearchTool — Search and discover available tools.

Mirrors src/tools/ToolSearchTool/ — helps users and agents find
the right tool for their task by searching keywords and descriptions.
"""

from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class ToolSearchTool(Tool):
    """Search for tools by keyword or capability."""

    @property
    def name(self) -> str:
        return "ToolSearch"

    @property
    def description(self) -> str:
        return """通过关键字或功能搜索和发现可用工具。

使用此工具：
- 查找用于特定任务的工具
- 发现之前未曾使用过的工具
- 了解有哪些可用功能
- 按功能关键字搜索（例如，"file"、"search"、"web"）

返回匹配的工具及其名称、描述和输入模式。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or capability to search for (e.g., 'file', 'search', 'git')",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Specific tool name to get details about",
                },
            },
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        from AutoRUN_v1.tools import get_tools

        query = args.get("query", "").strip().lower()
        tool_name = args.get("tool_name", "").strip()

        all_tools = get_tools()

        if tool_name:
            # Get details for a specific tool
            for t in all_tools:
                if t["name"].lower() == tool_name.lower():
                    return ToolResult(
                        data=f"## {t['name']}\n\n{t['description']}\n\n"
                             f"Input schema: {t.get('input_schema', {}).get('properties', {}).keys()}",
                        is_error=False,
                    )
            return ToolResult(
                data=f"Tool '{tool_name}' not found. Available: {', '.join(t['name'] for t in all_tools)}",
                is_error=False,
            )

        if query:
            # Search by keyword
            matches = []
            for t in all_tools:
                name = t["name"].lower()
                desc = t.get("description", "").lower()
                search_hint = t.get("search_hint", "").lower()

                score = 0
                if query in name:
                    score += 10
                if query in search_hint:
                    score += 5
                if query in desc:
                    score += 2

                if score > 0:
                    matches.append((t["name"], t.get("description", "").split("\n")[0], score))

            matches.sort(key=lambda x: x[2], reverse=True)

            if not matches:
                return ToolResult(
                    data=f"No tools found matching '{query}'. Try different keywords.",
                    is_error=False,
                )

            lines = [f"## Tools matching '{query}':"]
            for name, desc, score in matches[:10]:
                lines.append(f"  **{name}** — {desc[:120]}")

            return ToolResult(data="\n".join(lines), is_error=False)

        # No query, list all tools
        lines = ["## Available Tools:"]
        for t in all_tools:
            brief = t.get("description", "").split("\n")[0][:100]
            lines.append(f"  **{t['name']}** — {brief}")

        return ToolResult(data="\n".join(lines), is_error=False)
