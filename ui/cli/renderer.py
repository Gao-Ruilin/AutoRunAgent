"""
消息渲染器 — 使用 Rich 格式化和渲染各类消息。

负责:
- 流式文本输出（逐 token，使用 sys.stdout）
- 完整消息的 Markdown 渲染（Rich）
- 工具调用/结果面板
- 状态栏、错误、警告
"""

import logging
import sys
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.markdown import Markdown

logger = logging.getLogger(__name__)
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


class MessageRenderer:
    """AutoRUN 消息渲染器。

    流式输出使用 sys.stdout.write（token 级别），
    完整渲染使用 Rich（Markdown、代码高亮、面板）。
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()
        self._stream_active = False

    # ── 流式输出（sys.stdout，逐 token）─────────────────────────────

    def write_stream(self, text: str) -> None:
        """输出一个流式文本片段（逐 token），直接写 stdout。"""
        if not self._stream_active:
            self._stream_active = True
        sys.stdout.write(text)
        sys.stdout.flush()

    def write_stream_end(self) -> None:
        """结束流式输出，输出换行。"""
        if self._stream_active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._stream_active = False

    # ── 欢迎 / 退出 / 状态栏 ────────────────────────────────────────

    def render_welcome(self) -> None:
        self.console.print()
        self.console.print(Panel.fit(
            "[bold]AutoRUN_v1[/bold] — 智能编程助手\n"
            "输入 [green]/help[/green] 查看命令, [green]/exit[/green] 退出。\n"
            "管道模式: [dim]echo 'hello' | python cli.py -p[/dim]",
            title="欢迎", border_style="blue",
        ))
        self.console.print()

    def render_goodbye(self) -> None:
        self.console.print("\n[dim]再见！[/dim]")

    def render_status(self, model: str, msg_count: int,
                      permission_mode: str = "default",
                      turn_count: int = 0) -> None:
        mode_color = {
            "default": "green", "accept_edits": "yellow",
            "bypass": "red", "plan": "cyan",
        }.get(permission_mode, "white")
        self.console.print(
            f"[dim]模型:[/dim] {model}  "
            f"[dim]消息:[/dim] {msg_count}  "
            f"[dim]轮次:[/dim] {turn_count}  "
            f"[dim]模式:[/dim] [{mode_color}]{permission_mode}[/{mode_color}]"
        )

    # ── 助手消息渲染 ────────────────────────────────────────────────

    def render_assistant_header(self) -> None:
        self.console.print("\n[bold blue]助手:[/bold blue]")

    def render_markdown(self, text: str) -> None:
        if not text.strip():
            return
        try:
            md = Markdown(text, code_theme="monokai")
            self.console.print(md)
        except Exception:
            logger.debug("Rich Markdown render failed, falling back to plain text", exc_info=True)
            self.console.print(text)

    def render_code_block(self, language: str, code: str) -> None:
        lang = language or "text"
        try:
            syntax = Syntax(code, lang, theme="monokai",
                            line_numbers=False, word_wrap=True)
            self.console.print(Panel(syntax, border_style="dim blue"))
        except Exception:
            logger.debug("Rich Syntax render failed, falling back to code block", exc_info=True)
            self.console.print(f"```{lang}\n{code}\n```")

    def render_text_block(self, text: str) -> None:
        self.console.print(text)

    # ── 工具调用 / 结果 ─────────────────────────────────────────────

    def render_tool_use(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        input_summary = self._format_args(tool_input)
        self.console.print(Panel(
            f"[bold cyan]{tool_name}[/bold cyan]\n[dim]{input_summary}[/dim]",
            border_style="cyan", title="工具调用", title_align="left",
        ))

    def render_tool_result(self, content: str, is_error: bool = False) -> None:
        style = "red" if is_error else "green"
        title = "工具错误" if is_error else "工具结果"
        display = content
        if len(content) > 800:
            display = content[:800] + f"\n[dim]... (共 {len(content)} 字符)[/dim]"
        self.console.print(Panel(
            display, border_style=style, title=title, title_align="left",
        ))

    def render_tool_progress(self, tool_name: str,
                              description: str = "") -> None:
        msg = f"[cyan]⏳ {tool_name}[/cyan]"
        if description:
            msg += f" [dim]{description[:60]}[/dim]"
        self.console.print(msg)

    # ── 错误 / 警告 / 信息 ──────────────────────────────────────────

    def render_error(self, message: str) -> None:
        self.console.print(Panel(
            f"[red]{message}[/red]", border_style="red", title="错误",
        ))

    def render_warning(self, message: str) -> None:
        self.console.print(f"[yellow]⚠ {message}[/yellow]")

    def render_info(self, message: str) -> None:
        self.console.print(f"[dim]{message}[/dim]")

    def render_compact_notice(self, summary: str) -> None:
        self.console.print(Panel(
            f"[dim]{summary}[/dim]",
            border_style="magenta", title="上下文压缩", title_align="left",
        ))

    def render_system_message(self, text: str, level: str = "info") -> None:
        colors = {"info": "dim", "warning": "yellow", "error": "red"}
        color = colors.get(level, "dim")
        self.console.print(f"[{color}][{level.upper()}] {text}[/{color}]")

    def render_permission_request(self, tool_name: str,
                                   arguments: Dict[str, Any]) -> None:
        args_text = self._format_args(arguments)
        self.console.print(Panel(
            f"[bold yellow]工具: {tool_name}[/bold yellow]\n"
            f"[dim]参数: {args_text}[/dim]",
            border_style="yellow", title="需要权限确认", title_align="left",
        ))

    # ── 辅助 ────────────────────────────────────────────────────────

    @staticmethod
    def _format_args(args: Dict[str, Any], max_len: int = 100) -> str:
        if not args:
            return "(无参数)"
        keys = list(args.keys())
        if len(keys) == 1:
            v = str(args[keys[0]])
            if len(v) > max_len:
                v = v[:max_len] + "..."
            return f"{keys[0]} = {v}"
        parts = []
        for k in keys[:4]:
            v = str(args[k])
            if len(v) > 40:
                v = v[:40] + "..."
            parts.append(f"{k}={v}")
        if len(keys) > 4:
            parts.append(f"... (+{len(keys) - 4})")
        return ", ".join(parts)

    def get_console(self) -> Console:
        return self.console
