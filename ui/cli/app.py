"""
交互式 REPL — prompt_toolkit Application 全屏 UI.

严格仿照 src/screens/REPL.tsx + src/components/PromptInput/PromptInput.tsx
+ src/components/FullscreenLayout.tsx + src/components/messages/*:

终端尺寸处理:
- prompt_toolkit Application(full_screen=True) 自动处理 SIGWINCH
- 每次渲染时通过 self.application.output.width 读取最新宽度
- wrap_lines=True 自适应宽度折行
- VSplit/HSplit 通过 height/width Dimension 控制紧凑布局
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    HSplit, VSplit, Window, WindowAlign, Layout,
    FormattedTextControl, BufferControl, Dimension,
)
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style as PTStyle

logger = logging.getLogger(__name__)

from AutoRUN_v1.commands import execute_command, is_command, RESUME_MARKER
from AutoRUN_v1.state.app_state import get_app_state

# ── Figures (strict match src/constants/figures.ts) ─────────────────────

CH_BLACK_CIRCLE = "\u25cf"   # ● — BLACK_CIRCLE, assistant prefix
CH_POINTER = "\u276f"        # ❯ — figures.pointer, user input prefix
CH_PAUSE = "\u23f8"          # ⏸ — PAUSE_ICON, plan mode
CH_FAST_FWD = "\u23f5\u23f5" # ⏵⏵ — acceptEdits / bypass / auto
CH_LIGHTNING = "\u21af"      # ↯ — LIGHTNING_BOLT
CH_TOOL_PREFIX = "\u23bf"    # ⎿ — DENTISTRY SYMBOL, tool result prefix
CH_DOT = "\u00b7"            # · — middle dot separator

# ── Spinner frames (match SpinnerGlyph — src/components/Spinner/utils.ts) ──
# macOS: · ✢ ✳ ✶ ✻ ✽ — Windows/Linux uses * instead of ✳
# 12-frame back-and-forth animation pattern
_SPINNER_CHARS = ["\u00b7", "\u2722", "\u2733", "\u2736", "\u273b", "\u273d"]
_SPINNER_FRAMES = _SPINNER_CHARS + list(reversed(_SPINNER_CHARS))
_SPINNER_DOT = "\u25cf"  # ● for reduced-motion / paused

# ── Thinking verbs (match src/constants/spinnerVerbs.ts) ──────────────────
# Claude Code shows dynamic verbs like "Flambéing… (thinking)" during
# different assistant phases. We pick random ones each API request.
SPINNER_VERBS = [
    "Actioning", "Thinking", "Doing", "Working",
    "Processing", "Computing", "Calculating", "Composing",
    "Creating", "Generating", "Considering", "Determining",
    "Synthesizing", "Crafting", "Deliberating", "Orchestrating",
    "Forming", "Forging", "Deciphering", "Inferring",
    "Reasoning",
]

# ── Format helpers ───────────────────────────────────────────────────────

def _format_duration(ms: int) -> str:
    """Format elapsed milliseconds like Claude Code: 33s, 1m12s."""
    if ms < 1000:
        return f"{ms}ms"
    total_s = ms // 1000
    if total_s < 60:
        return f"{total_s}s"
    minutes = total_s // 60
    seconds = total_s % 60
    return f"{minutes}m{seconds}s"

def _format_tokens(n: int) -> str:
    """Format token count like Claude Code: 1.1k, 230."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)

# ── XML tool_calls parser ───────────────────────────────────────────────

from AutoRUN_v1.utils.xml_tool_parser import parse_xml_tool_calls as _parse_xml_tool_calls  # noqa: F401

# ── Permission mode symbols (match src/utils/permissions/PermissionMode.ts) ──

PERMISSION_SYMBOLS = {
    "default": "",
    "plan": CH_PAUSE,
    "accept_edits": CH_FAST_FWD,
    "bypass": CH_FAST_FWD,
    "auto": CH_FAST_FWD,
}

PERMISSION_TITLES = {
    "default": "Default",
    "plan": "Plan Mode",
    "accept_edits": "Accept edits",
    "bypass": "Bypass Permissions",
    "auto": "Auto mode",
}

PERMISSION_MODE_STYLES = {
    "default":    "class:footer-mode-d",
    "accept_edits": "class:footer-mode-a",
    "bypass":      "class:footer-mode-b",
    "plan":        "class:footer-mode-p",
    "auto":        "class:footer-mode-w",
}

# ── Colors (strict match dark theme — src/utils/theme.ts) ──────────────

C_CLAUDE        = "#6c8cff"   # accent — matches webUI --accent
C_SUBTLE        = "#505050"   # subtle
C_PROMPT_BORDER = "#888888"   # promptBorder
C_BASH_BORDER   = "#fd5db1"   # bashBorder
C_PLAN_MODE     = "#48968c"   # planMode
C_AUTO_ACCEPT   = "#af87ff"   # autoAccept
C_ERROR         = "#e55555"   # error — matches webUI --red
C_WARNING       = "#f0a030"   # warning — matches webUI --orange
C_SUCCESS       = "#4caf7d"   # success — matches webUI --green
C_SUGGESTION    = "#b1b9f9"   # suggestion
C_TEXT          = "#ffffff"   # text
C_INACTIVE      = "#999999"   # inactive

# ── Style ──────────────────────────────────────────────────────────────

STYLE = PTStyle.from_dict({
    # Message prefixes
    "msg-dot":            f"bold {C_CLAUDE}",
    "msg-pointer":        f"{C_SUBTLE}",
    "msg-user-text":      f"{C_TEXT}",
    "msg-text":           f"{C_TEXT}",
    "msg-text-dim":       C_INACTIVE,
    # Input area
    "input-border":       C_PROMPT_BORDER,
    "input-bash-border":  C_BASH_BORDER,
    "input-prompt":       C_SUBTLE,
    "input-prompt-dim":   C_INACTIVE,
    "input-text":         C_TEXT,
    # Tools
    "tool-name":          f"bold {C_SUGGESTION}",
    "tool-input":         C_INACTIVE,
    "tool-output":        C_INACTIVE,
    "tool-error":         C_ERROR,
    "tool-prefix":        C_INACTIVE,
    # Task strip
    "task-pending":       C_INACTIVE,
    "task-progress":      f"bold {C_SUGGESTION}",
    "task-done":          C_SUCCESS,
    "task-cancelled":     C_INACTIVE,
    "task-strip":         C_INACTIVE,
    # Footer
    "footer":             C_INACTIVE,
    "footer-mode-d":      C_TEXT,
    "footer-mode-a":      C_AUTO_ACCEPT,
    "footer-mode-b":      C_ERROR,
    "footer-mode-p":      C_PLAN_MODE,
    "footer-mode-w":      C_WARNING,
    # Spinner / loading
    "spinner":            C_CLAUDE,
    "thinking-shimmer":   "#b9b9b9",
    # Message types
    "error":              C_ERROR,
    "warn":               C_WARNING,
    "info":               C_INACTIVE,
    "success":            C_SUCCESS,
    "interrupted":        C_ERROR,
    "bold":               "bold",
})


# ── ToolBlock: collapsible tool call representation ─────────────────────

@dataclass
class ToolBlock:
    """A collapsible tool call block, mirroring WebUI's tool-card behavior."""
    id: str
    name: str
    inp_str: str = ""           # formatted input display
    result_str: str = ""        # formatted result display
    result_is_error: bool = False  # whether result is an error
    collapsed: bool = True      # default collapsed
    merged_count: int = 1       # for merged Read blocks
    merged_files: List[str] = field(default_factory=list)
    result_received: bool = False
    short_desc: str = ""        # brief description for collapsed view header

# ── Scroll-aware control ────────────────────────────────────────────────

class _ScrollableControl(FormattedTextControl):
    """FormattedTextControl that dispatches mouse scroll + left-click toggle."""

    def __init__(self, *args, on_scroll=None, on_left_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_scroll = on_scroll
        self._on_left_click = on_left_click

    def mouse_handler(self, mouse_event):
        # Left-click → toggle tool block (check before scroll)
        if (self._on_left_click
                and mouse_event.event_type == MouseEventType.MOUSE_UP
                and mouse_event.button == MouseButton.LEFT):
            result = self._on_left_click(mouse_event)
            if result is not NotImplemented:
                return result

        if self._on_scroll and mouse_event.event_type in (
            MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN,
        ):
            return self._on_scroll(mouse_event)
        return NotImplemented


# ── MessageBuffer ──────────────────────────────────────────────────────

class MessageBuffer:
    """Append-only line buffer consumed by FormattedTextControl callable."""

    def __init__(self):
        self.lines: List[Tuple[str, str]] = []

    def add(self, style: str, text: str):
        self.lines.append((style, text))

    def clear(self):
        self.lines.clear()

    def get(self, offset: int = 0, max_lines: int = 500) -> FormattedText:
        visible = self.lines[offset:offset + max_lines]
        return FormattedText(visible)

    def __len__(self):
        return len(self.lines)


# ── REPLApp ────────────────────────────────────────────────────────────

class REPLApp:
    """AutoRUN interactive REPL — visual match to src/ Claude Code UI.

    Layout (matches FullscreenLayout non-fullscreen path):
      [messages — flexGrow=1 fills all remaining space]
      ────────────  ← top border  (borderStyle="round" no sides)
      ❯ [input]     ← PromptInputModeIndicator + TextInput
      ────────────  ← bottom border (borderBottom)
      [footer]      ← PromptInputFooter (height=1)

    Terminal resize: prompt_toolkit Application handles SIGWINCH automatically.
    All dynamic text callables read self.application.output.width for latest size.
    """

    def __init__(self):
        self.running = True
        self.state = get_app_state()
        self._engine = None
        self._engine_ok = False
        self._perm_handler = None

        # Message buffer
        self.output = MessageBuffer()

        # Dynamic tool call rendering — collapsible tool blocks (like WebUI)
        self._tool_blocks: List[ToolBlock] = []
        self._shown_stream_tool_ids: set = set()
        self._suppressed_task_tool_ids: set = set()

        # Query state
        self.is_query_active = False
        self._cancel_requested = False
        self._streaming: str = ""
        self._streamed_total = 0
        self._task: Optional[asyncio.Task] = None
        self._scroll = 0
        self._user_scrolled = False  # True when user manually scrolls — suppress auto-scroll-to-bottom
        self._turn_count = 0
        self._loading_start_time: float = 0.0  # When current API request started
        self._thinking_verb: str = ""  # Current spinner verb
        self._thinking_status: Optional[str] = None  # 'thinking' | 'streaming' | 'done' | None

        # Input mode: "prompt" | "bash"
        self._input_mode = "prompt"

        # ── Command completer ──────────────────────────────────────────
        command_list = [
            "/help", "/h", "/exit", "/quit", "/q", "/clear",
            "/model", "/status", "/compact",
            "/memory", "/todos", "/tasks",
            "/fast", "/register",
            "/skill", "/skills",
        ]

        # ── Input buffer ───────────────────────────────────────────────
        # multiline=False: Enter → submit, Alt+Enter → insert newline
        self.input_buffer = Buffer(
            history=FileHistory(os.path.expanduser("~/.auto_run_history")),
            multiline=False,
            accept_handler=self._accept_input,
            completer=WordCompleter(command_list, ignore_case=True, sentence=True),
        )
        self.input_buffer.on_text_changed += self._on_input_changed

        # ── Application ────────────────────────────────────────────────
        self.application = Application(
            layout=Layout(self._build_root()),
            key_bindings=self._build_keybindings(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.03,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Layout
    # ═══════════════════════════════════════════════════════════════════

    def _build_root(self):
        """Build root layout matching FullscreenLayout + PromptInput.

        HSplit distribution:
          - Messages Window: no height constraint → fills all remaining space
          - All other Windows: height=1 → compact (Dimension.exact(1))
        """
        border_style = "class:input-border"

        return HSplit([
            # ── Messages (flexGrow=1) ─────────────────────────────────
            Window(
                content=_ScrollableControl(
                    text=self._get_output_text,
                    focusable=False,
                    on_scroll=self._on_mouse_scroll,
                    on_left_click=self._on_mouse_left_click,
                ),
                wrap_lines=True,
                always_hide_cursor=True,
                align=WindowAlign.LEFT,
            ),
            # ── Top border (borderStyle="round" borderLeft=false borderRight=false) ──
            Window(
                content=FormattedTextControl(
                    text=self._get_border_top_text,
                ),
                height=1,
                style=border_style,
                wrap_lines=False,
                dont_extend_height=True,
            ),
            # ── Task strip (shows when tasks exist) ─────────────────────
            Window(
                content=FormattedTextControl(text=self._get_task_strip_text),
                height=Dimension(preferred=1),
                style="class:task-strip",
                wrap_lines=True,
                dont_extend_height=False,
            ),
            # ── Prompt row (❯ + TextInput) ────────────────────────────
            VSplit([
                Window(
                    content=FormattedTextControl(
                        text=self._get_input_prompt_text,
                        focusable=False,
                    ),
                    width=2,
                    align=WindowAlign.LEFT,
                    style="class:input-prompt",
                    dont_extend_width=True,
                ),
                Window(
                    content=BufferControl(buffer=self.input_buffer),
                ),
            ], height=1),
            # ── Bottom border ─────────────────────────────────────────
            Window(
                content=FormattedTextControl(
                    text=self._get_border_bottom_text,
                ),
                height=1,
                style=border_style,
                wrap_lines=False,
                dont_extend_height=True,
            ),
            # ── Footer (PromptInputFooter height={1} overflow="hidden") ──
            Window(
                content=FormattedTextControl(text=self._get_footer_text),
                height=1,
                style="class:footer",
                align=WindowAlign.LEFT,
                wrap_lines=False,
                dont_extend_height=True,
            ),
        ])

    # ═══════════════════════════════════════════════════════════════════
    # Dynamic text callables (called every render frame → reads latest size)
    # ═══════════════════════════════════════════════════════════════════

    def _get_term_width(self) -> int:
        """Read current terminal width (updated on resize by prompt_toolkit)."""
        return self.application.output.get_size().columns

    def _get_output_text(self) -> FormattedText:
        """Render message buffer + streaming text + collapsible tool blocks + status row.

        Tool blocks are rendered as collapsible cards (like WebUI tool-card):
          Collapsed:  ▶ ToolName  — short desc  ✓ N lines
          Expanded:   ▼ ToolName
                         arg1: val1
                         arg2: val2
                        ⎿  result...

        The status/spinner row is always present at the bottom during active
        queries — it changes its label based on the current phase but never
        disappears, so the layout doesn't jump.
        """
        formatted = self.output.get(self._scroll, 500)

        # ── Streaming text (above tool blocks) ───────────────────────────
        if self._streaming:
            formatted.append(("", "\n"))
            formatted.append(("class:msg-dot", f"{CH_BLACK_CIRCLE} "))
            formatted.append(("", self._streaming))

        # ── Collapsible tool blocks ──────────────────────────────────────
        for tb in self._tool_blocks:
            formatted.append(("", "\n"))

            # Icon: ▶ collapsed, ▼ expanded, ● pending (no result yet)
            if not tb.result_received:
                # Pending: blinking dot
                dot_on = int(time.time() * 1000) % 600 < 450
                icon = CH_BLACK_CIRCLE if dot_on else " "
            elif tb.collapsed:
                icon = "\u25b6"  # ▶
            else:
                icon = "\u25bc"  # ▼

            formatted.append(("class:msg-dot", f"  {icon} "))

            if tb.collapsed:
                # ── Collapsed: one line ──
                label = tb.name
                if tb.merged_count > 1:
                    label += f" ({tb.merged_count})"
                formatted.append(("class:tool-name", label))
                if tb.short_desc:
                    desc = tb.short_desc
                    if len(desc) > 80:
                        desc = desc[:80] + "..."
                    formatted.append(("class:tool-input", f"  \u2014 {desc}"))
                # Result summary
                if tb.result_received and tb.result_str:
                    if tb.result_is_error:
                        formatted.append(("class:tool-error", "  \u2717"))
                    else:
                        result_lines = tb.result_str.count("\n") + 1
                        formatted.append(("class:tool-prefix", f"  \u2713 {result_lines} lines"))
            else:
                # ── Expanded: full details ──
                label = tb.name
                if tb.merged_count > 1:
                    label += f" ({tb.merged_count})"
                formatted.append(("class:tool-name", label))
                # Input args
                if tb.inp_str:
                    formatted.append(("class:tool-input", f"\n{tb.inp_str}"))
                # Result
                if tb.result_received and tb.result_str:
                    for i, line in enumerate(tb.result_str.split("\n")):
                        if i == 0:
                            formatted.append(("", "\n"))
                            formatted.append(("class:tool-prefix", f"     {CH_TOOL_PREFIX}  "))
                        else:
                            formatted.append(("", "\n"))
                            formatted.append(("class:tool-prefix", "          "))
                        style = "class:tool-error" if tb.result_is_error else "class:tool-output"
                        formatted.append((style, line))

        # ── Status row — always shown at bottom during active query ────
        if self.is_query_active:
            formatted.append(("", "\n"))
            tick = int(time.time() * 1000) // 120
            spinner_char = _SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)]
            verb = self._thinking_verb or "Thinking"

            elapsed_ms = 0
            if self._loading_start_time > 0:
                elapsed_ms = int((time.time() - self._loading_start_time) * 1000)
            elapsed_str = _format_duration(elapsed_ms)
            tokens = max(0, self._streamed_total // 2)

            # Phase label
            if self._thinking_status == "thinking":
                phase = "(thinking)"
            elif self._tool_blocks:
                last = self._tool_blocks[-1]
                phase = f"(executing {last.name})"
            elif self._streaming:
                phase = "(streaming)"
            elif self._thinking_status == "done":
                phase = ""
            else:
                phase = "(thinking)"

            parts: List[Tuple[str, str]] = []
            parts.append(("class:spinner", f"{spinner_char} {verb}\u2026"))

            status_parts: List[Tuple[str, str]] = []
            if elapsed_ms > 0:
                status_parts.append(("class:footer", elapsed_str))
            if tokens > 0 and self._streamed_total > 20:
                status_parts.append(("class:footer", f"\u2193 {_format_tokens(tokens)} tokens"))
            if phase:
                shimmer = (1 + math.sin(time.time() * math.pi)) / 2
                if shimmer > 0.55:
                    status_parts.append(("class:thinking-shimmer", phase))
                else:
                    status_parts.append(("class:footer", phase))

            if status_parts:
                parts.append(("class:footer", "  ("))
                for i, (sty, txt) in enumerate(status_parts):
                    if i > 0:
                        parts.append(("class:footer", f" {CH_DOT} "))
                    parts.append((sty, txt))
                parts.append(("class:footer", ")"))

            formatted.extend(parts)

        return formatted

    def _get_input_prompt_text(self) -> FormattedText:
        """PromptInputModeIndicator — ❯ or ! prefix."""
        if self._input_mode == "bash":
            return FormattedText([("class:input-bash-border", "! ")])
        if self.is_query_active:
            return FormattedText([("class:input-prompt-dim", f"{CH_POINTER} ")])
        return FormattedText([("class:input-prompt", f"{CH_POINTER} ")])

    def _get_border_top_text(self) -> FormattedText:
        """Top border — flat line, no corners (borderLeft/Right=false)."""
        style = "class:input-bash-border" if self._input_mode == "bash" else "class:input-border"
        return FormattedText([(style, "\u2500" * self._get_term_width())])

    def _get_border_bottom_text(self) -> FormattedText:
        """Bottom border — flat line, no corners."""
        style = "class:input-bash-border" if self._input_mode == "bash" else "class:input-border"
        return FormattedText([(style, "\u2500" * self._get_term_width())])

    def _get_footer_text(self) -> FormattedText:
        """Status bar — single line, no wrapping (matches PromptInputFooter).

        Format: [symbol] mode on · model · Nmsg · Nturn  [hint]
        """
        parts: List[Tuple[str, str]] = []

        # Permission mode pill
        mode = getattr(self.state, 'permission_mode', 'default')
        symbol = PERMISSION_SYMBOLS.get(mode, "")
        title = PERMISSION_TITLES.get(mode, "Default")
        mode_style = PERMISSION_MODE_STYLES.get(mode, "class:footer")

        if symbol:
            parts.append((mode_style, f"{symbol} "))
        parts.append((mode_style, f"{title.lower()} on"))

        parts.append(("class:footer", f" {CH_DOT} "))

        # Model — shorten common names
        model = getattr(self.state, 'model', None) or \
                os.environ.get("AUTORUN_MODEL", "")
        model_short = "???"
        if model:
            model_short = model.replace("deepseek-", "ds-").replace("claude-", "cl-")
        parts.append(("class:footer", model_short))

        # Message count
        msgs = len(self.state.get_messages())
        parts.append(("class:footer", f" {CH_DOT} {msgs}m"))
        if self._turn_count > 0:
            parts.append(("class:footer", f" {CH_DOT} {self._turn_count}t"))

        # Hint (right-side)
        if self.is_query_active:
            parts.append(("class:footer", "  esc to interrupt"))
        else:
            parts.append(("class:footer", "  ?/help  |  Ctrl+C to copy  |  Ctrl+E toggle tool  |  Ctrl+T toggle all"))

        return FormattedText(parts)

    # ═══════════════════════════════════════════════════════════════════
    # Keyboard bindings
    # ═══════════════════════════════════════════════════════════════════

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        def handle_ctrl_c(event):
            if self.is_query_active:
                self._cancel_requested = True
            else:
                self.running = False
                event.app.exit()

        @kb.add("c-d")
        def handle_ctrl_d(event):
            if not self.input_buffer.text.strip():
                self.running = False
                event.app.exit()

        @kb.add("c-l")
        def handle_ctrl_l(event):
            self.output.clear()
            self._scroll = 0

        @kb.add("escape", "enter")
        def handle_alt_enter(event):
            self.input_buffer.insert_text("\n")

        @kb.add("s-tab")
        def handle_shift_tab(event):
            modes = ["default", "accept_edits", "plan", "bypass"]
            current = getattr(self.state, 'permission_mode', 'default')
            try:
                idx = modes.index(current)
                self.state.permission_mode = modes[(idx + 1) % len(modes)]
            except ValueError:
                self.state.permission_mode = "default"

        def _do_pageup():
            step = max(3, self._avail_rows() // 2)
            self._scroll = max(0, self._scroll - step)
            if self._scroll > 0:
                self._user_scrolled = True
            else:
                self._user_scrolled = False
            self.application.invalidate()

        def _do_pagedown():
            buf_len = len(self.output)
            step = max(3, self._avail_rows() // 2)
            max_scroll = max(0, buf_len - 3)
            self._scroll = min(max_scroll, self._scroll + step)
            if self._scroll >= max_scroll:
                self._user_scrolled = False
            else:
                self._user_scrolled = True
            self.application.invalidate()

        @kb.add("pageup")
        def _(event):
            _do_pageup()

        @kb.add("pagedown")
        def _(event):
            _do_pagedown()

        @kb.add("c-e")
        def handle_ctrl_e(event):
            """Toggle collapse/expand of the last (most recent) tool block."""
            if self._tool_blocks:
                last = self._tool_blocks[-1]
                last.collapsed = not last.collapsed
                self.application.invalidate()

        @kb.add("c-t")
        def handle_ctrl_t(event):
            """Toggle collapse/expand of ALL tool blocks."""
            if self._tool_blocks:
                # If all are collapsed, expand all; otherwise collapse all
                all_collapsed = all(tb.collapsed for tb in self._tool_blocks)
                for tb in self._tool_blocks:
                    tb.collapsed = not all_collapsed
                self.application.invalidate()

        return kb

    # ═══════════════════════════════════════════════════════════════════
    # Input mode detection
    # ═══════════════════════════════════════════════════════════════════

    def _on_input_changed(self, _buffer: Buffer):
        """Detect ! prefix → switch to bash mode."""
        text = self.input_buffer.text
        if text.startswith("!") and self._input_mode == "prompt":
            self._input_mode = "bash"
        elif not text.startswith("!") and self._input_mode == "bash":
            self._input_mode = "prompt"

    # ═══════════════════════════════════════════════════════════════════
    # Input processing
    # ═══════════════════════════════════════════════════════════════════

    def _accept_input(self, buff: Buffer) -> bool:
        """Handle user input submission.

        Returns:
            False to clear buffer text after acceptance.
            In prompt_toolkit, returning True means "keep text in buffer".
        """
        raw_text = buff.text
        text = raw_text.strip()

        if not text:
            # Empty input — clear buffer, do nothing
            return False

        # ── /slash commands ─────────────────────────────────────────
        if is_command(text):
            result = execute_command(text, self)
            if result == RESUME_MARKER:
                cmd_name = text.split()[0].lower()
                if cmd_name in ("/resume", "/r"):
                    self._resume_flow_pt()
                elif cmd_name.startswith("/skills"):
                    self._skills_flow_pt(cmd_name)
                return False
            if result:
                for line in result.split("\n"):
                    self.output.add("", line)
            if not self.running:
                self.application.exit()
            return False

        # ── Normal message ──────────────────────────────────────────
        display_text = text

        # Show user input: ❯ prefix (match HighlightedThinkingText)
        # Always start on new line (previous content should end naturally)
        self._scroll_to_bottom()
        self._user_scrolled = False
        self.output.add("", "\n")
        self.output.add("class:msg-pointer", f"{CH_POINTER} ")
        self.output.add("class:msg-user-text", display_text)
        self.application.invalidate()

        # Run async query
        self._task = asyncio.ensure_future(self._process_message(display_text))
        return False

    # ═══════════════════════════════════════════════════════════════════
    # Message processing (match src/query.ts event flow)
    # ═══════════════════════════════════════════════════════════════════

    async def _process_message(self, user_input: str) -> None:
        if not self._engine_ok:
            self._engine_ok = await self._init_engine()
        if not self._engine_ok:
            self.output.add("class:error", "Engine not initialized")
            self.application.invalidate()
            return

        self.is_query_active = True
        self._cancel_requested = False
        self._streaming = ""
        self._streamed_total = 0
        self._thinking_verb = random.choice(SPINNER_VERBS)
        self._loading_start_time = time.time()
        self._thinking_status = "thinking"
        self._tool_blocks: List[ToolBlock] = []
        self._shown_stream_tool_ids: set = set()
        self._suppressed_task_tool_ids: set = set()
        self._user_scrolled = False  # Reset auto-scroll for new query

        # Spinner row is rendered dynamically by _get_output_text().
        # ● prefix is added when text content actually arrives.
        self.application.invalidate()

        # ── Build permission check callback ─────────────────────────
        async def _check_perm(tool_name: str, tool_args: Dict) -> bool:
            ph = self._get_perm_handler()
            if ph.is_tool_always_allowed(tool_name):
                return True
            is_dangerous = ph.is_tool_destructive(tool_name) or \
                           ph.check_sensitive_command(tool_args)
            return await ph.prompt_tool_permission(
                tool_name, tool_args, is_sensitive=is_dangerous,
            )

        # Track whether we've already seen structured tool_use blocks
        # to avoid duplicating XML-parsed blocks with structured blocks
        _seen_tool_ids: set = set()

        try:
            async for event in self._engine.send_message(
                user_input, can_use_tool=_check_perm,
            ):
                if self._cancel_requested:
                    self.output.add("", "\n")
                    self.output.add("class:tool-prefix", f"  {CH_TOOL_PREFIX}  ")
                    self.output.add("class:interrupted", "Interrupted by user")
                    break

                et = event.get("type", "")

                # ── stream_request_start — new API request, cycle verb ─
                if et == "stream_request_start":
                    self._loading_start_time = time.time()
                    self._thinking_status = "thinking"
                    # Pick a different verb for variety
                    prev = self._thinking_verb
                    while self._thinking_verb == prev and len(SPINNER_VERBS) > 1:
                        self._thinking_verb = random.choice(SPINNER_VERBS)
                    self.application.invalidate()
                    continue

                # ── assistant — AI content ──────────────────────────
                if et == "assistant":
                    content = event.get("content", [])
                    if event.get("is_partial"):
                        # Streaming text update
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                txt = b.get("text", "")
                                if len(txt) > self._streamed_total:
                                    self._thinking_status = "streaming"
                                    cleaned, xml_blocks = _parse_xml_tool_calls(txt)
                                    self._streaming = cleaned
                                    self._streamed_total = len(txt)
                                    for tb in xml_blocks:
                                        if tb["id"] not in self._shown_stream_tool_ids:
                                            self._render_tool_use(tb)
                                            self._shown_stream_tool_ids.add(tb["id"])
                                    self.application.invalidate()
                    else:  # Complete message

                        # Parse XML tool calls from the complete message's text blocks.
                        # (streaming text was already cleaned during partial phase, so
                        #  we re-parse from the original content for tool extraction.)
                        all_xml_blocks = []
                        text_for_buffer = ""
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                raw_text = b.get("text", "")
                                cleaned, xml_blocks = _parse_xml_tool_calls(raw_text)
                                text_for_buffer += cleaned
                                all_xml_blocks.extend(xml_blocks)

                        # If no text was found in content blocks, fall back to streaming text
                        if not text_for_buffer and self._streaming:
                            cleaned, _ = _parse_xml_tool_calls(self._streaming)
                            text_for_buffer = cleaned

                        if text_for_buffer:
                            self.output.add("", "\n")
                            self.output.add("class:msg-dot", f"{CH_BLACK_CIRCLE} ")
                            self.output.add("", text_for_buffer)

                        self._streaming = ""
                        self._streamed_total = 0
                        self._thinking_status = "done"

                        # Render XML-parsed tool calls
                        for tb in all_xml_blocks:
                            if tb["id"] not in _seen_tool_ids:
                                self._render_tool_use(tb)
                                _seen_tool_ids.add(tb["id"])

                        # Process structured content blocks
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            bt = b.get("type", "")
                            if bt == "tool_use":
                                tid = b.get("id", "")
                                if tid not in _seen_tool_ids:
                                    _seen_tool_ids.add(tid)
                                    self._render_tool_use(b)
                            elif bt == "tool_result":
                                self._render_tool_result(b)

                # ── user — tool result ──────────────────────────────
                elif et == "user":
                    for b in event.get("content", []):
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            self._render_tool_result(b)

                # ── retry_attempt — API 重试 ──────────────────
                elif et == "retry_attempt":
                    self.output.add("", "\n")
                    self.output.add("class:warn",
                        f"  API call failed, retrying "
                        f"({event.get('attempt', '?')}/{event.get('max_retries', '?')})..."
                    )

                # ── error ───────────────────────────────────────────
                elif et == "error":
                    self.output.add("", "\n")
                    self.output.add("class:error",
                                    f"  Error: {event.get('error', 'Unknown error')}")

                # ── attachment ──────────────────────────────────────
                elif et == "attachment":
                    a = event.get("attachment", {})
                    at = a.get("type", "")
                    if at == "auto_compact_suggestion":
                        self.output.add("", "\n")
                        self.output.add("class:warn",
                            "Context limit approaching. Consider /compact")

                # ── terminal ────────────────────────────────────────
                elif et == "terminal":
                    break

                self.application.invalidate()

        except Exception as e:
            self.output.add("class:error", f"  Error: {e}")
        finally:
            self.is_query_active = False
            self._streaming = ""
            self._streamed_total = 0
            self._thinking_status = None
            self._tool_blocks.clear()
            self._turn_count += 1
            self._scroll_to_bottom()
            self.application.invalidate()

    # ═══════════════════════════════════════════════════════════════════
    # Tool rendering (match AssistantToolUseMessage + MessageResponse)
    # ═══════════════════════════════════════════════════════════════════

    def _render_tool_use(self, block: Dict[str, Any]):
        """Queue tool call as a collapsible ToolBlock (mirrors WebUI tool-card).

        - Read tool calls are merged when consecutive
        - All tools default to collapsed (one-line display)
        - Task tools are suppressed (shown in task strip instead)
        """
        from AutoRUN_v1.tools.task_tool import TASK_TOOL_NAMES
        name = block.get("name", "unknown")
        if name in TASK_TOOL_NAMES:
            tool_id = block.get("id", name)
            self._suppressed_task_tool_ids.add(tool_id)
            return
        name = block.get("name", "unknown")
        tool_id = block.get("id", name)
        inp = block.get("input", {})

        # Build short description (mirrors WebUI's smart short desc logic)
        short_desc = ""
        is_read = "read" in name.lower() or "Read" in name
        is_bash = name.lower() in ("bash", "bash_tool", "bash tool")

        cmd = inp.get("command", "")
        file_path = inp.get("file_path", "")

        if is_bash:
            short_desc = cmd[:60] if cmd else ""
        elif file_path:
            short_desc = file_path.split("\\").pop().split("/").pop() or file_path
        elif inp.get("description"):
            short_desc = str(inp.get("description", ""))[:60]
        else:
            keys = [k for k in inp if k not in ("description", "file_path") and inp[k]]
            if keys:
                v = str(inp[keys[0]])
                short_desc = v[:60] + ("..." if len(v) > 60 else "")

        # Format input args for expanded view
        arg_lines = []
        for k, v in inp.items():
            v_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            if len(v_str) > 100:
                v_str = v_str[:100] + "..."
            arg_lines.append(f"     {k}: {v_str}")
        inp_str = "\n".join(arg_lines) if arg_lines else ""

        # ── Read merging (mirrors WebUI) ──
        if is_read and self._tool_blocks:
            last = self._tool_blocks[-1]
            # Check if last block is also a Read (by name containing 'read'/'Read')
            if "read" in last.name.lower() or "Read" in last.name:
                last.merged_count += 1
                if file_path and file_path not in last.merged_files:
                    last.merged_files.append(file_path)
                # Update short_desc to show the latest file
                last.short_desc = short_desc
                # Append this file's args to the input display
                if inp_str:
                    last.inp_str = (last.inp_str or "") + f"\n{inp_str}"
                return

        block = ToolBlock(
            id=tool_id,
            name=name,
            inp_str=inp_str,
            short_desc=short_desc,
            collapsed=True,  # default collapsed
            merged_files=[file_path] if is_read and file_path else [],
        )
        self._tool_blocks.append(block)

    def _render_tool_result(self, block: Dict[str, Any]):
        """Append tool result to the matching ToolBlock (mirrors WebUI tool-result).

        Marks the block as having received its result, and updates the
        collapsed/expanded display accordingly.
        """
        # Skip task tools — rendered in the task strip instead
        tool_id = block.get("tool_use_id", "")
        if tool_id in self._suppressed_task_tool_ids:
            self._suppressed_task_tool_ids.discard(tool_id)
            # Clean up any tool block with this ID
            self._tool_blocks = [
                t for t in self._tool_blocks if t.id != tool_id
            ]
            return

        content = str(block.get("content", ""))
        content = content.replace("\t", "    ")
        is_err = block.get("is_error", False)

        # Truncate long output by lines
        lines = content.split("\n")
        if len(lines) > 5:
            shown = lines[:5]
            hidden_count = len(lines) - 5
            lines = shown + [f"... ({hidden_count} more lines)"]
        result_str = "\n".join(lines)

        # Find and update the matching tool block
        for tb in self._tool_blocks:
            if tb.id == tool_id:
                tb.result_str = result_str
                tb.result_is_error = is_err
                tb.result_received = True
                return

        # If no matching block found (e.g., result arrived before tool_use),
        # flush existing blocks to buffer and show result as standalone
        if not self._tool_blocks:
            # Standalone result
            style = "class:tool-error" if is_err else "class:tool-output"
            for i, line in enumerate(lines):
                if i == 0:
                    self.output.add("", "\n")
                    self.output.add("class:tool-prefix", f"  {CH_TOOL_PREFIX}  ")
                    self.output.add(style, line)
                else:
                    self.output.add("", "\n")
                    self.output.add(style, f"     {line}")

    # ═══════════════════════════════════════════════════════════════════
    # Task strip (rendered above input bar)
    # ═══════════════════════════════════════════════════════════════════

    def _get_task_strip_text(self) -> FormattedText:
        """Render the task list as a checkbox list above the input bar."""
        from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display
        tasks = get_all_tasks_for_display(self.state)

        if not tasks:
            return FormattedText([])

        icon_styles = {
            "in_progress": ("class:task-progress", "\u25c9"),  # ◉
            "pending": ("class:task-pending", "\u25cb"),        # ○
            "completed": ("class:task-done", "\u2611"),         # ☑
            "cancelled": ("class:task-cancelled", "\u2612"),    # ☒
        }

        parts: List[Tuple[str, str]] = []
        for t in tasks:
            status = t.get("status", "pending")
            style, icon = icon_styles.get(status, icon_styles["pending"])
            label = t.get("label", "")[:80]
            parts.append((style, f"  {icon} {label}\n"))

        return FormattedText(parts)

    # ═══════════════════════════════════════════════════════════════════
    # Resume flow (prompt_toolkit)
    # ═══════════════════════════════════════════════════════════════════

    def _resume_flow_pt(self) -> None:
        """Resume flow using radiolist_dialog for prompt_toolkit UI."""
        from prompt_toolkit.shortcuts import radiolist_dialog

        # Step 1: choose scope
        result = radiolist_dialog(
            title="选择恢复方式",
            text="↑↓ 移动, Enter 确认, Esc 退出",
            values=[
                ("project", "当前项目目录下的对话"),
                ("all", "所有项目目录下的对话"),
                ("search", "AI 智能搜索"),
            ],
        ).run()

        if result is None:
            return  # cancelled

        if result == "search":
            self._resume_search_pt()
            return

        # Step 2: show conversations
        self._resume_list_pt(result)

    def _resume_list_pt(self, action: str) -> None:
        from prompt_toolkit.shortcuts import radiolist_dialog
        from AutoRUN_v1.services.conversations import list_conversations

        cwd = os.getcwd() if action == "project" else None
        try:
            conversations = list_conversations(cwd_filter=cwd)
        except RuntimeError as e:
            self.output.add("", "\n")
            self.output.add("class:error", f"加载对话列表失败：{e}")
            self.application.invalidate()
            return

        if not conversations:
            self.output.add("", "\n")
            self.output.add("class:info", "没有找到已保存的对话。")
            self.application.invalidate()
            return

        values = []
        for conv in conversations:
            sid = conv.get("session_id", "")[:12]
            updated = conv.get("updated_at", "")[:16].replace("T", " ")
            count = conv.get("message_count", 0)
            preview = conv.get("preview", "")[:50]
            values.append((sid, f"[{updated}] {count}条 — {preview}"))

        session_id = radiolist_dialog(
            title="选择对话",
            text="↑↓ 移动, Enter 加载, Esc 返回",
            values=values,
        ).run()

        if session_id:
            self._resume_load_pt(session_id)

    def _resume_search_pt(self) -> None:
        from prompt_toolkit.shortcuts import input_dialog, radiolist_dialog
        from AutoRUN_v1.services.conversations import search_conversations

        query = input_dialog(
            title="AI 搜索对话",
            text="输入搜索关键词:",
        ).run()

        if not query or not query.strip():
            return

        try:
            results = search_conversations(query.strip())
        except RuntimeError as e:
            self.output.add("", "\n")
            self.output.add("class:error", f"搜索对话失败：{e}")
            self.application.invalidate()
            return

        if not results:
            self.output.add("", "\n")
            self.output.add("class:info", f"未找到包含 '{query}' 的对话。")
            self.application.invalidate()
            return

        values = []
        for conv in results:
            sid = conv.get("session_id", "")[:12]
            updated = conv.get("updated_at", "")[:16].replace("T", " ")
            count = conv.get("message_count", 0)
            preview = conv.get("preview", "")[:50]
            project = conv.get("project_name", "")
            values.append((sid, f"[{updated}] {count}条 [{project}] — {preview}"))

        session_id = radiolist_dialog(
            title=f"搜索结果: {query}",
            text="↑↓ 移动, Enter 加载, Esc 返回",
            values=values,
        ).run()

        if session_id:
            self._resume_load_pt(session_id)

    def _resume_load_pt(self, session_id: str) -> None:
        from AutoRUN_v1.services.conversations import restore_to_state, load_conversation
        from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

        try:
            data = load_conversation(session_id)
        except RuntimeError as e:
            self.output.add("", "\n")
            self.output.add("class:error", str(e))
            self.application.invalidate()
            return
        if not data:
            self.output.add("", "\n")
            self.output.add("class:error", "无法加载对话。")
            self.application.invalidate()
            return

        ok = restore_to_state(session_id, self.state)
        if not ok:
            self.output.add("", "\n")
            self.output.add("class:error", "恢复对话失败。")
            self.application.invalidate()
            return

        clear_skills_cache()
        disabled = self.state._get_disabled_skills()
        discover_skills(refresh=True, disabled_skills=disabled)
        register_skills_to_tool(disabled_skills=disabled)

        messages = self.state.get_messages()
        project = data.get("project_name", "")
        model = data.get("model", "")
        self.output.add("", "\n")
        self.output.add("class:info",
            f"对话已恢复: {project} ({len(messages)}条消息, {model})"
        )
        self.application.invalidate()

    def _skills_flow_pt(self, cmd_name: str) -> None:
        from AutoRUN_v1.skills.loader import discover_skills, clear_skills_cache, register_skills_to_tool

        all_skills = discover_skills(refresh=True)
        disabled = self.state._get_disabled_skills()

        if not all_skills:
            self.output.add("", "\n")
            self.output.add("class:info", "没有已加载的 skill。")
            self.application.invalidate()
            return

        # Just show status as text output
        lines = ["\nSkill 状态:"]
        for name in sorted(all_skills.keys()):
            status = "✗ 已禁用" if name in disabled else "✓ 已启用"
            skill_def = all_skills[name]
            desc = skill_def.get("description", "")
            source = skill_def.get("_source", "unknown")
            source_label = {"bundled": "[内置]", "user": "[用户]", "project": "[项目]"}.get(source, f"[{source}]")
            lines.append(f"  {status} {source_label} {name}")
            if desc:
                lines.append(f"      {desc}")

        for line in lines:
            self.output.add("", line)
        self.application.invalidate()

    # Engine
    # ═══════════════════════════════════════════════════════════════════

    async def _init_engine(self) -> bool:
        try:
            from AutoRUN_v1.query_engine import QueryEngine
            self._engine = QueryEngine(self.state)
            await self._engine.initialize()
            return True
        except Exception as e:
            self.output.add("class:error", f"Engine error: {e}")
            return False

    def _get_perm_handler(self):
        if self._perm_handler is None:
            from AutoRUN_v1.ui.cli.permissions import get_permission_handler
            self._perm_handler = get_permission_handler()
        return self._perm_handler

    def _avail_rows(self) -> int:
        """Messages area height = terminal rows - 4 fixed rows."""
        return self.application.renderer.output.get_size().rows - 4

    def _scroll_to_bottom(self):
        """Position to show the last N entries, where N = available rows."""
        if not self._user_scrolled:
            self._scroll = max(0, len(self.output) - self._avail_rows())

    def _on_mouse_scroll(self, mouse_event):
        """Handle mouse wheel via UIControl.mouse_handler."""
        step = max(1, self._avail_rows() // 6)
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._scroll = max(0, self._scroll - step)
            self._user_scrolled = True
            self.application.invalidate()
        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            max_scroll = max(0, len(self.output) - 3)
            self._scroll = min(max_scroll, self._scroll + step)
            if self._scroll >= max_scroll:
                self._user_scrolled = False
            else:
                self._user_scrolled = True
            self.application.invalidate()
        return None  # Consumed

    def _on_mouse_left_click(self, mouse_event):
        """Left-click on output area — toggle the last tool block if any exists."""
        if self._tool_blocks:
            last = self._tool_blocks[-1]
            last.collapsed = not last.collapsed
            self.application.invalidate()
            return None  # Consumed
        return NotImplemented

    def request_cancel(self):
        self._cancel_requested = True

    # ═══════════════════════════════════════════════════════════════════
    # Startup
    # ═══════════════════════════════════════════════════════════════════

    async def run(self) -> None:
        """Launch REPL application."""
        # API key check
        if os.environ.get("AUTORUN_DEV") != "1":
            from AutoRUN_v1.utils.config import get_api_key, get_server_url
            if not get_api_key():
                self.output.add("class:warn",
                    "No API key detected. Run /register [nickname] to register.")
                self.output.add("class:info",
                    f"Server: {get_server_url()}")
                self.output.add("", "\n")

        # Welcome banner
        self.output.add("class:msg-dot", f"{CH_BLACK_CIRCLE} AutoRUN v1.0")
        self.output.add("class:msg-text-dim", f"  /help for commands  |  Ctrl+C to exit  |  Ctrl+E toggle tool  |  Ctrl+T expand/collapse all")
        self.output.add("", "\n")

        # Init engine (may take a moment)
        self._engine_ok = await self._init_engine()

        try:
            await self.application.run_async()
        finally:
            if self._task and not self._task.done():
                self._task.cancel()
