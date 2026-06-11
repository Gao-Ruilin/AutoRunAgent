"""
AutoRUN Daemon Mode - 守护模式核心模块。

守护模式是一个独立的后台进程，作为触发器驱动的 Agent Loop 运行。
与项目模式（现有模式）是兄弟关系。

模块结构:
- daemon_core: 守护模式核心（Agent Loop、生命周期管理）
- memory: 多级记忆系统（短期/中期/长期）
- triggers: 触发器系统（时间驱动/事件驱动/闹钟）
- daemon_webui: 独立 WebUI（FastAPI, 端口 8765）
"""

__version__ = "1.0.0"

from .daemon_webui import DaemonWebUI

__all__ = ["DaemonWebUI"]
