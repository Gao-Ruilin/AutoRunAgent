"""
TriggerManage Tool — 供 Daemon Agent 管理触发器。

通过 DaemonCore 单例操作 TriggerSystem，支持：
- list_triggers: 列出所有触发器
- add_time_trigger: 添加时间触发器
- add_alarm_trigger: 添加闹钟触发器
- remove_trigger: 删除触发器
- enable_trigger / disable_trigger: 启用/禁用触发器
"""

import json
import logging
from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class TriggerManageTool(Tool):
    """管理守护模式触发器。"""

    @property
    def name(self) -> str:
        return "TriggerManage"

    @property
    def description(self) -> str:
        return (
            "管理守护模式的触发器系统。支持添加/删除/列出/启用/禁用触发器。"
            "触发器类型: time(定时触发)、alarm(闹钟触发)。"
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add_time", "add_alarm", "remove", "enable", "disable"],
                    "description": "操作类型: list=列出所有, add_time=添加定时触发, add_alarm=添加闹钟, remove=删除, enable=启用, disable=禁用",
                },
                "name": {
                    "type": "string",
                    "description": "触发器名称（add_time/add_alarm 必填）",
                },
                "interval_seconds": {
                    "type": "integer",
                    "description": "时间触发间隔秒数（add_time 时使用，默认1200=20分钟）",
                    "default": 1200,
                },
                "trigger_type": {
                    "type": "string",
                    "enum": ["once", "daily", "weekly"],
                    "description": "闹钟类型: once=一次性, daily=每天, weekly=每周（add_alarm 时使用）",
                },
                "daily_time": {
                    "type": "string",
                    "description": "每天触发时间 HH:MM（add_alarm trigger_type=daily 时使用）",
                },
                "weekly_days": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "每周触发日期 0=周一...6=周日（add_alarm trigger_type=weekly 时使用）",
                },
                "weekly_time": {
                    "type": "string",
                    "description": "每周触发时间 HH:MM（add_alarm trigger_type=weekly 时使用）",
                },
                "fire_at": {
                    "type": "number",
                    "description": "一次性触发时间戳 epoch seconds（add_alarm trigger_type=once 时使用）",
                },
                "trigger_id": {
                    "type": "string",
                    "description": "触发器ID（remove/enable/disable 时使用）",
                },
            },
            "required": ["action"],
        }

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        """执行触发管理操作。"""
        action = args.get("action", "list")

        # 获取 DaemonCore 单例
        core = self._get_core()
        if core is None:
            return ToolResult(
                data="TriggerManage: DaemonCore 不可用（守护模式未运行）",
                is_error=True,
            )

        try:
            if action == "list":
                return self._list_triggers(core)
            elif action == "add_time":
                return self._add_time(core, args)
            elif action == "add_alarm":
                return self._add_alarm(core, args)
            elif action == "remove":
                return self._remove(core, args)
            elif action in ("enable", "disable"):
                return self._toggle(core, args)
            else:
                return ToolResult(data=f"未知操作: {action}", is_error=True)
        except Exception as e:
            logger.error("TriggerManage call failed: %s", e)
            return ToolResult(data=f"触发器操作失败: {e}", is_error=True)

    def _get_core(self):
        """获取 DaemonCore 单例。"""
        try:
            from AutoRUN_v1.daemon.daemon_core import get_daemon_core
            return get_daemon_core()
        except Exception:
            return None

    def _list_triggers(self, core) -> ToolResult:
        """列出所有触发器。"""
        triggers = core.triggers.get_all_triggers()
        if not triggers:
            return ToolResult(data="当前没有配置任何触发器。")

        lines = ["当前触发器列表:"]
        for t in triggers:
            ttype = t.get("type", "unknown")
            name = t.get("name", "")
            tid = t.get("id", "")
            enabled = "\u2713" if t.get("enabled", True) else "\u2717"

            if ttype == "time":
                interval = t.get("interval_seconds", 1200)
                interval_str = f"{interval // 60}分钟" if interval >= 60 else f"{interval}秒"
                detail = f"每{interval_str}"
            elif ttype == "alarm":
                tt = t.get("trigger_type", "daily")
                if tt == "daily":
                    detail = f"每天 {t.get('daily_time', '?')}"
                elif tt == "weekly":
                    days = t.get("weekly_days", [])
                    day_names = ["一", "二", "三", "四", "五", "六", "日"]
                    day_str = ",".join(day_names[d] for d in days if 0 <= d <= 6)
                    detail = f"每周{day_str} {t.get('weekly_time', '?')}"
                else:
                    detail = f"一次性 ({t.get('fire_at', '?')})"
            else:
                detail = str(ttype)

            lines.append(f"  [{enabled}] {name} - {detail} (ID: {tid})")

        return ToolResult(data="\n".join(lines))

    def _add_time(self, core, args: Dict[str, Any]) -> ToolResult:
        """添加时间触发器。"""
        name = args.get("name", "")
        if not name:
            return ToolResult(data="请提供触发器名称 (name)", is_error=True)

        interval = int(args.get("interval_seconds", 1200))
        trigger = core.triggers.add_time_trigger(name=name, interval_seconds=interval)

        interval_str = f"{interval // 60}分钟" if interval >= 60 else f"{interval}秒"
        return ToolResult(data=f"已添加时间触发器: {name} (每{interval_str}, ID: {trigger.id})")

    def _add_alarm(self, core, args: Dict[str, Any]) -> ToolResult:
        """添加闹钟触发器。"""
        name = args.get("name", "")
        if not name:
            return ToolResult(data="请提供触发器名称 (name)", is_error=True)

        trigger_type = args.get("trigger_type", "daily")

        if trigger_type == "once":
            fire_at = float(args.get("fire_at", 0))
            if fire_at <= 0:
                return ToolResult(data="一次性闹钟需要提供 fire_at (epoch seconds)", is_error=True)
            trigger = core.triggers.add_alarm_trigger(
                name=name, trigger_type="once", fire_at=fire_at,
            )
            return ToolResult(data=f"已添加一次性闹钟: {name} (ID: {trigger.id})")

        elif trigger_type == "daily":
            daily_time = args.get("daily_time", "09:00")
            trigger = core.triggers.add_alarm_trigger(
                name=name, trigger_type="daily", daily_time=daily_time,
            )
            return ToolResult(data=f"已添加每日闹钟: {name} (每天 {daily_time}, ID: {trigger.id})")

        elif trigger_type == "weekly":
            weekly_days = args.get("weekly_days", [])
            weekly_time = args.get("weekly_time", "09:00")
            if not weekly_days:
                return ToolResult(data="每周闹钟需要提供 weekly_days", is_error=True)
            trigger = core.triggers.add_alarm_trigger(
                name=name, trigger_type="weekly",
                weekly_days=weekly_days, weekly_time=weekly_time,
            )
            day_names = ["一", "二", "三", "四", "五", "六", "日"]
            day_str = ",".join(day_names[d] for d in weekly_days if 0 <= d <= 6)
            return ToolResult(data=f"已添加每周闹钟: {name} (每周{day_str} {weekly_time}, ID: {trigger.id})")

        else:
            return ToolResult(data=f"未知闹钟类型: {trigger_type}", is_error=True)

    def _remove(self, core, args: Dict[str, Any]) -> ToolResult:
        """删除触发器。"""
        trigger_id = args.get("trigger_id", "")
        if not trigger_id:
            return ToolResult(data="请提供要删除的 trigger_id", is_error=True)

        if core.triggers.remove_trigger(trigger_id):
            return ToolResult(data=f"已删除触发器: {trigger_id}")
        else:
            return ToolResult(data=f"未找到触发器: {trigger_id}", is_error=True)

    def _toggle(self, core, args: Dict[str, Any]) -> ToolResult:
        """启用/禁用触发器。"""
        trigger_id = args.get("trigger_id", "")
        if not trigger_id:
            return ToolResult(data="请提供 trigger_id", is_error=True)

        action = args.get("action", "")
        if action == "enable":
            if core.triggers.enable_trigger(trigger_id):
                return ToolResult(data=f"已启用触发器: {trigger_id}")
        elif action == "disable":
            if core.triggers.disable_trigger(trigger_id):
                return ToolResult(data=f"已禁用触发器: {trigger_id}")

        return ToolResult(data=f"操作失败: 未找到触发器 {trigger_id}", is_error=True)

    def is_enabled(self) -> bool:
        """仅守护模式可用。"""
        return self._get_core() is not None
