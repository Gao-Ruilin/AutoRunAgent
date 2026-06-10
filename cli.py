#!/usr/bin/env python3
"""
AutoRUN_v1 — AutoRUN Python 实现入口点。

对应 src/entrypoints/cli.tsx — 主入口点:
1. 处理快速路径标志（--version, -v）
2. 路由到 main.py 启动 REPL/web/pipe 模式
"""

import os
import sys
from pathlib import Path

# 始终将当前项目根目录注册为 AutoRUN_v1 包，使子模块可正常解析
import types as _types
_project_root = str(Path(__file__).resolve().parent)
if "AutoRUN_v1" not in sys.modules:
    _pkg = _types.ModuleType("AutoRUN_v1")
    _pkg.__file__ = str(Path(_project_root, "__init__.py"))
    _pkg.__path__ = [_project_root]
    _pkg.__package__ = "AutoRUN_v1"
    sys.modules["AutoRUN_v1"] = _pkg
    _init_path = str(Path(_project_root, "__init__.py"))
    try:
        with open(_init_path, "rb") as _f:
            _code = compile(_f.read(), _init_path, "exec")
            exec(_code, _pkg.__dict__)
    except FileNotFoundError:
        pass

VERSION = "1.0.0"


def _setup_windows_utf8() -> None:
    """Ensure UTF-8 encoding on Windows so copy/paste preserves non-ASCII text.

    Without this, Chinese/Japanese/emoji etc. get garbled when copied from the
    terminal because the console uses the system code page (e.g. CP936/GBK).
    """
    import io as _io
    # PYTHONUTF8 ensures subprocesses and the Python runtime use UTF-8
    os.environ.setdefault("PYTHONUTF8", "1")
    # Reconfigure stdio for UTF-8
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, 'reconfigure'):
                _stream.reconfigure(encoding='utf-8', errors='replace')
            elif hasattr(_stream, 'buffer'):
                setattr(sys, _stream is sys.stdout and 'stdout' or 'stderr',
                        _io.TextIOWrapper(_stream.buffer, encoding='utf-8', errors='replace'))
        except Exception:
            pass
    # Set console code page to UTF-8 (65001)
    try:
        import ctypes as _ctypes
        _ctypes.windll.kernel32.SetConsoleCP(65001)
        _ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


def main() -> None:
    """入口点 — 解析标志并路由到相应的处理器。"""
    args = sys.argv[1:]

    # -- Windows UTF-8 setup (must happen early, before any output) --
    if sys.platform == "win32":
        _setup_windows_utf8()

    # --version/-v 快速路径
    if len(args) == 1 and args[0] in ("--version", "-v", "-V"):
        print(f"{VERSION} (AutoRUN_v1)")
        return

    # 确保 .env 加载（如果可用）
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # 路由到主 CLI
    from AutoRUN_v1.main import cli_main
    cli_main()


if __name__ == "__main__":
    main()
