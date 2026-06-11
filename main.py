"""
CLI 参数解析和 Web UI 启动（Win7 适配版）。

仅支持 Web UI 模式，移除了 REPL 和管道模式以适配旧版 Windows 7 系统。

用法:
  autorun              启动 Web UI（默认）
  autorun --web         启动 Web UI
  autorun --setup       重新配置 API
  autorun --version     显示版本
"""

import argparse
import atexit
import logging
import os
import sys
import textwrap
from pathlib import Path


# ── Windows UTF-8 setup (must run at module import time, before any output) ──
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, 'reconfigure'):
                _stream.reconfigure(encoding='utf-8', errors='replace')
            elif hasattr(_stream, 'buffer'):
                import io as _io
                _wrapper = _io.TextIOWrapper(_stream.buffer, encoding='utf-8', errors='replace')
                if _stream is sys.stdout:
                    sys.stdout = _wrapper
                else:
                    sys.stderr = _wrapper
        except Exception:
            pass
    try:
        import ctypes as _ctypes
        _ctypes.windll.kernel32.SetConsoleCP(65001)
        _ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

logger = logging.getLogger(__name__)

# 始终将当前项目根目录注册为 AutoRUN_v1 包，使子模块可正常解析
# 这样无论项目目录叫什么名字、无论通过 python main.py 还是 pip entry point 启动都能正常工作
import types as _types
_project_root = str(Path(__file__).resolve().parent)
if "AutoRUN_v1" not in sys.modules:
    _pkg = _types.ModuleType("AutoRUN_v1")
    _pkg.__file__ = str(Path(_project_root, "__init__.py"))
    _pkg.__path__ = [_project_root]
    _pkg.__package__ = "AutoRUN_v1"
    sys.modules["AutoRUN_v1"] = _pkg
    # 如果有 __init__.py 则执行它
    _init_path = str(Path(_project_root, "__init__.py"))
    try:
        with open(_init_path, "rb") as _f:
            _code = compile(_f.read(), _init_path, "exec")
            exec(_code, _pkg.__dict__)
    except FileNotFoundError:
        pass

VERSION = "1.0.0"


# ═══════════════════════════════════════════════════════════════════════════
# Setup Wizard
# ═══════════════════════════════════════════════════════════════════════════

def _run_setup() -> None:
    """首次运行设置向导 — 交互式配置 API。

    类似于 Claude Code 的首次登录流程。
    配置保存到 ~/.autorun/config.json。
    """
    from AutoRUN_v1.utils.config import (
        get_api_type, get_api_url, get_api_key, get_model,
        set_api_type, set_api_url, save_api_key, set_model,
    )
    from AutoRUN_v1.api.client import reset_client

    current_type = get_api_type()
    current_url = get_api_url() or ""
    current_key = get_api_key() or ""
    current_model = get_model() or ""

    has_config = bool(current_key and current_url)

    _print_box("AutoRUN v" + VERSION + (" — 首次设置" if not has_config else " — 重新配置"))

    print()

    if has_config:
        print("  当前配置:")
        print(f"    API 类型: {current_type}")
        print(f"    API URL:  {current_url}")
        masked = current_key[:8] + "..." + current_key[-4:] if len(current_key) > 12 else "***"
        print(f"    API Key:  {masked}")
        print(f"    模型:     {current_model}")
        print()

    print("  AutoRUN 需要 API 配置才能工作。")
    print("  配置将保存到 ~/.autorun/config.json")
    print()

    # 1. API type
    prompt = "  选择 API 类型 [1=OpenAI 兼容, 2=Anthropic 兼容]" + _default_str(current_type, "openai") + ": "
    choice = _safe_input(prompt).strip().lower()
    if choice in ("2", "anthropic"):
        api_type = "anthropic"
    elif choice in ("1", "openai", ""):
        api_type = "openai"
    else:
        api_type = current_type or "openai"

    set_api_type(api_type)
    default_url = "https://api.openai.com" if api_type == "openai" else "https://api.anthropic.com"
    print(f"  → API 类型: {api_type}")
    print()

    # 2. API URL
    default = current_url or default_url
    prompt = f"  API URL" + _default_str("", default) + ": "
    api_url = _safe_input(prompt).strip()
    if not api_url:
        api_url = default
    if api_url and not api_url.startswith(("http://", "https://")):
        api_url = "https://" + api_url
    set_api_url(api_url)
    print(f"  → API URL: {api_url}")
    print()

    # 3. API Key (masked input if available)
    prompt = "  API Key" + (" (已设置, 回车跳过)" if current_key else "") + ": "
    api_key = _safe_input(prompt).strip()
    if not api_key:
        if current_key:
            api_key = current_key  # Keep existing
        else:
            print()
            print("  ⚠ 警告: 未设置 API Key。你可以稍后通过 /api key <key> 设置。")
            print()
            api_key = ""
    if api_key and api_key != current_key:
        save_api_key(api_key)
        os.environ["AUTORUN_API_KEY"] = api_key
    masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***" if api_key else "(未设置)"
    print(f"  → API Key: {masked}")
    print()

    # 4. Model
    default_model = current_model or "gpt-4o"
    prompt = f"  模型名称" + _default_str("", default_model) + ": "
    model = _safe_input(prompt).strip()
    if not model:
        model = default_model
    set_model(model)
    os.environ["AUTORUN_MODEL"] = model
    print(f"  → 模型: {model}")
    print()

    # Reset client with new config
    try:
        reset_client()
    except Exception:
        logger.debug("reset_client() failed during config setup", exc_info=True)

    print("  ✓ 配置已保存！")
    print()


def _default_str(current: str, default: str) -> str:
    """Format default value hint for prompt."""
    val = current or default
    return f" [默认 {val}]"


def _print_box(title: str) -> None:
    """Print a centered box title (cross-platform)."""
    width = 54
    # Simple top + bottom border
    top = "\u2554" + "\u2550" * (width - 2) + "\u2557"
    mid = "\u2551" + title.center(width - 2) + "\u2551"
    bot = "\u255a" + "\u2550" * (width - 2) + "\u255d"
    print(top)
    print(mid)
    print(bot)


def _safe_input(prompt: str) -> str:
    """Cross-platform input with fallback for non-interactive contexts."""
    try:
        return input(prompt)
    except EOFError:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# CLI Parser
# ═══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="autorun",
        description="AutoRUN — Universal AI coding assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              autorun                   启动 Web UI 并在浏览器打开
              autorun --web             启动 Web UI（同默认）
              autorun --setup           重新配置 API 设置
              autorun --daemon          启动守护模式（后台运行）
              autorun -m gpt-4o         使用指定模型启动
              autorun --port 8080       指定端口启动
        """),
    )

    parser.add_argument(
        "--version", "-V", "-v",
        action="store_true",
        dest="show_version",
        help="显示版本号",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        dest="setup_mode",
        help="重新配置 API 设置（API key、URL、模型等）",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        dest="web_ui",
        help="启动 Web UI 服务器（Win7 默认模式）",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        dest="daemon_mode",
        help="启动守护模式（后台运行，悬浮球 + 独立 WebUI）",
    )
    parser.add_argument(
        "--daemon-no-ball",
        action="store_true",
        dest="daemon_no_ball",
        help="守护模式不启动悬浮球",
    )
    parser.add_argument(
        "--daemon-port",
        type=int,
        dest="daemon_port",
        default=8766,
        help="守护模式 WebUI 端口（默认: 8766）",
    )
    parser.add_argument(
        "--port",
        type=int,
        dest="web_port",
        default=8765,
        help="Web UI 服务器端口（默认: 8765）",
    )
    parser.add_argument(
        "--host",
        type=str,
        dest="web_host",
        default="127.0.0.1",
        help="Web UI 服务器地址（默认: 127.0.0.1）",
    )
    parser.add_argument(
        "-d", "--dir",
        type=str,
        dest="work_dir",
        default=None,
        help="工作目录（默认: 当前目录）",
    )
    parser.add_argument(
        "-m", "--model",
        type=str,
        dest="model",
        default=None,
        help="覆盖默认模型",
    )
    parser.add_argument(
        "--context",
        type=int,
        dest="context_window",
        default=None,
        help="模型上下文窗口大小（tokens 数，默认: 200000）",
    )
    return parser


# ═══════════════════════════════════════════════════════════════════════════
# First-run detection
# ═══════════════════════════════════════════════════════════════════════════

def _check_first_run() -> bool:
    """Return True if this appears to be a first run (no API config)."""
    from AutoRUN_v1.utils.config import get_api_key, get_api_url

    # Skip in dev mode
    if os.environ.get("AUTORUN_DEV") == "1":
        return False

    key = get_api_key()
    url = get_api_url()

    # If both key and URL are missing, it's first run
    if not key or not url:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# LSP cleanup
# ═══════════════════════════════════════════════════════════════════════════

def _cleanup_lsp() -> None:
    """Cleanup: shutdown all LSP servers on exit."""
    try:
        from AutoRUN_v1.services.lsp import shutdown_lsp_server_manager
        import asyncio
        asyncio.run(shutdown_lsp_server_manager())
    except Exception:
        logger.debug("LSP cleanup failed during shutdown", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def cli_main() -> None:
    """主 CLI 入口点（Win7 适配版 — 仅 Web UI 模式）。

    路由到:
    - --setup: 显示设置向导
    - --version / -V: 打印版本
    - 默认: Web UI
    """
    parser = build_parser()
    args = parser.parse_args()

    # ── --version ────────────────────────────────────────────────────────
    if args.show_version:
        print(f"AutoRUN v{VERSION} (Win7 适配版)")
        return

    # ── --setup ───────────────────────────────────────────────────────────
    if args.setup_mode:
        _run_setup()
        return

    # ── First-run check ───────────────────────────────────────────────────
    if _check_first_run():
        print()
        _run_setup()
        print("  启动 AutoRUN Web UI...")
        print()

    # ── Set environment variables from flags ──────────────────────────────
    if args.model:
        os.environ["AUTORUN_MODEL"] = args.model
    if args.context_window:
        os.environ["AUTORUN_CONTEXT_WINDOW"] = str(args.context_window)

    # ── Working directory ─────────────────────────────────────────────────
    if args.work_dir:
        work_dir = os.path.abspath(os.path.expanduser(args.work_dir))
        if not os.path.isdir(work_dir):
            print(f"[错误] 目录不存在: {work_dir}", file=sys.stderr)
            sys.exit(1)
        os.chdir(work_dir)

    # ── Register LSP cleanup ──────────────────────────────────────────────
    atexit.register(_cleanup_lsp)

    # ── Daemon mode ──────────────────────────────────────────────────────
    if args.daemon_mode:
        import subprocess
        daemon_script = os.path.join(os.path.dirname(__file__), "daemon", "run_daemon.py")
        cmd = [sys.executable, daemon_script]
        if args.daemon_no_ball:
            cmd.append("--no-ball")
        if args.daemon_port:
            cmd.extend(["--port", str(args.daemon_port)])
        
        if sys.platform == "win32":
            # Windows: 隐藏窗口后台运行
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.Popen(cmd, startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc = subprocess.Popen(cmd, start_new_session=True)
        
        print(f"守护模式已启动 (PID: {proc.pid})")
        print(f"  WebUI: http://127.0.0.1:{args.daemon_port}")
        print("  悬浮球已启动（查看右下角）")
        print("  使用 'taskkill /PID {0}' 或通过悬浮球退出".format(proc.pid) if sys.platform == "win32" else "  使用 'kill {0}' 或通过悬浮球退出".format(proc.pid))
        return

    # ── Web UI mode (default for Win7) ────────────────────────────────────
    from AutoRUN_v1.ui.web.server import start_web_server

    url = start_web_server(host=args.web_host, port=args.web_port)
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        logger.debug("Failed to open browser for Web UI", exc_info=True)
    print(f"\n  WebUI: {url}\n")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭...")


# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cli_main()
