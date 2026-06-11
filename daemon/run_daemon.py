"""
守护模式启动入口。

用法:
    python daemon/run_daemon.py            启动守护模式（后台进程 + 悬浮球 + WebUI）
    python daemon/run_daemon.py --no-ball   不启动悬浮球
    python daemon/run_daemon.py --no-webui  不启动 WebUI
    autorun --daemon                       通过 CLI 启动

开机自启:
    首次运行后，守护模式会提示是否启用开机自启。
    也可通过 WebUI (http://127.0.0.1:8765) 或悬浮球手动启用。
"""

import asyncio
import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# ── 设置包路径（与 main.py 相同的机制）───────────────────────────────────────
_project_root = str(Path(__file__).resolve().parent.parent)
if "AutoRUN_v1" not in sys.modules:
    import types as _types
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

# 设置 UTF-8 编码
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from daemon.daemon_core import DaemonCore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DAEMON] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("daemon")


async def _run_webui(core, port=8765):
    """在后台启动独立 WebUI。"""
    from daemon.daemon_webui import DaemonWebUI
    webui = DaemonWebUI(core, port=port)
    logger.info("守护模式 WebUI: http://127.0.0.1:%d", port)
    await webui.start()


def _run_ball(core):
    """在独立线程启动悬浮球（PyQt 必须主线程）。"""
    try:
        from daemon.daemon_ball import DaemonBall
        logger.info("启动悬浮球...")
        DaemonBall.run(core)
    except Exception as e:
        logger.error("悬浮球启动失败: %s", e, exc_info=True)
        print(f"[警告] 悬浮球启动失败（PyQt5 可能未正确安装或显示不可用）: {e}", file=sys.stderr)


async def main(no_ball=False, no_webui=False, port=8766):
    """启动守护模式所有组件。"""
    core = DaemonCore()
    tasks = []

    # 1. 启动核心 Agent Loop
    logger.info("启动守护核心...")
    await core.start()

    # 2. 启动 WebUI（后台 asyncio task）
    if not no_webui:
        webui_task = asyncio.create_task(_run_webui(core, port))
        tasks.append(webui_task)

    # 3. 启动悬浮球（独立线程，PyQt 需要）
    if not no_ball:
        ball_thread = threading.Thread(
            target=_run_ball, args=(core,), daemon=True
        )
        ball_thread.start()

    # 4. 等待关闭信号
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("收到关闭信号，正在安全退出...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, ValueError):
            # Windows 不支持 add_signal_handler
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("正在停止守护模式...")
        await core.stop()
        for t in tasks:
            t.cancel()
        logger.info("守护模式已停止")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AutoRUN 守护模式")
    parser.add_argument("--no-ball", action="store_true", help="不启动悬浮球")
    parser.add_argument("--no-webui", action="store_true", help="不启动 WebUI")
    parser.add_argument("--port", type=int, default=8766, help="WebUI 端口 (默认 8766)")
    args = parser.parse_args()

    asyncio.run(main(no_ball=args.no_ball, no_webui=args.no_webui, port=args.port))
