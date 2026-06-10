"""
Daemon Mode - 触发器系统。

触发器类型：
- 时间驱动：默认每20分钟检查一次，支持自定义间隔
- 事件驱动：可通过文件变更、Web API等触发
- 闹钟功能：可设定在特定日期、每天、每周固定几天触发
- 支持休眠/唤醒

所有触发器配置持久化到 ~/.autorun/daemon/triggers.json。
"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from AutoRUN_v1.utils.env_utils import get_autorun_config_dir
from AutoRUN_v1.utils.file_lock import FileLock

logger = logging.getLogger(__name__)

# 默认检查间隔（秒）
DEFAULT_CHECK_INTERVAL = 20 * 60  # 20分钟


@dataclass
class TimeTrigger:
    """时间驱动触发器 — 按固定间隔触发。"""
    id: str
    name: str
    interval_seconds: int = DEFAULT_CHECK_INTERVAL  # 间隔秒数
    enabled: bool = True
    last_fired: float = 0.0  # epoch seconds
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_due(self) -> bool:
        """检查是否应该触发。"""
        if not self.enabled:
            return False
        if self.last_fired <= 0:
            return True  # 从未触发过，立即触发
        return (time.time() - self.last_fired) >= self.interval_seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "time",
            "id": self.id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "last_fired": self.last_fired,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TimeTrigger":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            interval_seconds=d.get("interval_seconds", DEFAULT_CHECK_INTERVAL),
            enabled=d.get("enabled", True),
            last_fired=d.get("last_fired", 0.0),
            metadata=d.get("metadata", {}),
        )


@dataclass
class AlarmTrigger:
    """闹钟触发器 — 在特定时间/周期触发。"""
    id: str
    name: str
    # 触发时间定义
    trigger_type: str = "once"  # once | daily | weekly
    # 对于 once: 具体 datetime (epoch)
    fire_at: float = 0.0
    # 对于 daily: 一天中的时间 (HH:MM, 如 "09:00")
    daily_time: str = ""
    # 对于 weekly: 一周中的几天 (0=Mon, 1=Tue, ..., 6=Sun)
    weekly_days: List[int] = field(default_factory=list)
    weekly_time: str = ""  # HH:MM

    enabled: bool = True
    last_fired: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_due(self) -> bool:
        """检查是否应该触发。"""
        if not self.enabled:
            return False
        if self.trigger_type == "once":
            return self.last_fired <= 0 and time.time() >= self.fire_at
        elif self.trigger_type == "daily":
            return self._is_daily_due()
        elif self.trigger_type == "weekly":
            return self._is_weekly_due()
        return False

    def _is_daily_due(self) -> bool:
        """检查每天定时是否该触发。"""
        if not self.daily_time:
            return False
        now = datetime.now()
        today_target = now.replace(
            hour=int(self.daily_time[:2]),
            minute=int(self.daily_time[3:5]),
            second=0, microsecond=0,
        )
        target_epoch = today_target.timestamp()
        # 如果今天的目标时间已过，检查上次触发是否在今天之前
        return time.time() >= target_epoch and self.last_fired < target_epoch

    def _is_weekly_due(self) -> bool:
        """检查每周定时是否该触发。"""
        if not self.weekly_time or not self.weekly_days:
            return False
        now = datetime.now()
        # 今天的星期几 (0=Mon in Python's datetime.weekday())
        today_weekday = now.weekday()
        if today_weekday not in self.weekly_days:
            return False

        today_target = now.replace(
            hour=int(self.weekly_time[:2]),
            minute=int(self.weekly_time[3:5]),
            second=0, microsecond=0,
        )
        target_epoch = today_target.timestamp()
        return time.time() >= target_epoch and self.last_fired < target_epoch

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "alarm",
            "id": self.id,
            "name": self.name,
            "trigger_type": self.trigger_type,
            "fire_at": self.fire_at,
            "daily_time": self.daily_time,
            "weekly_days": self.weekly_days,
            "weekly_time": self.weekly_time,
            "enabled": self.enabled,
            "last_fired": self.last_fired,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AlarmTrigger":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            trigger_type=d.get("trigger_type", "once"),
            fire_at=d.get("fire_at", 0.0),
            daily_time=d.get("daily_time", ""),
            weekly_days=d.get("weekly_days", []),
            weekly_time=d.get("weekly_time", ""),
            enabled=d.get("enabled", True),
            last_fired=d.get("last_fired", 0.0),
            metadata=d.get("metadata", {}),
        )


@dataclass
class EventTrigger:
    """事件驱动触发器 — 由外部事件触发。"""
    id: str
    name: str
    event_type: str = ""  # file_change | web_api | custom
    config: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    last_fired: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "event",
            "id": self.id,
            "name": self.name,
            "event_type": self.event_type,
            "config": self.config,
            "enabled": self.enabled,
            "last_fired": self.last_fired,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EventTrigger":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            event_type=d.get("event_type", ""),
            config=d.get("config", {}),
            enabled=d.get("enabled", True),
            last_fired=d.get("last_fired", 0.0),
            metadata=d.get("metadata", {}),
        )


class TriggerSystem:
    """触发器管理 — 统一管理所有类型的触发器。

    线程安全。
    """

    def __init__(self, save_dir: Optional[str] = None):
        self._lock = threading.RLock()
        self._save_dir = save_dir or os.path.join(
            get_autorun_config_dir(), "daemon"
        )
        os.makedirs(self._save_dir, exist_ok=True)
        self._save_path = os.path.join(self._save_dir, "triggers.json")

        # 触发器存储
        self._time_triggers: Dict[str, TimeTrigger] = {}
        self._alarm_triggers: Dict[str, AlarmTrigger] = {}
        self._event_triggers: Dict[str, EventTrigger] = {}

        # 休眠状态
        self._sleeping: bool = False
        self._sleep_until: float = 0.0  # epoch seconds

        # 回调
        self._on_trigger_callbacks: List[Callable] = []

        # 加载
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载触发器配置。"""
        if not os.path.exists(self._save_path):
            self._ensure_default_trigger()
            return
        try:
            with open(self._save_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            with self._lock:
                for item in data.get("time_triggers", []):
                    t = TimeTrigger.from_dict(item)
                    self._time_triggers[t.id] = t
                for item in data.get("alarm_triggers", []):
                    t = AlarmTrigger.from_dict(item)
                    self._alarm_triggers[t.id] = t
                for item in data.get("event_triggers", []):
                    t = EventTrigger.from_dict(item)
                    self._event_triggers[t.id] = t
                self._sleeping = data.get("sleeping", False)
                self._sleep_until = data.get("sleep_until", 0.0)

            logger.info(
                "Triggers loaded: time=%d, alarm=%d, event=%d",
                len(self._time_triggers), len(self._alarm_triggers),
                len(self._event_triggers),
            )
        except Exception as e:
            logger.warning("Failed to load triggers: %s", e)
            self._ensure_default_trigger()

    def _ensure_default_trigger(self) -> None:
        """确保至少有一个默认的时间触发器。"""
        import uuid
        default = TimeTrigger(
            id=f"default_{uuid.uuid4().hex[:8]}",
            name="默认每20分钟检查",
            interval_seconds=DEFAULT_CHECK_INTERVAL,
        )
        self._time_triggers[default.id] = default
        self.save()

    def save(self) -> None:
        """持久化触发器配置。"""
        with self._lock:
            data = {
                "time_triggers": [t.to_dict() for t in self._time_triggers.values()],
                "alarm_triggers": [t.to_dict() for t in self._alarm_triggers.values()],
                "event_triggers": [t.to_dict() for t in self._event_triggers.values()],
                "sleeping": self._sleeping,
                "sleep_until": self._sleep_until,
                "saved_at": time.time(),
            }
        try:
            with FileLock(self._save_path):
                with open(self._save_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save triggers: %s", e)

    # ── Trigger Registration ─────────────────────────────────────────────────────

    def add_time_trigger(self, name: str, interval_seconds: int = DEFAULT_CHECK_INTERVAL,
                         **kwargs) -> TimeTrigger:
        """添加时间触发器。"""
        import uuid
        trigger = TimeTrigger(
            id=f"time_{uuid.uuid4().hex[:8]}",
            name=name,
            interval_seconds=interval_seconds,
            metadata=kwargs,
        )
        with self._lock:
            self._time_triggers[trigger.id] = trigger
        self.save()
        return trigger

    def add_alarm_trigger(
        self, name: str,
        trigger_type: str = "daily",
        fire_at: float = 0.0,
        daily_time: str = "",
        weekly_days: Optional[List[int]] = None,
        weekly_time: str = "",
        **kwargs,
    ) -> AlarmTrigger:
        """添加闹钟触发器。"""
        import uuid
        trigger = AlarmTrigger(
            id=f"alarm_{uuid.uuid4().hex[:8]}",
            name=name,
            trigger_type=trigger_type,
            fire_at=fire_at,
            daily_time=daily_time,
            weekly_days=weekly_days or [],
            weekly_time=weekly_time,
            metadata=kwargs,
        )
        with self._lock:
            self._alarm_triggers[trigger.id] = trigger
        self.save()
        return trigger

    def add_event_trigger(self, name: str, event_type: str,
                          config: Optional[Dict[str, Any]] = None,
                          **kwargs) -> EventTrigger:
        """添加事件触发器。"""
        import uuid
        trigger = EventTrigger(
            id=f"event_{uuid.uuid4().hex[:8]}",
            name=name,
            event_type=event_type,
            config=config or {},
            metadata=kwargs,
        )
        with self._lock:
            self._event_triggers[trigger.id] = trigger
        self.save()
        return trigger

    # ── Trigger Management ───────────────────────────────────────────────────────

    def remove_trigger(self, trigger_id: str) -> bool:
        """移除触发器。"""
        with self._lock:
            for d in (self._time_triggers, self._alarm_triggers, self._event_triggers):
                if trigger_id in d:
                    del d[trigger_id]
                    self.save()
                    return True
        return False

    def get_trigger(self, trigger_id: str) -> Optional[Any]:
        """获取指定触发器。"""
        with self._lock:
            for d in (self._time_triggers, self._alarm_triggers, self._event_triggers):
                if trigger_id in d:
                    return d[trigger_id]
        return None

    def enable_trigger(self, trigger_id: str) -> bool:
        """启用触发器。"""
        trigger = self.get_trigger(trigger_id)
        if trigger:
            with self._lock:
                trigger.enabled = True
            self.save()
            return True
        return False

    def disable_trigger(self, trigger_id: str) -> bool:
        """禁用触发器。"""
        trigger = self.get_trigger(trigger_id)
        if trigger:
            with self._lock:
                trigger.enabled = False
            self.save()
            return True
        return False

    def get_all_triggers(self) -> List[Dict[str, Any]]:
        """获取所有触发器（用于 WebUI）。"""
        with self._lock:
            result = []
            for t in self._time_triggers.values():
                result.append(t.to_dict())
            for t in self._alarm_triggers.values():
                result.append(t.to_dict())
            for t in self._event_triggers.values():
                result.append(t.to_dict())
        return result

    # ── Sleep / Wake ─────────────────────────────────────────────────────────────

    @property
    def is_sleeping(self) -> bool:
        """是否处于休眠状态。"""
        with self._lock:
            if self._sleeping and self._sleep_until > 0:
                if time.time() >= self._sleep_until:
                    # 休眠时间到，自动唤醒
                    self._sleeping = False
                    self._sleep_until = 0.0
                    return False
            return self._sleeping

    def sleep(self, duration_seconds: float = 0) -> None:
        """进入休眠模式。

        Args:
            duration_seconds: 休眠时长（秒）。0 表示无限期休眠（直到手动唤醒）。
        """
        with self._lock:
            self._sleeping = True
            self._sleep_until = (
                time.time() + duration_seconds if duration_seconds > 0 else float("inf")
            )
            logger.info("Entering sleep mode (until=%s)", self._sleep_until)
        self.save()

    def wake(self) -> None:
        """唤醒。"""
        with self._lock:
            self._sleeping = False
            self._sleep_until = 0.0
            logger.info("Waking from sleep mode")
        self.save()

    # ── Trigger Callbacks ────────────────────────────────────────────────────────

    def on_trigger(self, callback: Callable) -> None:
        """注册触发器回调。

        callback 应接受 (trigger, trigger_context: Dict) 参数。
        """
        self._on_trigger_callbacks.append(callback)

    async def _fire_trigger(self, trigger: Any) -> None:
        """触发回调。"""
        context = {
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "trigger_type": type(trigger).__name__,
            "fired_at": time.time(),
        }
        for callback in self._on_trigger_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(trigger, context)
                else:
                    callback(trigger, context)
            except Exception as e:
                logger.error("Trigger callback failed: %s", e)

    # ── Main Check Loop ──────────────────────────────────────────────────────────

    async def check_and_fire(self) -> List[Dict[str, Any]]:
        """检查所有触发器，触发应执行的。

        Returns:
            已触发的触发器信息列表。
        """
        if self.is_sleeping:
            return []

        fired = []
        now = time.time()

        with self._lock:
            # 检查时间触发器
            for trigger in list(self._time_triggers.values()):
                if trigger.is_due():
                    trigger.last_fired = now
                    fired.append({
                        "trigger_id": trigger.id,
                        "trigger_name": trigger.name,
                        "trigger_type": "time",
                        "fired_at": now,
                    })

            # 检查闹钟触发器
            for trigger in list(self._alarm_triggers.values()):
                if trigger.is_due():
                    trigger.last_fired = now
                    fired.append({
                        "trigger_id": trigger.id,
                        "trigger_name": trigger.name,
                        "trigger_type": "alarm",
                        "fired_at": now,
                    })

        if fired:
            self.save()

        # 触发回调
        for info in fired:
            trigger = self.get_trigger(info["trigger_id"])
            if trigger:
                await self._fire_trigger(trigger)

        return fired

    def fire_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """手动触发事件（用于外部事件如 Web API）。

        Args:
            event_type: 事件类型（需匹配已注册的事件触发器）。
            payload: 事件附带数据。
        """
        with self._lock:
            matching = [
                t for t in self._event_triggers.values()
                if t.enabled and t.event_type == event_type
            ]
        if matching:
            for trigger in matching:
                asyncio.ensure_future(self._fire_trigger(trigger))
                logger.info("Event triggered: %s (type=%s)", trigger.name, event_type)