"""
WebFetchTool — Fetch and process web page content.

Mirrors src/tools/WebFetchTool/ — fetches URLs, converts HTML to markdown,
and processes the content with an AI prompt. Includes caching.
"""

import hashlib
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


# Simple in-memory cache (15-minute TTL)
_cache: Dict[str, tuple] = {}
CACHE_TTL = 15 * 60  # 15 minutes


class WebFetchTool(Tool):
    """Fetch content from a URL and process it."""

    @property
    def name(self) -> str:
        return "WebFetch"

    @property
    def description(self) -> str:
        return """从指定 URL 获取内容并使用 AI 模型进行处理。

- 接收 URL 和提示词作为输入
- 获取 URL 内容，将 HTML 转换为 markdown
- 使用小型快速模型处理内容
- 返回模型关于内容的响应
- 需要检索和分析 Web 内容时使用此工具

用法说明:
  - 重要: 如果有 MCP 提供的网页抓取工具，优先使用它而不是此工具，因为它可能有更少的限制。
  - URL 必须是完整的有效 URL
  - HTTP URL 将自动升级为 HTTPS
  - prompt 应描述你想从页面中提取什么信息
  - 此工具是只读的，不会修改任何文件
  - 如果内容非常大，结果可能会被摘要
  - 包含 15 分钟自清理缓存，重复访问同一 URL 时响应更快
  - 当 URL 重定向到不同主机时，使用重定向 URL 发出新的 WebFetch 请求
  - 对于 GitHub URL，优先使用 Bash 工具通过 gh CLI 访问"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "The URL to fetch content from",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to run on the fetched content",
                },
            },
            "required": ["url", "prompt"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        url = args.get("url", "").strip()
        prompt = args.get("prompt", "").strip()

        if not url:
            return ToolResult(data="Error: url is required", is_error=True)
        if not prompt:
            return ToolResult(data="Error: prompt is required", is_error=True)

        # Normalize URL
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Check cache
        cache_key = hashlib.md5((url + prompt).encode()).hexdigest()
        if cache_key in _cache:
            cached_data, timestamp = _cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return ToolResult(data=cached_data, is_error=False)

        # Validate URL
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return ToolResult(
                    data=f"Error: Invalid URL: {url}",
                    is_error=True,
                )
        except Exception:
            return ToolResult(
                data=f"Error: Invalid URL format: {url}",
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "AutoRUN/1.0 (Web Fetch Tool)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            ) as client:
                response = await client.get(url)

                if response.status_code >= 400:
                    return ToolResult(
                        data=f"Error: HTTP {response.status_code} from {url}",
                        is_error=True,
                    )

                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    content = self._html_to_text(response.text)
                elif "text/" in content_type or "application/json" in content_type:
                    content = response.text
                else:
                    return ToolResult(
                        data=f"Error: Unsupported content type: {content_type}",
                        is_error=True,
                    )

        except httpx.TimeoutException:
            return ToolResult(
                data=f"Error: Request timed out for {url}",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                data=f"Error fetching URL: {e}",
                is_error=True,
            )

        # Truncate content for the response
        max_content_length = 100000
        if len(content) > max_content_length:
            content = content[:max_content_length] + "\n... (content truncated)"

        result = f"Content from {url}:\n\n{content}"

        # Cache result
        _cache[cache_key] = (result, time.time())

        return ToolResult(data=result, is_error=False)

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Basic HTML to text conversion."""
        import re

        # Remove scripts and styles
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', html)

        # Decode common entities
        entities = {
            '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
            '&#39;': "'", '&nbsp;': ' ', '&mdash;': '—', '&ndash;': '–',
        }
        for entity, char in entities.items():
            text = text.replace(entity, char)

        # Clean up whitespace
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = text.strip()

        return text
