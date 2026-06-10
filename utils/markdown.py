"""
轻量级 Markdown → Rich Text 解析器。

将 Markdown 文本转换为 rich.text.Text 对象，支持:
- **粗体** / *斜体* / `行内代码`
- 围栏代码块 (```) 带语法高亮
- 标题 (# ## ### ...)
- 无序列表 (- * +) 和有序列表 (1. 2.)
- 引用 (> blockquote)
- 水平线 (---, ***)
- 链接 [text](url)
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

from rich.text import Text as RichText
from rich.syntax import Syntax


# ── Style constants ───────────────────────────────────────────────────────────

# Color constants matching webUI (--accent, --text-primary, etc.)
STYLE_BOLD = "bold"
STYLE_ITALIC = "italic"
STYLE_CODE = "#6c8cff"          # inline code — matches webUI --accent
STYLE_CODE_BG = "#1e1e2e"       # inline code background
STYLE_HEADING = "bold #6c8cff"  # heading — matches webUI --accent
STYLE_BLOCKQUOTE = "#888888"
STYLE_LINK = "#6c8cff"          # link — matches webUI --accent
STYLE_TEXT = "#ffffff"
STYLE_DIM = "#888888"
STYLE_LIST_MARKER = "#666666"


# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches fenced code blocks: ```lang\ncode\n```
_FENCE_RE = re.compile(
    r'^```(\w*)\n(.*?)```', re.DOTALL | re.MULTILINE
)

# Inline formatting (processed in order)
# Bold: **text** or __text__
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*|__(.+?)__')
# Italic: *text* or _text_ (but not ** or __)
_ITALIC_RE = re.compile(r'(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)')
# Inline code: `text`
_CODE_RE = re.compile(r'`([^`\n]+?)`')
# Link: [text](url)
_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def parse_markdown(text: str) -> RichText:
    """解析 Markdown 文本并返回 RichText 对象。

    处理流程:
    1. 分离围栏代码块
    2. 逐行解析（标题、列表、引用、水平线）
    3. 行内格式化（粗体、斜体、代码、链接）
    """
    result = RichText()
    lines = text.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Check for fenced code block start ──
        stripped = line.strip()
        if stripped.startswith("```") and not _is_inline_fence(stripped):
            lang = stripped[3:].strip()
            # Collect all lines until closing ```
            code_lines = []
            i += 1
            while i < len(lines):
                if lines[i].strip() == "```":
                    break
                code_lines.append(lines[i])
                i += 1
            code_text = "\n".join(code_lines)
            _append_code_block(result, code_text, lang or "text")
            i += 1
            continue

        # ── Heading ──
        heading_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            if result.plain:
                result.append("\n")
            # Apply inline formatting to heading text too
            formatted = _parse_inline(heading_text)
            formatted.stylize(STYLE_HEADING)
            result.append(formatted)
            result.append("\n")
            i += 1
            continue

        # ── Horizontal rule ──
        if stripped in ("---", "***", "___", "* * *", "- - -"):
            if result.plain:
                result.append("\n")
            result.append("\u2500" * 40, style=STYLE_DIM)
            result.append("\n")
            i += 1
            continue

        # ── Blockquote ──
        if stripped.startswith("> "):
            quote_text = stripped[2:]
            if result.plain:
                result.append("\n")
            result.append("\u2502 ", style=STYLE_BLOCKQUOTE)
            result.append(_parse_inline(quote_text))
            result.append("\n")
            i += 1
            continue

        # ── Unordered list ──
        ul_match = re.match(r'^(\s*)([-*+])\s+(.*)', line)
        if ul_match:
            indent = len(ul_match.group(1))
            marker = ul_match.group(2)
            item_text = ul_match.group(3)
            prefix = "  " * (indent // 2) + f"{marker} "
            if result.plain:
                result.append("\n")
            result.append(prefix, style=STYLE_LIST_MARKER)
            result.append(_parse_inline(item_text))
            result.append("\n")
            i += 1
            continue

        # ── Ordered list ──
        ol_match = re.match(r'^(\s*)(\d+)\.\s+(.*)', line)
        if ol_match:
            indent = len(ol_match.group(1))
            num = ol_match.group(2)
            item_text = ol_match.group(3)
            prefix = "  " * (indent // 2) + f"{num}. "
            if result.plain:
                result.append("\n")
            result.append(prefix, style=STYLE_LIST_MARKER)
            result.append(_parse_inline(item_text))
            result.append("\n")
            i += 1
            continue

        # ── Table row (detected by | ... | pattern) ──
        if stripped.startswith("|") and stripped.endswith("|"):
            if result.plain:
                result.append("\n")
            result.append(_parse_inline(stripped))
            result.append("\n")
            # Check if next line is a separator row (| --- | --- |)
            if i + 1 < len(lines) and re.match(r'^\|[\s\-:|]+\|$', lines[i + 1].strip()):
                # Render separator as dim
                result.append(lines[i + 1].strip(), style=STYLE_DIM)
                result.append("\n")
                i += 1
            i += 1
            continue

        # ── Normal paragraph ──
        if stripped:
            if result.plain:
                result.append("\n")
            result.append(_parse_inline(line))
            result.append("\n")
        else:
            # Empty line = paragraph break
            if result.plain and not result.plain.endswith("\n\n"):
                result.append("\n")

        i += 1

    return result


def parse_markdown_styled(
    text: str,
    body_style: Optional[str] = None,
    code_style: Optional[str] = STYLE_CODE,
    code_bg: Optional[str] = STYLE_CODE_BG,
    heading_style: Optional[str] = STYLE_HEADING,
    link_style: Optional[str] = STYLE_LINK,
    dim_style: Optional[str] = STYLE_DIM,
    list_marker_style: Optional[str] = STYLE_LIST_MARKER,
) -> RichText:
    """同 parse_markdown()，但允许自定义所有颜色。"""
    global STYLE_CODE, STYLE_CODE_BG, STYLE_HEADING, STYLE_LINK
    global STYLE_DIM, STYLE_LIST_MARKER
    orig = (STYLE_CODE, STYLE_CODE_BG, STYLE_HEADING, STYLE_LINK,
            STYLE_DIM, STYLE_LIST_MARKER)
    STYLE_CODE = code_style or "#6c8cff"
    STYLE_CODE_BG = code_bg or "#1e1e2e"
    STYLE_HEADING = heading_style or "bold #6c8cff"
    STYLE_LINK = link_style or "#6c8cff"
    STYLE_DIM = dim_style or "#888888"
    STYLE_LIST_MARKER = list_marker_style or "#666666"
    try:
        result = parse_markdown(text)
    finally:
        (STYLE_CODE, STYLE_CODE_BG, STYLE_HEADING, STYLE_LINK,
         STYLE_DIM, STYLE_LIST_MARKER) = orig
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_inline_fence(stripped: str) -> bool:
    """Check if ``` appears as inline code rather than block fence.

    Block fence:  ```lang  (starts with ```, no closing ``` on same line)
    Block fence:  ```      (just backticks, no other content)
    Inline code:  ```some inline code```
    """
    if not stripped.startswith("```"):
        return False
    # Pure backticks → closing block fence
    if re.match(r'^`{3,}\s*$', stripped):
        return False
    # ```lang → opening block fence with language
    if not stripped.endswith("```"):
        return False
    # Has ``` at both start and end → inline code
    return True


def _append_code_block(result: RichText, code: str, lang: str) -> None:
    """Append a fenced code block to the result."""
    if result.plain and not result.plain.endswith("\n"):
        result.append("\n")

    # Try syntax highlighting via Rich Syntax
    try:
        # Normalize language name
        lang_map = {
            "py": "python", "js": "javascript", "ts": "typescript",
            "sh": "bash", "yml": "yaml", "rb": "ruby", "rs": "rust",
            "go": "go", "java": "java", "c": "c", "cpp": "c++",
            "css": "css", "html": "html", "json": "json", "sql": "sql",
            "md": "markdown", "yaml": "yaml", "xml": "xml",
            "ps1": "powershell", "pwsh": "powershell",
        }
        lang = lang_map.get(lang.lower(), lang.lower() or "text")
        syntax = Syntax(code, lang, theme="monokai",
                       line_numbers=False, word_wrap=True)
    except Exception:
        # Fallback: render as plain text with code style
        logger.debug("Syntax highlight failed, falling back to plain code text", exc_info=True)
        for line in code.split("\n"):
            result.append(f"  {line}\n", style=STYLE_CODE)
        return

    # Render Syntax to string with ANSI color codes
    from rich.console import Console
    console = Console(width=120)
    with console.capture() as capture:
        console.print(syntax)
    rendered = capture.get()

    if rendered:
        # Prepend code indicator with language label
        result.append(f"  \u250c\u2500 {lang} \u2500\u2500\n", style=STYLE_DIM)
        render_lines = [l for l in rendered.split("\n") if l.strip() or l == ""]
        # Skip trailing empty line from Syntax output
        if render_lines and render_lines[-1] == "":
            render_lines = render_lines[:-1]
        for render_line in render_lines:
            result.append(f"  \u2502 ", style=STYLE_DIM)
            if render_line.strip():
                result.append(RichText.from_ansi(render_line.rstrip()))
            result.append("\n")
        result.append(f"  \u2514\u2500\u2500\u2500\u2500\n", style=STYLE_DIM)


def _parse_inline(text: str) -> RichText:
    """Parse inline markdown formatting: bold, italic, code, links.

    Uses priority-based interval approach: code > bold > italic > link.
    Overlapping formats are resolved by priority (higher priority wins).
    """
    if not text:
        return RichText()

    # ── Step 1: collect all formatting intervals ──
    # Each interval: (full_start, full_end, style, display_text)
    intervals = []

    # Code spans (highest priority) — `text`
    for m in _CODE_RE.finditer(text):
        intervals.append((m.start(), m.end(), STYLE_CODE, m.group(1)))

    # Bold spans — **text** or __text__
    for m in _BOLD_RE.finditer(text):
        for gi in range(1, m.lastindex + 1):
            if m.group(gi) is not None:
                intervals.append((m.start(), m.end(), STYLE_BOLD, m.group(gi)))
                break

    # Italic spans — *text* or _text_
    for m in _ITALIC_RE.finditer(text):
        for gi in range(1, m.lastindex + 1):
            if m.group(gi) is not None:
                intervals.append((m.start(), m.end(), STYLE_ITALIC, m.group(gi)))
                break

    # Link spans — [text](url) → display text only
    for m in _LINK_RE.finditer(text):
        intervals.append((m.start(), m.end(), STYLE_LINK, m.group(1)))

    if not intervals:
        return RichText(text, style=STYLE_TEXT)

    # ── Step 2: sort by start position ──
    intervals.sort(key=lambda x: (x[0], x[1]))

    # ── Step 3: build styled result ──
    result = RichText()
    pos = 0

    for start, end, style, display in intervals:
        # Text before this interval
        if pos < start:
            result.append(text[pos:start], style=STYLE_TEXT)

        # The styled display text
        result.append(display, style=style)
        pos = end

    # Remaining text
    if pos < len(text):
        result.append(text[pos:], style=STYLE_TEXT)

    return result
