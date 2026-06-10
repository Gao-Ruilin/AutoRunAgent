"""
工作流触发器服务 — 支持时间触发、事件触发、API 调用触发。

触发器类型:
  - call:   HTTP API 调用触发（外部程序通过 REST 端点触发）
  - watch:  文件系统变更触发（监控指定文件/目录）
  - cron:   定时触发（类 cron 表达式）

紧急属性:
  触发器可设置 emergency_timeout 秒数。在紧急模式下，如果工作流内的 confirm
  步骤在 X 秒内未收到用户响应，门控Agent 将自行决策并推进工作流。

储存位置: ~/.autorun/triggers/<trigger_name>.json
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── 运行时常量 ──────────────────────────────────────────────────────────────

TRIGGERS_DIR = os.path.expanduser("~/.autorun/triggers")

# 活跃触发器实例: trigger_name → TriggerInstance
_active_triggers: Dict[str, "TriggerInstance"] = {}

# 触发器执行回调: session_id → callable(name, workflow_name, trigger_type)
_on_trigger_fire: Dict[str, Any] = {}

# 文件监控任务
_file_watcher_task: Optional[asyncio.Task] = None
# cron 定时任务
_cron_task: Optional[asyncio.Task] = None


# ── 触发器数据模型 ──────────────────────────────────────────────────────────

def list_triggers() -> List[Dict[str, Any]]:
    """列出所有已定义的触发器。"""
    if not os.path.isdir(TRIGGERS_DIR):
        return []
    result = []
    for f in sorted(Path(TRIGGERS_DIR).glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            result.append({
                "name": data.get("name", f.stem),
                "workflow": data.get("workflow", ""),
                "type": data.get("type", "call"),
                "description": data.get("description", ""),
                "emergency_timeout": data.get("emergency_timeout", 0),
                "enabled": data.get("enabled", True),
            })
        except (json.JSONDecodeError, OSError):
            pass
    return result


def get_trigger(name: str) -> Optional[Dict[str, Any]]:
    """获取指定触发器的完整定义。"""
    path = os.path.join(TRIGGERS_DIR, f"{name}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_trigger(definition: Dict[str, Any]) -> str:
    """保存或更新触发器定义。返回文件路径。"""
    os.makedirs(TRIGGERS_DIR, exist_ok=True)
    name = definition.get("name", "")
    if not name:
        raise ValueError("触发器必须提供 name")
    path = os.path.join(TRIGGERS_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(definition, f, ensure_ascii=False, indent=2)
    return path


def delete_trigger(name: str) -> bool:
    """删除触发器定义。"""
    path = os.path.join(TRIGGERS_DIR, f"{name}.json")
    if not os.path.isfile(path):
        return False
    os.remove(path)
    # 如果正在运行，也停止它
    if name in _active_triggers:
        _active_triggers.pop(name).stop()
    return True


# ── 触发器实例 ──────────────────────────────────────────────────────────────

class TriggerInstance:
    """一个运行中的触发器实例。"""

    def __init__(self, definition: Dict[str, Any], fire_callback: Callable):
        self.name: str = definition["name"]
        self.workflow: str = definition.get("workflow", "")
        self.trigger_type: str = definition.get("type", "call")
        self.config: Dict[str, Any] = definition.get("config", {})
        self.emergency_timeout: int = definition.get("emergency_timeout", 0)
        self.enabled: bool = definition.get("enabled", True)
        self._fire_callback = fire_callback
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """启动触发器监听。"""
        if not self.enabled:
            return
        if self.trigger_type == "watch":
            self._task = asyncio.create_task(self._watch_loop())
        elif self.trigger_type == "cron":
            self._task = asyncio.create_task(self._cron_loop())

    def stop(self):
        """停止触发器。"""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()

    async def fire(self, meta: Optional[Dict[str, Any]] = None) -> str:
        """手动触发此工作流。返回触发 ID。"""
        trigger_id = str(uuid.uuid4())[:8]
        logger.info(
            "触发器 [%s] → 工作流 [%s] (紧急=%ds, meta=%s)",
            self.name, self.workflow, self.emergency_timeout, meta or {}
        )
        if self._fire_callback:
            await self._fire_callback(
                trigger_name=self.name,
                workflow_name=self.workflow,
                trigger_type=self.trigger_type,
                emergency_timeout=self.emergency_timeout,
                meta=meta or {},
                trigger_id=trigger_id,
            )
        return trigger_id

    async def _watch_loop(self):
        """文件系统监控循环。"""
        watch_path = self.config.get("path", "")
        if not watch_path or not os.path.exists(watch_path):
            logger.warning("触发器 [%s] 监控路径不存在: %s", self.name, watch_path)
            return

        last_mtime: Dict[str, float] = {}
        # 初始扫描
        self._snapshot(watch_path, last_mtime)

        poll_interval = self.config.get("poll_seconds", 5)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval)
                break  # 收到停止信号
            except asyncio.TimeoutError:
                pass  # 超时, 继续检查

            changed = self._snapshot(watch_path, last_mtime)
            if changed:
                logger.info("触发器 [%s] 检测到文件变更: %s", self.name, changed)
                await self.fire(meta={"changed_files": changed})

    def _snapshot(self, root: str, state: Dict[str, float]) -> List[str]:
        """扫描目录树, 检测变化。返回变更的文件列表。"""
        changed = []
        current: Dict[str, float] = {}
        try:
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    try:
                        mtime = os.path.getmtime(fp)
                        current[fp] = mtime
                    except OSError:
                        continue
        except (OSError, PermissionError):
            return changed

        for fp, mtime in current.items():
            if fp not in state or state[fp] != mtime:
                changed.append(fp)
        # 检测删除
        for fp in state:
            if fp not in current:
                changed.append(fp + " (已删除)")

        state.clear()
        state.update(current)
        return changed

    async def _cron_loop(self):
        """定时触发循环（简化版, 支持 interval_seconds 和 daily_at）。"""
        interval = self.config.get("interval_seconds", 0)
        daily_at = self.config.get("daily_at", "")  # 格式: "HH:MM"

        while not self._stop_event.is_set():
            wait_seconds = self._next_wait(interval, daily_at)
            if wait_seconds <= 0:
                wait_seconds = 60  # fallback
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                break
            except asyncio.TimeoutError:
                await self.fire()

    def _next_wait(self, interval: int, daily_at: str) -> int:
        """计算到下次触发的等待秒数。"""
        if interval > 0:
            return interval
        if daily_at:
            try:
                h, m = daily_at.split(":")
                target_h, target_m = int(h), int(m)
            except (ValueError, TypeError):
                return 3600
            now = datetime.now()
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                from datetime import timedelta
                target += timedelta(days=1)
            return int((target - now).total_seconds())
        return 3600  # 默认每小时


# ── 触发器管理 ──────────────────────────────────────────────────────────────

async def load_and_start_triggers(fire_callback: Callable):
    """从磁盘加载所有启用触发器并启动监听。"""
    if not os.path.isdir(TRIGGERS_DIR):
        return

    for f in sorted(Path(TRIGGERS_DIR).glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                definition = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        if not definition.get("enabled", True):
            continue
        if definition.get("type") not in ("watch", "cron"):
            continue  # call 类型按需触发

        instance = TriggerInstance(definition, fire_callback)
        instance.start()
        _active_triggers[definition["name"]] = instance
        logger.info("已启动触发器: %s (类型=%s)", definition["name"], definition.get("type"))


async def fire_trigger_by_name(name: str, meta: Optional[Dict] = None) -> Optional[str]:
    """通过名称触发一个 call 类型触发器。返回 trigger_id, 未找到返回 None。"""
    definition = get_trigger(name)
    if not definition:
        return None
    if definition.get("type") != "call":
        return None

    instance = TriggerInstance(definition, lambda **kw: _call_callback(**kw))
    return await instance.fire(meta=meta)


# 全局回调注册（由 server.py 设置）
_global_callback: Optional[Callable] = None


def register_trigger_callback(cb: Callable):
    """注册全局触发器回调。"""
    global _global_callback
    _global_callback = cb


async def _call_callback(**kwargs):
    """通过全局回调触发工作流。"""
    if _global_callback:
        await _global_callback(**kwargs)
    else:
        logger.warning("触发器回调未注册, 丢弃触发: %s", kwargs)


def stop_all_triggers():
    """停止所有运行中的触发器。"""
    for instance in _active_triggers.values():
        instance.stop()
    _active_triggers.clear()
    logger.info("已停止所有触发器")


def get_trigger_status() -> Dict[str, Any]:
    """获取触发器运行状态。"""
    return {
        "active_count": len(_active_triggers),
        "active_names": list(_active_triggers.keys()),
        "definitions": list_triggers(),
    }
