"""
WebSearchTool — Web search integration.

Supports multiple backends with automatic fallback:
- Bing (primary, accessible in most regions including China)
- DuckDuckGo HTML (fallback)
"""

import os
import re
from typing import Any, Dict, List
from urllib.parse import quote, unquote

import httpx

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class WebSearchTool(Tool):
    """Search the web for information."""

    @property
    def name(self) -> str:
        return "WebSearch"

    @property
    def description(self) -> str:
        return """允许搜索网络并使用结果来提供信息回复。

- 为当前事件和最新数据提供最新信息
- 返回格式化为搜索结果块的信息
- 使用此工具访问模型知识截止日期之外的信息
- 搜索在单个 API 调用中自动执行

重要要求:
  - 回答用户问题后，在回复末尾包含"Sources:"部分
  - 在 Sources 部分，将所有相关的搜索结果 URL 列为 markdown 超链接
  - 这是强制性的——绝不在回复中省略来源引用

用法说明:
  - 支持域名过滤，可以包含或阻止特定网站
  - Web 搜索在特定地区可用"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to use",
                    "minLength": 2,
                },
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only include search results from these domains",
                },
                "blocked_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Never include search results from these domains",
                },
            },
            "required": ["query"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        query = args.get("query", "").strip()
        allowed_domains = args.get("allowed_domains", [])
        blocked_domains = args.get("blocked_domains", [])

        if not query or len(query) < 2:
            return ToolResult(data="Error: query is required (min 2 characters)", is_error=True)

        # Determine backend order from env or default to Bing-first
        backend_order = os.environ.get("AUTORUN_SEARCH_BACKEND", "").strip().lower()
        if backend_order == "ddg":
            backends = ["ddg", "bing"]
        elif backend_order == "bing":
            backends = ["bing"]
        else:
            backends = ["bing", "ddg"]

        errors = []
        for backend in backends:
            try:
                results = await self._search(query, allowed_domains, blocked_domains, backend)
                if results and "No search results found" not in results:
                    return ToolResult(data=results, is_error=False)
                errors.append(f"{backend}: {results}")
            except Exception as e:
                errors.append(f"{backend}: {type(e).__name__}: {e}")

        return ToolResult(
            data=self._no_results_message(query, errors),
            is_error=False,
        )

    async def _search(self, query: str, allowed_domains: List[str],
                      blocked_domains: List[str], backend: str) -> str:
        """Execute a web search using the specified backend."""
        if backend == "bing":
            return await self._search_bing(query, allowed_domains, blocked_domains)
        else:
            return await self._search_ddg(query, allowed_domains, blocked_domains)

    # ── Bing Backend ────────────────────────────────────────────────────────

    async def _search_bing(self, query: str, allowed_domains: List[str],
                            blocked_domains: List[str]) -> str:
        """Search using Bing (accessible in most regions including China)."""
        encoded = quote(query)
        # Use cn.bing.com for better accessibility
        search_url = f"https://cn.bing.com/search?q={encoded}&setlang=zh-cn"

        async with httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
        ) as client:
            response = await client.get(search_url)
            if response.status_code != 200:
                return self._no_results_message(query)

            html = response.text
            results = self._parse_bing_results(html)

            return self._format_results(results, allowed_domains, blocked_domains, query)

    @staticmethod
    def _parse_bing_results(html: str) -> List[Dict[str, str]]:
        """Parse Bing HTML search results."""
        results = []

        # Bing main result blocks: <li class="b_algo">
        blocks = re.split(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>', html)
        if len(blocks) <= 1:
            # Try alternative pattern
            blocks = re.split(r'<li[^>]*class="[^"]*b_algo', html)

        for block in blocks[1:]:
            # Title: <h2><a href="...">Title</a></h2>
            title_match = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not title_match:
                # Broader match
                title_match = re.search(r'<a[^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)

            title = ""
            url = ""
            if title_match:
                url = title_match.group(1)
                title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()

            # Snippet: <p> or <div class="b_caption">
            snippet = ""
            snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            if snippet_match:
                snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip()
            if not snippet:
                cap_match = re.search(r'class="[^"]*b_caption[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
                if cap_match:
                    snippet = re.sub(r'<[^>]+>', '', cap_match.group(1)).strip()[:200]

            if title:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })

        return results

    # ── DuckDuckGo Backend ──────────────────────────────────────────────────

    async def _search_ddg(self, query: str, allowed_domains: List[str],
                           blocked_domains: List[str]) -> str:
        """Search using DuckDuckGo HTML (fallback)."""
        encoded = quote(query)
        search_url = f"https://html.duckduckgo.com/html/?q={encoded}"

        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "AutoRUN/1.0 (Web Search Tool)"},
        ) as client:
            response = await client.get(search_url)
            if response.status_code != 200:
                return self._no_results_message(query)

            html = response.text
            results = WebSearchTool._parse_ddg_results(html)

            return self._format_results(results, allowed_domains, blocked_domains, query)

    @staticmethod
    def _parse_ddg_results(html: str) -> List[Dict[str, str]]:
        """Parse DuckDuckGo HTML results."""
        results = []

        # DDG result blocks with flexible class matching
        result_blocks = re.split(r'<div[^>]*class="[^"]*result[^"]*"[^>]*>', html)

        for block in result_blocks[1:]:
            # Title + URL: <a class="result__a" href="...">Title</a>
            link_match = re.search(
                r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                block, re.DOTALL
            )
            title = ""
            url = ""
            if link_match:
                raw_url = link_match.group(1)
                title = re.sub(r'<[^>]+>', '', link_match.group(2)).strip()
                # Decode DDG redirect URL
                uddg_match = re.search(r'uddg=([^&]+)', raw_url)
                if uddg_match:
                    url = unquote(uddg_match.group(1))
                else:
                    url = raw_url

            # Snippet: <a class="result__snippet">
            snippet = ""
            snippet_match = re.search(
                r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                block, re.DOTALL
            )
            if snippet_match:
                snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip()

            if title:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })

        return results

    # ── Shared Formatter ────────────────────────────────────────────────────

    @staticmethod
    def _format_results(results: List[Dict[str, str]],
                         allowed_domains: List[str],
                         blocked_domains: List[str],
                         query: str) -> str:
        """Apply domain filters and format results for output."""
        if allowed_domains:
            results = [r for r in results
                      if any(d in r.get("url", "") for d in allowed_domains)]
        if blocked_domains:
            results = [r for r in results
                      if not any(d in r.get("url", "") for d in blocked_domains)]

        if not results:
            return WebSearchTool._no_results_message(query)

        # Deduplicate by URL
        seen = set()
        unique = []
        for r in results:
            u = r.get("url", "")
            if u and u not in seen:
                seen.add(u)
                unique.append(r)

        formatted = []
        for i, r in enumerate(unique[:10], 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            snippet = r.get("snippet", "")

            formatted.append(f"{i}. {title}")
            if url:
                formatted.append(f"   URL: {url}")
            if snippet:
                formatted.append(f"   {snippet}")
            formatted.append("")

        return "\n".join(formatted)

    @staticmethod
    def _no_results_message(query: str, errors: List[str] = None) -> str:
        """Message returned when no search results are found."""
        msg = f"No search results found for: {query}\n\n"
        if errors:
            msg += "Backend status:\n"
            for e in errors:
                msg += f"  - {e}\n"
            msg += "\n"
        msg += (
            "The web search could not return results. Consider:\n"
            "- Refining your search query\n"
            "- Using WebFetch to access specific URLs directly\n"
            "- Checking your network connection\n"
            "- Setting AUTORUN_SEARCH_BACKEND env var (bing, ddg, or bing,ddg for fallback)"
        )
        return msg
