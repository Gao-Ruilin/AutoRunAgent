"""
交互式 REPL — Textual 全屏应用.

严格仿照 src/screens/REPL.tsx + src/components/:
- FullscreenLayout: ScrollBox(flexGrow=1) + PromptInput(flexShrink=0)
- Messages: AssistantTextMessage, AssistantToolUseMessage, MessageResponse
- PromptInput: PromptInputModeIndicator + TextInput + borders + footer

架构:
  AutoRUNApp(App)
    VerticalScroll(#messages-scroll)  ← 滚动容器
      MessageContent(#messages)       ← 渲染所有消息内容
    Static(#border-top)              ← 边框 (height=1)
    Horizontal(#input-row)           ← 输入行 (height=1)
      Static(#prompt-indicator)      ← ❯ 或 ! 前缀 (width=2)
      Input(#text-input)             ← 用户输入 (1fr)
    Static(#border-bottom)           ← 边框 (height=1)
    Static(#footer)                  ← 状态栏 (height=1)
"""

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Key, MouseDown
from textual.widgets import Static, TextArea

logger = logging.getLogger(__name__)



from AutoRUN_v1.commands import execute_command, is_command, RESUME_MARKER
from AutoRUN_v1.state.app_state import get_app_state
from AutoRUN_v1.ui.cli.suggestions import (
    SuggestionBar,
    get_command_suggestions,
    get_file_suggestions,
)
from AutoRUN_v1.utils.markdown import parse_markdown, _parse_inline


# ── Figures ────────────────────────────────────────────────────────────────

CH_BLACK_CIRCLE = "\u25cf"     # ● BLACK_CIRCLE, assistant prefix
CH_POINTER = "\u276f"          # ❯ figures.pointer, user input prefix
CH_PAUSE = "\u23f8"            # ⏸ PAUSE_ICON, plan mode
CH_FAST_FWD = "\u23f5\u23f5"   # ⏵⏵ acceptEdits / bypass
CH_TOOL_PREFIX = "\u23bf"      # ⎿ DENTISTRY SYMBOL, tool result prefix
CH_DOT = "\u00b7"              # · middle dot separator

# ── Spinner frames ─────────────────────────────────────────────────────────

_SPINNER_CHARS = ["\u00b7", "\u2722", "\u2733", "\u2736", "\u273b", "\u273d"]
_SPINNER_FRAMES = _SPINNER_CHARS + list(reversed(_SPINNER_CHARS))

# ── Colors ─────────────────────────────────────────────────────────────────

C_CLAUDE       = "#6c8cff"
C_SUBTLE       = "#505050"
C_BORDER       = "#888888"
C_BASH_BORDER  = "#fd5db1"
C_PLAN_MODE    = "#48968c"
C_AUTO_ACCEPT  = "#af87ff"
C_ERROR        = "#e55555"
C_WARNING      = "#f0a030"
C_SUCCESS      = "#4caf7d"
C_SUGGESTION   = "#b1b9f9"
C_TEXT         = "#ffffff"
C_INACTIVE     = "#999999"

# ── Permission mode symbols ────────────────────────────────────────────────

PERMISSION_SYMBOLS = {
    "default": "", "plan": CH_PAUSE,
    "accept_edits": CH_FAST_FWD, "bypass": CH_FAST_FWD, "auto": CH_FAST_FWD,
}
PERMISSION_TITLES = {
    "default": "Default", "plan": "Plan Mode", "accept_edits": "Accept edits",
    "bypass": "Bypass Permissions", "auto": "Auto mode",
}

# ── Thinking verbs ─────────────────────────────────────────────────────────

SPINNER_VERBS = [
    "Actioning", "Thinking", "Doing", "Working",
    "Processing", "Computing", "Calculating", "Composing",
    "Creating", "Generating", "Considering", "Determining",
    "Synthesizing", "Crafting", "Deliberating", "Orchestrating",
    "Forming", "Forging", "Deciphering", "Inferring",
    "Reasoning", "Executing",
]

# ── XML tool_calls parser ──────────────────────────────────────────────────

from AutoRUN_v1.utils.xml_tool_parser import parse_xml_tool_calls as _parse_xml_tool_calls  # noqa: F401


# ── ToolBlock: collapsible tool call representation ──────────────────────

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


def _format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    total_s = ms // 1000
    if total_s < 60:
        return f"{total_s}s"
    return f"{total_s // 60}m{total_s % 60}s"


def _format_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


# ═══════════════════════════════════════════════════════════════════════════
# MessageContent — renders the full message list as Rich Text
# ═══════════════════════════════════════════════════════════════════════════

class MessageContent(Static):
    """Renders message history, streaming text, and spinner status.

    Stores state in plain attributes; _msg_refresh() rebuilds and updates
    the Static content. No render() or refresh() override — avoids Textual
    rendering recursion issues.
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._mc_entries: List[Dict[str, Any]] = []
        self._mc_streaming: str = ""
        self._mc_spinner: str = ""
        self._mc_status_text: str = ""
        self._mc_tool_blocks: List[ToolBlock] = []

    def add(self, kind: str, content: str = "", **extra):
        self._mc_entries.append({"kind": kind, "content": content, **extra})

    def clear_all(self):
        self._mc_entries.clear()
        self._mc_streaming = ""
        self._mc_spinner = ""
        self._mc_status_text = ""
        self._mc_tool_blocks.clear()

    # Properties so external code can use msg.spinner = "x" etc.
    @property
    def streaming(self): return self._mc_streaming
    @streaming.setter
    def streaming(self, v): self._mc_streaming = v

    @property
    def spinner(self): return self._mc_spinner
    @spinner.setter
    def spinner(self, v): self._mc_spinner = v

    @property
    def status_text(self): return self._mc_status_text
    @status_text.setter
    def status_text(self, v): self._mc_status_text = v

    def _mc_apply(self):
        """Build RichText from state and update Static content.

        Does NOT override refresh() — avoids recursion with Static.update().
        All call sites that need re-render must call this instead of refresh().
        """
        t = self._build_rich_text()
        self.update(t)

    def _build_rich_text(self) -> RichText:
        """Build the RichText content from current state.

        Entries are rendered in order. Tool blocks inside entries
        (kind="tool_block") are rendered inline at their correct position.
        Tool blocks NOT referenced by entries (e.g. from streaming XML
        extraction) are rendered at the end as before.
        """
        t = RichText()
        rendered_tool_ids: set = set()

        def _render_tool_block(tb: ToolBlock) -> None:
            """Render a single collapsible tool block into the RichText."""
            t.append("\n")
            if not tb.result_received:
                icon = CH_BLACK_CIRCLE
            elif tb.collapsed:
                icon = "\u25b6"  # ▶
            else:
                icon = "\u25bc"  # ▼

            t.append(f"  {icon} ", style=f"bold {C_CLAUDE}")

            if tb.collapsed:
                label = tb.name
                if tb.merged_count > 1:
                    label += f" ({tb.merged_count})"
                t.append(label, style=f"bold {C_SUGGESTION}")
                if tb.short_desc:
                    desc = tb.short_desc
                    if len(desc) > 80:
                        desc = desc[:80] + "..."
                    t.append(f"  \u2014 {desc}", style=C_INACTIVE)
                if tb.result_received and tb.result_str:
                    if tb.result_is_error:
                        t.append("  \u2717", style=C_ERROR)
                    else:
                        result_lines = tb.result_str.count("\n") + 1
                        t.append(f"  \u2713 {result_lines} lines", style=C_INACTIVE)
            else:
                label = tb.name
                if tb.merged_count > 1:
                    label += f" ({tb.merged_count})"
                t.append(label, style=f"bold {C_SUGGESTION}")
                if tb.inp_str:
                    t.append(f"\n{tb.inp_str}", style=C_INACTIVE)
                if tb.result_received and tb.result_str:
                    for i, line in enumerate(tb.result_str.split("\n")):
                        if i == 0:
                            t.append(f"\n     {CH_TOOL_PREFIX}  ", style=C_INACTIVE)
                        else:
                            t.append("\n          ", style=C_INACTIVE)
                        sty = C_ERROR if tb.result_is_error else C_INACTIVE
                        t.append(line, style=sty)

        for e in self._mc_entries:
            kind = e.get("kind", "text")
            content = e.get("content", "")
            if kind == "blank":
                t.append("\n")
            elif kind == "pointer":
                t.append(f"\n{CH_POINTER} ", style=C_SUBTLE)
                t.append(content, style=C_TEXT)
            elif kind == "assistant":
                t.append(f"\n{CH_BLACK_CIRCLE} ", style=f"bold {C_CLAUDE}")
                try:
                    md_parsed = parse_markdown(content)
                    t.append(md_parsed)
                except Exception:
                    logger.debug("Markdown parse failed in message render", exc_info=True)
                    t.append(content, style=C_TEXT)
            elif kind == "tool_block":
                tool_id = e.get("tool_id")
                if tool_id:
                    for tb in self._mc_tool_blocks:
                        if tb.id == tool_id:
                            _render_tool_block(tb)
                            rendered_tool_ids.add(tool_id)
                            break
                # Fallback: render placeholder if tool block not found
                if tool_id not in rendered_tool_ids:
                    t.append(f"\n  [{e.get('tool_name', 'tool')}]", style=C_INACTIVE)
            elif kind == "tool_result":
                sty = C_ERROR if e.get("is_error") else C_INACTIVE
                lines = content.split("\n")
                for i, line in enumerate(lines):
                    if i == 0:
                        t.append(f"\n  {CH_TOOL_PREFIX}  ", style=C_INACTIVE)
                        t.append(line, style=sty)
                    else:
                        t.append(f"\n     {line}", style=sty)
            elif kind == "interrupted":
                t.append(f"\n  {CH_TOOL_PREFIX}  ", style=C_INACTIVE)
                t.append(content, style=C_ERROR)
            elif kind == "raw":
                sty = e.get("style", C_TEXT)
                t.append(f"\n{content}", style=sty)
            elif kind == "error":
                t.append(f"\n  {content}", style=C_ERROR)
            elif kind == "warn":
                t.append(f"\n  {content}", style=C_WARNING)
            elif kind == "info":
                t.append(f"\n{content}", style=C_INACTIVE)
            else:
                sty = e.get("style", C_TEXT)
                t.append(f"\n{content}", style=sty)

        # ── Render remaining tool blocks (those not in _mc_entries) ──
        for tb in self._mc_tool_blocks:
            if tb.id not in rendered_tool_ids:
                _render_tool_block(tb)

        if self._mc_streaming:
            t.append(f"\n{CH_BLACK_CIRCLE} ", style=f"bold {C_CLAUDE}")
            try:
                t.append(_parse_inline(self._mc_streaming))
            except Exception:
                logger.debug("Inline parse failed in streaming render", exc_info=True)
                t.append(self._mc_streaming, style=C_TEXT)

        if self._mc_spinner:
            t.append("\n")
            t.append(self._mc_spinner + " ", style=C_CLAUDE)
        if self._mc_status_text:
            if not self._mc_spinner:
                t.append("\n")
            t.append(self._mc_status_text, style=C_INACTIVE)

        return t

# ═══════════════════════════════════════════════════════════════════════════
# SelectionOverlay — keyboard-navigable selection panel
# ═══════════════════════════════════════════════════════════════════════════

class SelectionOverlay(Static):
    """A keyboard-navigable selection panel overlaid on the chat area.

    Used for /resume conversation selection, /skills-status skill toggle, etc.
    Unified interaction: ↑↓ to move, Enter to confirm, Esc to cancel.
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._visible = False
        self._title = ""
        self._items: List[Dict[str, Any]] = []  # [{label, detail, data}]
        self._selected_index = 0
        self._mode = ""  # "options" | "conversations" | "skills" | "search"
        self._search_query = ""
        self._on_select: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._on_cancel: Optional[Callable[[], Any]] = None

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self, title: str, items: List[Dict[str, Any]], mode: str = "options",
             on_select: Optional[Callable[[Dict[str, Any]], Any]] = None,
             on_cancel: Optional[Callable[[], Any]] = None):
        self._visible = True
        self._title = title
        self._items = items
        self._selected_index = 0
        self._mode = mode
        self._search_query = ""
        self._on_select = on_select
        self._on_cancel = on_cancel
        self.styles.display = "block"
        self._refresh()

    def hide(self):
        self._visible = False
        self._items = []
        self._on_select = None
        self._on_cancel = None
        self.styles.display = "none"
        self.update("")

    def move_up(self):
        if self._selected_index > 0:
            self._selected_index -= 1
            self._refresh()

    def move_down(self):
        if self._selected_index < len(self._items) - 1:
            self._selected_index += 1
            self._refresh()

    def select_current(self) -> Optional[Dict[str, Any]]:
        if 0 <= self._selected_index < len(self._items):
            return self._items[self._selected_index]
        return None

    def cancel(self):
        cb = self._on_cancel
        self.hide()
        if cb:
            cb()

    def _refresh(self):
        if not self._visible:
            self.update("")
            return

        max_items = 12
        lines = []
        lines.append(f"[bold blue]{self._title}[/]  (↑↓ 移动, Enter 确认, Esc 退出)")
        lines.append("─" * 60)

        start = max(0, self._selected_index - max_items + 3)
        end = min(len(self._items), start + max_items)
        if start > 0:
            lines.append(f"  ... ({start} more above)")

        for i in range(start, end):
            item = self._items[i]
            prefix = "[bold reverse green]>[/] " if i == self._selected_index else "  "
            label = item.get("label", "")
            detail = item.get("detail", "")
            line = f"{prefix}{label}"
            if detail:
                line += f"  [dim]{detail}[/]"
            lines.append(line)

        if end < len(self._items):
            lines.append(f"  ... ({len(self._items) - end} more below)")

        if self._mode == "search":
            lines.append("─" * 60)
            sq = self._search_query or "_"
            lines.append(f"搜索: [bold]{sq}[/]")

        lines.append("─" * 60)
        lines.append("[dim]↑↓ 导航  |  Enter 选择  |  Esc 退出[/]")

        self.update("\n".join(lines))


# ═══════════════════════════════════════════════════════════════════════════
# AutoRUNApp — main Textual Application
# ═══════════════════════════════════════════════════════════════════════════

class AutoRUNApp(App):
    """AutoRUN interactive REPL — Textual-based visual match to Claude Code UI.

    Layout (matches FullscreenLayout + PromptInput):
      VerticalScroll(#messages-scroll)  — flexGrow=1
        MessageContent(#messages)       — auto-height content
      Static(#border-top)              — height=1
      Horizontal(#input-row)           — height=1
      Static(#border-bottom)           — height=1
      Static(#footer)                  — height=1
    """

    CSS = """
    Screen {
        layers: base;
        color: #ffffff;
    }

    #messages-scroll {
        height: 1fr;
        scrollbar-size: 0 0;
    }

    #messages {
        height: auto;
    }

    #border-top {
        height: 1;
        color: #888888;
    }

    #border-bottom {
        height: 1;
        color: #888888;
    }

    #continuation-display {
        height: 0;
        padding: 0 2;
        color: #888888;
    }

    #task-strip {
        height: 0;
        padding: 0 2;
        margin: 0;
        overflow: hidden;
    }

    #input-row {
        height: auto;
        min-height: 1;
    }

    #prompt-indicator {
        width: 2;
        height: auto;
        min-height: 1;
        content-align: left top;
    }

    #text-input {
        width: 3;
        height: auto;
        min-height: 1;
        max-height: 10;
        border: none;
        background: transparent;
        padding: 0 1;
        color: #ffffff;
    }

    #text-input:focus {
        border: none;
        background: transparent;
        color: #ffffff;
    }

    #suggestions {
        height: 0;
        max-height: 6;
        padding: 0 2;
        color: #888888;
    }

    #footer {
        height: 1;
        color: $text-disabled;
    }

    #selection-overlay {
        height: auto;
        max-height: 20;
        padding: 0 1;
        margin: 0 1;
        background: $surface;
        border: solid $accent;
        color: $text;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_quit", "Interrupt / Quit", show=False),
        Binding("escape", "dismiss_suggestions", "Dismiss", show=False),
        Binding("tab", "complete_suggestion", "Complete", show=False, priority=True),
        Binding("enter", "submit_input", "Submit", show=False, priority=True),
        Binding("ctrl+e", "toggle_tool", "Toggle tool", show=False),
        Binding("ctrl+t", "toggle_all_tools", "Toggle all tools", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._state = get_app_state()
        self._engine = None
        self._engine_ok = False
        self._perm_handler = None

        # Query state
        self.is_query_active = False
        self._cancel_requested = False
        self._streamed_total = 0
        self._turn_count = 0
        self._loading_start_time: float = 0.0
        self._thinking_verb: str = ""
        self._thinking_status: Optional[str] = None
        self._tool_blocks: List[ToolBlock] = []
        self._shown_stream_tool_ids: set = set()
        self._suppressed_task_tool_ids: set = set()
        self._input_mode: str = "prompt"

        # Multi-line continuation buffer (bash-style \ at end of line)
        self._continuation_buffer: str = ""
        self._multiline_mode: str = os.environ.get(
            "AUTORUN_MULTILINE", "backslash")  # "backslash" | "shift_enter" | "off"

        # Selection overlay state
        self._selection_mode = False
        self._selection_overlay: Optional[SelectionOverlay] = None

        # Input history (Textual Input has no built-in history)
        self._history: List[str] = []
        self._history_index: int = -1
        self._history_file = os.path.expanduser("~/.auto_run_history")
        self._load_history()

    # ── Compose ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="messages-scroll"):
            yield MessageContent(id="messages")
        yield SelectionOverlay(id="selection-overlay")
        yield Static("", id="border-top")
        yield Static("", id="continuation-display")
        yield Static("", id="task-strip")
        with Horizontal(id="input-row"):
            yield Static("", id="prompt-indicator")
            yield TextArea(id="text-input")
        yield SuggestionBar("", id="suggestions")
        yield Static("", id="border-bottom")
        yield Static("", id="footer")

    # ── Mount ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._selection_overlay = self.query_one("#selection-overlay", SelectionOverlay)
        self._selection_overlay.hide()
        msg = self._msg()
        msg.add("raw", f"{CH_BLACK_CIRCLE} AutoRUN v1.0", style=f"bold {C_CLAUDE}")
        msg.add("info", "  /help for commands  |  Ctrl+C/Esc to interrupt")
        msg.add("blank", "")
        msg._mc_apply()
        self._update_prompt()
        self._update_borders()
        self._update_footer()
        self.query_one("#text-input", TextArea).focus()
        import asyncio
        asyncio.ensure_future(self._init_engine_bg())
        self.set_interval(0.1, self._tick_spinner)

    async def on_event(self, event):
        """Intercept MouseDown to auto-focus, and Enter to submit before TextArea sees it.

        We handle Enter here (pre super) because TextArea._on_key consumes the
        Enter key by inserting \\n, which prevents the App-level priority binding
        from ever firing.  Intercepting at on_event is the earliest reliable hook.
        """
        # ── Selection mode key handling ──────────────────────────────
        if self._selection_mode and isinstance(event, Key):
            ov = self._selection_overlay
            if not ov or not ov.visible:
                self._selection_mode = False
            elif event.key == "up":
                event.stop()
                event.prevent_default()
                ov.move_up()
                return
            elif event.key == "down":
                event.stop()
                event.prevent_default()
                ov.move_down()
                return
            elif event.key == "enter":
                event.stop()
                event.prevent_default()
                self._selection_confirm()
                return
            elif event.key == "escape":
                event.stop()
                event.prevent_default()
                self._selection_cancel()
                return
            elif ov._mode == "search" and event.key and len(event.key) == 1:
                # Typeable character in search mode
                ov._search_query += event.key
                event.stop()
                event.prevent_default()
                ov._refresh()
                return
            elif ov._mode == "search" and event.key == "backspace":
                ov._search_query = ov._search_query[:-1]
                event.stop()
                event.prevent_default()
                ov._refresh()
                return

        if isinstance(event, MouseDown):
            if getattr(event, 'button', 0) != 3:
                # Left/middle click → focus the text input
                try:
                    self.query_one("#text-input", TextArea).focus()
                except Exception:
                    logger.debug("Failed to focus text input", exc_info=True)
        elif isinstance(event, Key) and event.key == "enter":
            # Check that the text input (or a child) is focused, so Enter in
            # modals / popups is not stolen.
            focused = self.focused
            if focused is not None and focused.id == "text-input":
                event.stop()
                event.prevent_default()
                self.action_submit_input()
                return
        elif isinstance(event, Key) and event.key == "up":
            focused = self.focused
            if focused is not None and focused.id == "text-input":
                ta = self.query_one("#text-input", TextArea)
                row, _col = ta.cursor_location
                bar = self._suggestion_bar()
                # Intercept up: navigate suggestion or history when at first line
                if bar.is_visible or row == 0:
                    event.stop()
                    event.prevent_default()
                    self.action_history_up()
                    return
        elif isinstance(event, Key) and event.key == "down":
            focused = self.focused
            if focused is not None and focused.id == "text-input":
                ta = self.query_one("#text-input", TextArea)
                row, _col = ta.cursor_location
                lines = ta.text.split("\n")
                bar = self._suggestion_bar()
                # Intercept down: navigate suggestion or history when at last line
                if bar.is_visible or row >= len(lines) - 1:
                    event.stop()
                    event.prevent_default()
                    self.action_history_down()
                    return
        await super().on_event(event)

    def on_unmount(self) -> None:
        self._save_history()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _msg(self) -> MessageContent:
        return self.query_one("#messages", MessageContent)

    def _scroll_view(self) -> VerticalScroll:
        return self.query_one("#messages-scroll", VerticalScroll)

    def _scroll_to_bottom(self) -> None:
        self._scroll_view().scroll_end(animate=False)

    # ── Spinner ──────────────────────────────────────────────────────────

    def _tick_spinner(self) -> None:
        if not self.is_query_active:
            return

        msg = self._msg()
        tick = int(time.time() * 1000) // 120
        spinner_char = _SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)]
        verb = self._thinking_verb or "Thinking"

        elapsed_ms = 0
        if self._loading_start_time > 0:
            elapsed_ms = int((time.time() - self._loading_start_time) * 1000)

        elapsed_str = _format_duration(elapsed_ms)
        status_parts = [elapsed_str]

        tokens = max(0, self._streamed_total // 2)
        if tokens > 0 and self._streamed_total > 20:
            status_parts.append(f"\u2193 {_format_tokens(tokens)} tokens")

        if self._thinking_status == "thinking":
            status_parts.append("(thinking)")
        elif self._tool_blocks:
            status_parts.append(f"(executing {self._tool_blocks[-1].name})")
        elif self._streamed_total > 0:
            status_parts.append("(streaming)")

        status_str = "  (" + f" {CH_DOT} ".join(status_parts) + ")"
        msg.spinner = f"{spinner_char} {verb}\u2026"
        msg.status_text = status_str
        msg._mc_apply()

        self._update_footer()

    # ── UI updates ───────────────────────────────────────────────────────

    def _update_prompt(self) -> None:
        indicator = self.query_one("#prompt-indicator", Static)
        if self._continuation_buffer:
            # PS2 continuation prompt
            indicator.update(f"[{C_TEXT}]> [/]")
        elif self._input_mode == "bash":
            indicator.update(f"[{C_BASH_BORDER}]![/] ")
        elif self.is_query_active:
            indicator.update(f"[{C_TEXT}]{CH_POINTER}[/] ")
        else:
            indicator.update(f"[{C_TEXT}]{CH_POINTER}[/] ")

    def _update_borders(self) -> None:
        w = self.size.width
        line = "\u2500" * w
        color = C_BASH_BORDER if self._input_mode == "bash" else C_BORDER
        self.query_one("#border-top", Static).update(f"[{color}]{line}[/]")
        self.query_one("#border-bottom", Static).update(f"[{color}]{line}[/]")

    def _update_continuation_display(self) -> None:
        """Show/hide the continuation buffer above the input row.

        When the user builds a multi-line input (via \\ or Shift+Enter), the
        accumulated lines are displayed above the input row so the user can
        see what will be submitted. Hidden (height=0) when the buffer is empty.
        """
        widget = self.query_one("#continuation-display", Static)
        if self._continuation_buffer:
            lines = self._continuation_buffer.split("\n")
            display = "\n".join(
                f"[{C_INACTIVE}]  {line}[/]" for line in lines
            )
            widget.update(display)
            widget.styles.height = len(lines)
        else:
            widget.update("")
            widget.styles.height = 0

    def _update_footer(self) -> None:
        mode = getattr(self._state, 'permission_mode', 'default')
        symbol = PERMISSION_SYMBOLS.get(mode, "")
        title = PERMISSION_TITLES.get(mode, "Default")

        model = getattr(self._state, 'model', None) or \
                os.environ.get("AUTORUN_MODEL", "")
        model_short = "???"
        if model:
            model_short = model.replace("deepseek-", "ds-").replace("claude-", "cl-")
        msgs = len(self._state.get_messages())

        parts = []
        if symbol:
            parts.append(f"{symbol} ")
        parts.append(f"{title.lower()} on")
        parts.append(f" {CH_DOT} {model_short}")
        parts.append(f" {CH_DOT} {msgs}m")
        if self._turn_count > 0:
            parts.append(f" {CH_DOT} {self._turn_count}t")

        if self.is_query_active:
            parts.append("  Ctrl+C/esc to interrupt")
        else:
            parts.append("  ?/help  |  Ctrl+C/esc exit  |  Ctrl+E toggle tool  |  Ctrl+T toggle all")

        self.query_one("#footer", Static).update(
            f"[{C_INACTIVE}]{''.join(parts)}[/]")

    def on_resize(self, event) -> None:
        self._update_borders()
        # Re-adjust input width for new terminal size (allow shrink)
        ta = self.query_one("#text-input", TextArea)
        self._adjust_input_width(ta.text.rstrip("\n"), force=True)

    # ── Input ────────────────────────────────────────────────────────────

    def action_submit_input(self) -> None:
        """Enter: send message, or continue line if text ends with \\."""
        bar = self._suggestion_bar()
        if bar.is_visible and bar.selected_item:
            self.action_complete_suggestion()
            return

        ta = self.query_one("#text-input", TextArea)
        text = ta.text.rstrip("\n")

        if not text:
            return

        # \\ at end of line → remove \\, insert newline, continue typing
        if text.rstrip().endswith("\\"):
            # Strip the trailing \\ and append newline
            stripped = text.rstrip()[:-1]  # remove \
            ta.text = stripped + "\n"
            self._move_cursor_to_end(ta)
            return

        ta.text = ""
        self._handle_submit(text)

    def _handle_submit(self, raw_text: str) -> None:
        # Bash mode detection
        if raw_text.startswith("!") and self._input_mode == "prompt":
            self._input_mode = "bash"
            self._update_prompt()
            self._update_borders()
        elif not raw_text.startswith("!") and self._input_mode == "bash":
            self._input_mode = "prompt"
            self._update_prompt()
            self._update_borders()

        text = raw_text.strip()
        if not text:
            return

        self._update_prompt()
        self._push_history(text)

        if is_command(text):
            result = execute_command(text, self)
            if result == RESUME_MARKER:
                # Detect intent from command name
                cmd_name = text.split()[0].lower()
                if cmd_name in ("/resume", "/r"):
                    self._start_resume_flow()
                elif cmd_name.startswith("/skills"):
                    self._start_skills_selection(cmd_name)
                return
            if result:
                msg = self._msg()
                for line in result.split("\n"):
                    msg.add("text", line, style=C_TEXT)
                msg._mc_apply()
                self._scroll_to_bottom()
            return

        display = "!" + text if self._input_mode == "bash" else text

        msg = self._msg()
        msg.add("pointer", display)
        msg._mc_apply()
        self._scroll_to_bottom()

        import asyncio
        asyncio.ensure_future(self._run_query(display))

    # ── Keyboard actions ─────────────────────────────────────────────────

    def action_interrupt_or_quit(self) -> None:
        if self.is_query_active:
            self._cancel_requested = True
        else:
            self.exit()

    def action_eof_exit(self) -> None:
        ta = self.query_one("#text-input", TextArea)
        if not ta.text.strip():
            self.exit()

    def action_clear_screen(self) -> None:
        msg = self._msg()
        msg.clear_all()
        msg._mc_apply()

    def action_insert_newline(self) -> None:
        """Shift+Enter: insert a literal newline in the text area."""
        ta = self.query_one("#text-input", TextArea)
        row, col = ta.cursor_location
        ta.insert("\n", (row, col))

    def action_cycle_permission(self) -> None:
        modes = ["default", "accept_edits", "plan", "bypass"]
        current = getattr(self._state, 'permission_mode', 'default')
        try:
            idx = modes.index(current)
            self._state.permission_mode = modes[(idx + 1) % len(modes)]
        except ValueError:
            self._state.permission_mode = "default"
        self._update_footer()

    def action_toggle_tool(self) -> None:
        """Ctrl+E: toggle collapse/expand of the last tool block."""
        if self._tool_blocks:
            last = self._tool_blocks[-1]
            last.collapsed = not last.collapsed
            # Sync the state — tool blocks are shared objects between
            # _tool_blocks and _mc_tool_blocks, so mutation is visible in both.
            # Just trigger a re-render.
            self._msg()._mc_apply()

    def action_toggle_all_tools(self) -> None:
        """Ctrl+T: toggle collapse/expand of ALL tool blocks."""
        if self._tool_blocks:
            all_collapsed = all(tb.collapsed for tb in self._tool_blocks)
            for tb in self._tool_blocks:
                tb.collapsed = not all_collapsed
            self._msg()._mc_apply()

    # ── Suggestions ───────────────────────────────────────────────────────

    @staticmethod
    def _display_width(text: str) -> int:
        """Calculate display cell width, matching Textual's internal wcwidth."""
        from rich.cells import cell_len
        return cell_len(text)

    def _adjust_input_width(self, text: str, force: bool = False) -> None:
        """Resize the text input to fit the text. Max width = window width - 4.

        Uses grow-only strategy during typing to avoid horizontal scrollbar
        flicker (which changes TextArea height → border jitter).
        Resets to minimum when text is cleared. Set force=True to allow
        shrinking (used in on_resize when terminal is resized).
        """
        content_w = self._display_width(text)
        # padding 0 1 → 2 cells + 1 cursor cell + 2 safety margin
        needed = content_w + 5
        max_w = max(3, self.size.width - 4)
        new_w = max(3, min(needed, max_w))

        ta = self.query_one("#text-input", TextArea)
        current_w = ta.size.width

        if not text:
            # Reset to minimum when text cleared
            if current_w > 3:
                ta.styles.width = 3
        elif force or new_w > current_w:
            # force (on_resize): always apply. typing: only grow.
            ta.styles.width = new_w

    def _suggestion_bar(self) -> SuggestionBar:
        return self.query_one("#suggestions", SuggestionBar)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Update suggestions and dynamically resize input width."""
        ta = event.text_area
        text = ta.text.rstrip("\n")
        self._adjust_input_width(text)

        if not text:
            self._suggestion_bar().clear()
            return

        # Only trigger suggestions when cursor is at end of text
        row, col = ta.cursor_location
        lines = ta.text.split("\n")
        if row >= len(lines) or col != len(lines[row]):
            return

        # / command suggestions
        if text.startswith("/") and " " not in text:
            partial = text[1:]
            items = get_command_suggestions(partial)
            if items:
                bar = self._suggestion_bar()
                old_selected = bar.selected_index if bar.is_visible else 0
                bar.update_items(items, min(old_selected, len(items) - 1))
            else:
                self._suggestion_bar().clear()
            return

        # @ file suggestions
        if text.startswith("@"):
            partial = text[1:]
            items = get_file_suggestions(partial)
            if items:
                bar = self._suggestion_bar()
                old_selected = bar.selected_index if bar.is_visible else 0
                bar.update_items(items, min(old_selected, len(items) - 1))
            else:
                self._suggestion_bar().clear()
            return

        # Not a suggestion trigger — clear
        self._suggestion_bar().clear()

    def action_dismiss_suggestions(self) -> None:
        """Escape: dismiss the suggestion bar if visible."""
        bar = self._suggestion_bar()
        if bar.is_visible:
            bar.clear()
        else:
            # If suggestions not visible, let escape fall through to interrupt
            self.action_interrupt_or_quit()

    def action_complete_suggestion(self) -> None:
        """Tab: complete the currently selected suggestion."""
        bar = self._suggestion_bar()
        if not bar.is_visible:
            return
        ta = self.query_one("#text-input", TextArea)
        text = ta.text.rstrip("\n")

        if text.startswith("/"):
            selected = bar.selected_item
            if selected:
                cmd = selected.split()[0].lstrip("/").split("(")[0].strip()
                if cmd.endswith(","):
                    cmd = cmd[:-1]
                if "\u2192" in cmd:
                    cmd = cmd.split("\u2192")[-1].strip().lstrip("/")
                ta.text = "/" + cmd + " "
                self._move_cursor_to_end(ta)
        elif text.startswith("@"):
            selected = bar.selected_item
            if selected:
                f = selected
                if f.startswith("+ ") or f.startswith("\u25b8 "):
                    f = f[2:]
                ta.text = "@" + f + " "
                self._move_cursor_to_end(ta)
                inp.action_end()
        bar.clear()

    def action_scroll_page_up(self) -> None:
        self._scroll_view().scroll_page_up(animate=False)

    def action_scroll_page_down(self) -> None:
        self._scroll_view().scroll_page_down(animate=False)

    # ── Input history ────────────────────────────────────────────────────

    def _load_history(self) -> None:
        try:
            with open(self._history_file, "r", encoding="utf-8") as f:
                self._history = [line.rstrip("\n") for line in f if line.strip()]
        except FileNotFoundError:
            self._history = []

    def _save_history(self) -> None:
        if self._history:
            with open(self._history_file, "w", encoding="utf-8") as f:
                for item in self._history[-500:]:  # keep last 500
                    f.write(item + "\n")

    def _push_history(self, text: str) -> None:
        # Avoid consecutive duplicates
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = len(self._history)

    def _move_cursor_to_end(self, ta: TextArea) -> None:
        """Move cursor to the end of the text."""
        lines = ta.text.split("\n")
        last_row = max(0, len(lines) - 1)
        last_col = len(lines[last_row])
        ta.cursor_location = (last_row, last_col)

    def action_history_up(self) -> None:
        bar = self._suggestion_bar()
        if bar.is_visible:
            bar.move_up()
            return
        if not self._history:
            return
        ta = self.query_one("#text-input", TextArea)
        if self._history_index == len(self._history):
            self._history_unsaved = ta.text
        if self._history_index > 0:
            self._history_index -= 1
            ta.text = self._history[self._history_index]
            self._move_cursor_to_end(ta)

    def action_history_down(self) -> None:
        bar = self._suggestion_bar()
        if bar.is_visible:
            bar.move_down()
            return
        ta = self.query_one("#text-input", TextArea)
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            ta.text = self._history[self._history_index]
            self._move_cursor_to_end(ta)
        elif self._history_index == len(self._history) - 1:
            self._history_index = len(self._history)
            ta.text = getattr(self, "_history_unsaved", "")
            self._move_cursor_to_end(ta)

    # ── Selection mode ─────────────────────────────────────────────────────

    def _selection_confirm(self) -> None:
        """Confirm current selection."""
        ov = self._selection_overlay
        if not ov or not ov.visible:
            self._selection_mode = False
            return
        selected = ov.select_current()
        if selected and ov._on_select:
            ov._on_select(selected)

    def _selection_cancel(self) -> None:
        """Cancel selection mode."""
        self._selection_mode = False
        ov = self._selection_overlay
        if ov:
            ov.cancel()

    def _start_resume_flow(self) -> None:
        """Enter resume conversation flow — step 1: choose scope."""
        items = [
            {"label": "当前项目目录下的对话", "detail": "", "data": {"action": "project"}},
            {"label": "所有项目目录下的对话", "detail": "", "data": {"action": "all"}},
            {"label": "AI 智能搜索", "detail": "", "data": {"action": "search"}},
        ]

        ov = self._selection_overlay
        ov.show(
            title="选择恢复方式",
            items=items,
            mode="options",
            on_select=lambda item: self._resume_step2(item["data"]["action"]),
            on_cancel=lambda: None,
        )
        self._selection_mode = True

    def _resume_step2(self, action: str) -> None:
        """Step 2: show conversation list or search mode."""
        from AutoRUN_v1.services.conversations import list_conversations

        if action == "search":
            items = [{"label": "输入关键词后按 Enter 搜索", "detail": "", "data": {"action": "do_search"}}]
            ov = self._selection_overlay
            ov.show(
                title="AI 搜索对话",
                items=items,
                mode="search",
                on_select=lambda item: self._resume_search(),
                on_cancel=lambda: self._start_resume_flow(),
            )
            self._selection_mode = True
            return

        cwd = os.getcwd() if action == "project" else None
        try:
            conversations = list_conversations(cwd_filter=cwd)
        except RuntimeError as e:
            self._selection_mode = False
            ov = self._selection_overlay
            ov.hide()
            msg = self._msg()
            msg.add("error", f"加载对话列表失败：{e}")
            msg._mc_apply()
            return

        if not conversations:
            self._selection_mode = False
            ov = self._selection_overlay
            ov.hide()
            msg = self._msg()
            label = "当前项目" if action == "project" else ""
            msg.add("info", f"没有找到{label}已保存的对话。")
            msg._mc_apply()
            return

        items = []
        for conv in conversations:
            sid = conv.get("session_id", "")[:12]
            updated = conv.get("updated_at", "")[:16].replace("T", " ")
            count = conv.get("message_count", 0)
            preview = conv.get("preview", "")[:60]
            project = conv.get("project_name", "")
            label = f"[{updated}] {count}条"
            detail = f"{project} — {preview}"
            items.append({
                "label": label,
                "detail": detail,
                "data": {"session_id": sid},
            })

        title = "当前项目" if action == "project" else "所有项目"
        ov = self._selection_overlay
        ov.show(
            title=f"{title}的对话",
            items=items,
            mode="conversations",
            on_select=lambda item: self._resume_load(item["data"]["session_id"]),
            on_cancel=lambda: self._start_resume_flow(),
        )
        self._selection_mode = True

    def _resume_search(self) -> None:
        """Execute search and show results."""
        from AutoRUN_v1.services.conversations import search_conversations

        ov = self._selection_overlay
        query = ov._search_query.strip()
        if not query:
            return

        try:
            results = search_conversations(query)
        except RuntimeError as e:
            self._selection_mode = False
            ov.hide()
            msg = self._msg()
            msg.add("error", f"搜索对话失败：{e}")
            msg._mc_apply()
            return

        if not results:
            self._selection_mode = False
            ov.hide()
            msg = self._msg()
            msg.add("info", f"未找到包含 '{query}' 的对话。")
            msg._mc_apply()
            return

        items = []
        for conv in results:
            sid = conv.get("session_id", "")[:12]
            updated = conv.get("updated_at", "")[:16].replace("T", " ")
            count = conv.get("message_count", 0)
            preview = conv.get("preview", "")[:60]
            project = conv.get("project_name", "")
            items.append({
                "label": f"[{updated}] {count}条 — {project}",
                "detail": preview,
                "data": {"session_id": sid},
            })

        ov.show(
            title=f"搜索结果: {query}",
            items=items,
            mode="conversations",
            on_select=lambda item: self._resume_load(item["data"]["session_id"]),
            on_cancel=lambda: self._start_resume_flow(),
        )
        self._selection_mode = True

    def _resume_load(self, session_id: str) -> None:
        """Load a conversation and restore state."""
        from AutoRUN_v1.services.conversations import restore_to_state, load_conversation
        from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

        self._selection_mode = False
        ov = self._selection_overlay
        if ov:
            ov.hide()

        try:
            data = load_conversation(session_id)
        except RuntimeError as e:
            msg = self._msg()
            msg.add("error", str(e))
            msg._mc_apply()
            return
        if not data:
            msg = self._msg()
            msg.add("error", "无法加载对话。")
            msg._mc_apply()
            return

        ok = restore_to_state(session_id, self._state)
        if not ok:
            msg = self._msg()
            msg.add("error", "恢复对话失败。")
            msg._mc_apply()
            return

        # Refresh skills with loaded disabled state
        clear_skills_cache()
        disabled = self._state._get_disabled_skills()
        discover_skills(refresh=True, disabled_skills=disabled)
        register_skills_to_tool(disabled_skills=disabled)

        # Clear UI and re-render messages
        ui_msg = self._msg()
        ui_msg.clear_all()
        for msg_obj in self._state.get_messages():
            self._render_loaded_message(ui_msg, msg_obj)
        ui_msg._mc_apply()
        self._scroll_to_bottom()

        # Show summary
        messages = self._state.get_messages()
        msg_count = len(messages)
        project = data.get("project_name", "")
        model = data.get("model", "")
        ui_msg.add("info", f"对话已恢复: {project} ({msg_count}条消息, {model})")
        ui_msg._mc_apply()

    def _render_loaded_message(self, ui_msg, msg_obj) -> None:
        """Render a restored message into the UI."""
        try:
            text = msg_obj.get_text()
            if text and hasattr(msg_obj, 'type') and msg_obj.type == "user":
                prefix = "❯ " if not getattr(msg_obj, 'tool_use_result', None) else ""
                ui_msg.add("pointer" if not getattr(msg_obj, 'tool_use_result', None) else "text",
                           prefix + (text[:200] + "..." if len(text) > 200 else text))
            elif text and hasattr(msg_obj, 'type') and msg_obj.type == "assistant":
                ui_msg.add("assistant", text[:300] + "..." if len(text) > 300 else text)
        except Exception:
            logger.debug("Failed to render loaded message", exc_info=True)

    def _start_skills_selection(self, cmd_name: str) -> None:
        """Show skills list for selection/toggle."""
        from AutoRUN_v1.skills.loader import discover_skills

        all_skills = discover_skills(refresh=True)
        disabled = self._state._get_disabled_skills()

        if not all_skills:
            self._selection_mode = False
            msg = self._msg()
            msg.add("info", "没有已加载的 skill。")
            msg._mc_apply()
            return

        items = []
        for name in sorted(all_skills.keys()):
            skill = all_skills[name]
            desc = skill.get("description", "")
            status = "✗" if name in disabled else "✓"
            items.append({
                "label": f"{status} {name}",
                "detail": desc,
                "data": {"skill_name": name, "is_disabled": name in disabled},
            })

        ov = self._selection_overlay
        ov.show(
            title="Skill 状态 (Enter 切换启用/禁用)",
            items=items,
            mode="skills",
            on_select=lambda item: self._skills_toggle_item(item["data"]["skill_name"]),
            on_cancel=lambda: None,
        )
        self._selection_mode = True

    def _skills_toggle_item(self, skill_name: str) -> None:
        """Toggle a skill and refresh the display."""
        from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

        disabled = self._state._get_disabled_skills()
        if skill_name in disabled:
            self._state.enable_skill(skill_name)
        else:
            self._state.disable_skill(skill_name)

        clear_skills_cache()
        discover_skills(refresh=True, disabled_skills=self._state._get_disabled_skills())
        register_skills_to_tool(disabled_skills=self._state._get_disabled_skills())

        # Refresh the overlay
        self._start_skills_selection("")

    # ── Query runner ─────────────────────────────────────────────────────

    async def _run_query(self, user_input: str) -> None:
        msg = self._msg()
        msg.spinner = "·"
        msg.status_text = "Connecting..."
        msg._mc_apply()

        if not self._engine_ok:
            msg.add("error", "Engine not initialized. Restart required.")
            msg._mc_apply()
            return

        self.is_query_active = True
        self._cancel_requested = False
        self._streamed_total = 0
        self._thinking_verb = random.choice(SPINNER_VERBS)
        self._loading_start_time = time.time()
        self._thinking_status = "thinking"
        self._tool_blocks = []
        self._shown_stream_tool_ids = set()
        self._suppressed_task_tool_ids = set()

        msg.streaming = ""
        msg.spinner = ""
        msg.status_text = ""
        self._update_prompt()
        self._update_footer()
        msg._mc_apply()

        async def _check_perm(tool_name: str, tool_args: Dict) -> bool:
            ph = self._get_perm_handler()
            if ph.is_tool_always_allowed(tool_name):
                return True
            is_dangerous = (ph.is_tool_destructive(tool_name) or
                          ph.check_sensitive_command(tool_args))
            return await ph.prompt_tool_permission(
                tool_name, tool_args, is_sensitive=is_dangerous,
            )

        _seen_tool_ids: set = set()

        try:
            async for event in self._engine.send_message(
                user_input, can_use_tool=_check_perm,
            ):
                if self._cancel_requested:
                    msg = self._msg()
                    msg.add("interrupted", "Interrupted by user")
                    msg.streaming = ""
                    msg.spinner = ""
                    msg.status_text = ""
                    msg._mc_apply()
                    break

                et = event.get("type", "")

                if et == "stream_request_start":
                    self._loading_start_time = time.time()
                    self._thinking_status = "thinking"
                    prev = self._thinking_verb
                    while self._thinking_verb == prev and len(SPINNER_VERBS) > 1:
                        self._thinking_verb = random.choice(SPINNER_VERBS)
                    continue

                elif et == "assistant":
                    content = event.get("content", [])

                    if event.get("is_partial"):
                        # Streaming text delta
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                txt = b.get("text", "")
                                if len(txt) > self._streamed_total:
                                    self._thinking_status = "streaming"
                                    cleaned, xml_blocks = _parse_xml_tool_calls(txt)
                                    msg.streaming = cleaned
                                    self._streamed_total = len(txt)
                                    for tb in xml_blocks:
                                        if tb["id"] not in self._shown_stream_tool_ids:
                                            self._add_tool_use(tb)
                                            self._shown_stream_tool_ids.add(tb["id"])
                                    msg._mc_apply()
                    else:
                        # Complete message — process content blocks in order so
                        # tool_use blocks appear interleaved with text, not all
                        # lumped at the bottom.
                        has_text = any(
                            isinstance(b, dict) and b.get("type") == "text"
                            for b in content
                        )
                        has_tool_use = any(
                            isinstance(b, dict) and b.get("type") == "tool_use"
                            for b in content
                        )

                        if has_text:
                            # Native tool calling (Anthropic-style): text and
                            # tool_use blocks are interleaved in the content
                            # array. Process them in order.
                            msg.streaming = ""
                            for b in content:
                                if not isinstance(b, dict):
                                    continue
                                bt = b.get("type", "")
                                if bt == "text":
                                    raw_text = b.get("text", "")
                                    if raw_text:
                                        msg.add("assistant", raw_text)
                                elif bt == "tool_use":
                                    tid = b.get("id", "")
                                    if tid not in _seen_tool_ids:
                                        _seen_tool_ids.add(tid)
                                        self._add_tool_use(b)
                                elif bt == "tool_result":
                                    self._add_tool_result(b)
                            self._thinking_status = "done"
                        elif msg.streaming:
                            # DeepSeek XML tools: tool blocks already added
                            # during streaming. Flush streaming text as one
                            # entry (it's all the text from this turn, XML
                            # already stripped).
                            cleaned, _ = _parse_xml_tool_calls(msg.streaming)
                            if cleaned:
                                msg.add("assistant", cleaned)
                            msg.streaming = ""
                            self._thinking_status = "done"
                        else:
                            # Only tool_use blocks, no streaming text
                            # (e.g. Anthropic model that goes straight to tools).
                            msg.streaming = ""
                            for b in content:
                                if not isinstance(b, dict):
                                    continue
                                bt = b.get("type", "")
                                if bt == "tool_use":
                                    tid = b.get("id", "")
                                    if tid not in _seen_tool_ids:
                                        _seen_tool_ids.add(tid)
                                        self._add_tool_use(b)
                                elif bt == "tool_result":
                                    self._add_tool_result(b)
                            self._thinking_status = "done"

                        msg._mc_apply()

                elif et == "user":
                    for b in event.get("content", []):
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            self._add_tool_result(b)
                    self._update_task_strip()
                    msg._mc_apply()

                elif et == "retry_attempt":
                    msg = self._msg()
                    msg.add("warn",
                        f"API 调用失败，正在重试 "
                        f"({event.get('attempt', '?')}/{event.get('max_retries', '?')})..."
                    )
                    msg._mc_apply()

                elif et == "error":
                    msg = self._msg()
                    msg.add("error", f"Error: {event.get('error', 'Unknown error')}")
                    msg._mc_apply()

                elif et == "attachment":
                    a = event.get("attachment", {})
                    at = a.get("type", "")
                    m = self._msg()
                    if at == "auto_compact_suggestion":
                        m.add("warn", "Context limit approaching. Consider /compact")
                    m._mc_apply()

                elif et == "terminal":
                    break

                self._scroll_to_bottom()

        finally:
            self.is_query_active = False
            msg = self._msg()
            msg.streaming = ""
            msg.spinner = ""
            msg.status_text = ""
            msg._mc_apply()
            self._streamed_total = 0
            self._thinking_status = None
            self._tool_blocks = []  # new list for next query (old ref kept by msg._mc_tool_blocks)
            self._suppressed_task_tool_ids.clear()
            self._update_task_strip()
            self._turn_count += 1
            self._update_prompt()
            self._update_footer()
            self._scroll_to_bottom()

    # ── Tool rendering ───────────────────────────────────────────────────

    def _add_tool_use(self, block: Dict[str, Any]) -> None:
        """Queue tool call as a collapsible ToolBlock (mirrors WebUI tool-card)."""
        name = block.get("name", "unknown")
        tool_id = block.get("id", name)

        # Suppress task tool calls from chat — shown in task strip instead
        from AutoRUN_v1.tools.task_tool import TASK_TOOL_NAMES
        if name in TASK_TOOL_NAMES:
            self._suppressed_task_tool_ids.add(tool_id)
            return

        inp = block.get("input", {})

        # Build short description
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

        # Format args
        arg_lines = []
        for k, v in inp.items():
            v_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            if len(v_str) > 100:
                v_str = v_str[:100] + "..."
            arg_lines.append(f"     {k}: {v_str}")
        inp_str = "\n".join(arg_lines) if arg_lines else ""

        # ── Read merging ──
        if is_read and self._tool_blocks:
            last = self._tool_blocks[-1]
            if "read" in last.name.lower() or "Read" in last.name:
                last.merged_count += 1
                if file_path and file_path not in last.merged_files:
                    last.merged_files.append(file_path)
                last.short_desc = short_desc
                if inp_str:
                    last.inp_str += f"\n{inp_str}"
                self._msg()._mc_tool_blocks = list(self._tool_blocks)
                self._msg()._mc_apply()
                return

        tb = ToolBlock(
            id=tool_id,
            name=name,
            inp_str=inp_str,
            short_desc=short_desc,
            collapsed=True,
            merged_files=[file_path] if is_read and file_path else [],
        )
        self._tool_blocks.append(tb)

        # Sync to MessageContent
        msg = self._msg()
        msg._mc_tool_blocks = list(self._tool_blocks)  # copy, avoid shared ref
        # Also register in _mc_entries for inline rendering (correct position)
        msg._mc_entries.append({"kind": "tool_block", "tool_id": tool_id, "tool_name": name})
        msg._mc_apply()

    def _add_tool_result(self, block: Dict[str, Any]) -> None:
        """Append tool result to the matching ToolBlock."""
        tool_id = block.get("tool_use_id", "")

        # If this is a suppressed task tool result, just update the task strip
        if tool_id in self._suppressed_task_tool_ids:
            self._suppressed_task_tool_ids.discard(tool_id)
            self._update_task_strip()
            return

        content = str(block.get("content", "")).replace("\t", "    ")
        is_err = block.get("is_error", False)

        # Truncate long output
        lines = content.split("\n")
        if len(lines) > 5:
            lines = lines[:5] + [f"... ({len(lines) - 5} more lines)"]
        result_str = "\n".join(lines)

        # Find and update matching tool block
        for tb in self._tool_blocks:
            if tb.id == tool_id:
                tb.result_str = result_str
                tb.result_is_error = is_err
                tb.result_received = True
                break

        msg = self._msg()
        # ToolBlock objects are shared — updating tb in _tool_blocks also
        # updates the copy in _mc_tool_blocks. Just trigger re-render.
        msg._mc_apply()

    def _update_task_strip(self) -> None:
        """Render the task list above the input bar with checkbox indicators."""
        from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display

        tasks = get_all_tasks_for_display(self._state)
        widget = self.query_one("#task-strip", Static)

        if not tasks:
            widget.update("")
            widget.styles.height = 0
            return

        # Build Rich markup with checkbox + label per line
        status_icons = {
            "in_progress": f"[bold {C_SUGGESTION}]\u25c9[/] ",  # ◉ blue filled
            "pending": f"[{C_INACTIVE}]\u25cb[/] ",             # ○ grey hollow
            "completed": f"[{C_SUCCESS}]\u2611[/] ",           # ☑ green checked
        }
        status_suffix = {
            "in_progress": f"[bold {C_SUGGESTION}]",
            "pending": f"[{C_INACTIVE}]",
            "completed": f"[strikethrough {C_SUCCESS}]",
        }

        lines = []
        for t in tasks:
            status = t.get("status", "pending")
            icon = status_icons.get(status, status_icons["pending"])
            suffix = status_suffix.get(status, "")
            label = t.get("label", "")[:60]
            lines.append(f"{icon}{suffix}{label}[/]")

        widget.update("\n".join(lines))
        widget.styles.height = len(lines)

    # ── Engine ───────────────────────────────────────────────────────────

    async def _init_engine_bg(self) -> None:
        """Initialize engine in background after mount."""
        self._engine_ok = await self._init_engine()

    async def _init_engine(self) -> bool:
        from AutoRUN_v1.query_engine import QueryEngine
        self._engine = QueryEngine(self._state)
        await self._engine.initialize()

        # 检查是否需要提示用户构建索引
        idx = self._state.indexer
        if idx and idx.needs_prompt():
            self._notify_indexer_prompt()

        # 自动恢复当前目录最新对话
        await self._auto_resume_latest()
        return True

    async def _auto_resume_latest(self) -> None:
        """自动恢复当前项目目录的最新对话。"""
        try:
            from AutoRUN_v1.services.conversations import (
                list_conversations, restore_to_state,
            )
            from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

            cwd = os.getcwd()
            conversations = list_conversations(cwd_filter=cwd)
            if not conversations:
                return  # 当前目录没有保存的对话

            # 取最新的一条（已按 updated_at 降序排列）
            latest = conversations[0]
            session_id = latest.get("session_id", "")

            ok = restore_to_state(session_id, self._state)
            if not ok:
                return

            # 刷新 skills
            clear_skills_cache()
            disabled = self._state._get_disabled_skills()
            discover_skills(refresh=True, disabled_skills=disabled)
            register_skills_to_tool(disabled_skills=disabled)

            # 渲染已加载的消息到 UI
            ui_msg = self._msg()
            ui_msg.clear_all()
            for msg_obj in self._state.get_messages():
                self._render_loaded_message(ui_msg, msg_obj)

            msg_count = len(self._state.get_messages())
            project = latest.get("project_name", "")
            model = latest.get("model", "")
            ui_msg.add("info", f"已自动恢复上次对话: {project} ({msg_count}条消息, {model})")
            ui_msg._mc_apply()
            self._scroll_to_bottom()

        except Exception:
            logger.debug("Auto-resume failed", exc_info=True)

    def _notify_indexer_prompt(self) -> None:
        """通过 status 栏提示用户构建索引。"""
        import sys
        msg = (
            "\n[AutoRUN] 项目文件索引尚未构建。\n"
            "[AutoRUN] 输入 /index build 开始构建（后台），或 /index skip 跳过。\n"
        )
        sys.stdout.write(msg)
        sys.stdout.flush()

    def _get_perm_handler(self):
        if self._perm_handler is None:
            from AutoRUN_v1.ui.cli.permissions import get_permission_handler
            self._perm_handler = get_permission_handler()
        return self._perm_handler
