"""
Slash command / file suggestion bar.

Mirrors the suggestion overlay from the TypeScript Claude Code
(src/components/PromptInput/PromptInputFooterSuggestions.tsx).

Provides a dropdown suggestion list below the input row when
the user types / (slash commands) or @ (file references).
"""

import glob
import logging
import os
from typing import List, Optional

from textual.widgets import Static

logger = logging.getLogger(__name__)


MAX_VISIBLE_ITEMS = 6


class SuggestionBar(Static):
    """A suggestion dropdown that appears below the input row.

    Managed by AutoRUNApp — call update_suggestions(items, selected)
    to show suggestions, or clear() to hide.
    """

    def __init__(self, renderable="", **kwargs):
        super().__init__(renderable, **kwargs)
        self._items: List[str] = []          # list of display strings
        self._selected: int = -1             # currently highlighted index
        self.SUGGESTION_COLOR = "#b1b9f9"
        self.DIM_COLOR = "#888888"
        self.SELECTED_COLOR = "#ffffff"

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def selected_item(self) -> Optional[str]:
        if 0 <= self._selected < len(self._items):
            return self._items[self._selected]
        return None

    def update_items(self, items: List[str], selected: int = -1) -> None:
        """Update the suggestion list and selected index.

        If items is empty, the bar is cleared (hidden).
        """
        self._items = items
        self._selected = selected if 0 <= selected < len(items) else -1
        self._draw()

    def move_up(self) -> None:
        if not self._items:
            return
        if self._selected <= 0:
            self._selected = len(self._items) - 1  # wrap to bottom
        else:
            self._selected -= 1
        self._draw()

    def move_down(self) -> None:
        if not self._items:
            return
        if self._selected >= len(self._items) - 1:
            self._selected = 0  # wrap to top
        else:
            self._selected += 1
        self._draw()

    def clear(self) -> None:
        self._items = []
        self._selected = -1
        self.update("")
        # Force zero height when no suggestions — otherwise Static("") still
        # takes up 1 row in Textual, causing a gap between input and border.
        self.styles.height = 0
        self.styles.padding = 0

    @property
    def is_visible(self) -> bool:
        return len(self._items) > 0

    def _draw(self) -> None:
        if not self._items:
            self.update("")
            self.styles.height = 0
            self.styles.padding = 0
            return
        # Restore visible dimensions when suggestions are shown
        self.styles.height = "auto"
        self.styles.padding = (0, 2)

        # Show a window of MAX_VISIBLE_ITEMS centred on selected
        start = max(0, self._selected - MAX_VISIBLE_ITEMS // 2)
        start = min(start, max(0, len(self._items) - MAX_VISIBLE_ITEMS))
        visible = self._items[start:start + MAX_VISIBLE_ITEMS]

        lines = []
        for i, item in enumerate(visible):
            idx = start + i
            if idx == self._selected:
                lines.append(f"[bold {self.SELECTED_COLOR} on #333333] {item} [/]")
            else:
                lines.append(f"[{self.DIM_COLOR}] {item} [/]")
        self.update("\n".join(lines))


# ── Suggestion generators ─────────────────────────────────────────────────

def get_command_suggestions(partial: str) -> List[str]:
    """Get slash-command suggestions matching the partial input.

    Args:
        partial: The text after / (e.g. "he" for "/he")
    """
    from AutoRUN_v1.commands import get_registry

    registry = get_registry()
    cmds = registry.get_visible_commands()
    partial_lower = partial.lower()

    results = []
    for cmd in cmds:
        raw_name = cmd["name"]
        clean_name = raw_name.lstrip("/")
        if partial_lower in raw_name.lower():
            aliases = [a for a in cmd.get("aliases", []) if a != clean_name]
            desc = cmd.get("description", "")
            if aliases:
                alias_str = f" ({', '.join('/' + a for a in aliases)})"
            else:
                alias_str = ""
            line = f"/{clean_name}{alias_str}"
            if desc:
                # Truncate description to avoid overflow
                max_desc = max(0, 60 - len(line))
                if len(desc) > max_desc:
                    desc = desc[:max_desc - 1] + "\u2026"  # …
                line += f"  \u2014  {desc}"  # —
            results.append(line)

        # Also match aliases (skip aliases that are just the command name without /)
        for alias in cmd.get("aliases", []):
            if alias == clean_name:
                continue
            if partial_lower in alias.lower():
                desc = cmd.get("description", "")
                line = f"/{alias}  \u2192  /{clean_name}"
                if desc:
                    max_desc = max(0, 60 - len(line))
                    if len(desc) > max_desc:
                        desc = desc[:max_desc - 1] + "\u2026"
                    line += f"  \u2014  {desc}"
                results.append(line)

    return results


def get_file_suggestions(partial: str) -> List[str]:
    """Get file-path suggestions matching the partial input.

    Args:
        partial: The text after @ (e.g. "src" for "@src")
    """
    if not partial:
        pattern = "*"
    elif partial.endswith(os.sep) or partial.endswith("/"):
        pattern = partial + "*"
    else:
        pattern = partial + "*"

    try:
        matches = glob.glob(pattern)
        # Also try searching in subdirectories
        if not matches:
            pattern2 = "**/" + pattern
            matches = glob.glob(pattern2, recursive=True)
    except Exception:
        logger.debug("File glob suggestion failed", exc_info=True)
        return []

    # Sort: directories first, then alphabetically
    matches.sort(key=lambda p: (not os.path.isdir(p), p.lower()))
    # Limit to 20 items
    return [("+ " if not os.path.isdir(m) else "\u25b8 ") + m for m in matches[:20]]


def find_common_prefix(items: List[str]) -> str:
    """Find the longest common prefix among cleaned item strings."""
    if not items:
        return ""
    # Strip icon prefix for comparison
    cleaned = []
    for item in items:
        c = item
        if c.startswith("+ ") or c.startswith("\u25b8 "):
            c = c[2:]
        if c.startswith("/"):
            c = c[1:]
        # Keep only up to first space/separator
        if "  " in c:
            c = c.split("  ")[0]
        cleaned.append(c)
    if not cleaned:
        return ""

    prefix = cleaned[0]
    for s in cleaned[1:]:
        while not s.lower().startswith(prefix.lower()):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix
