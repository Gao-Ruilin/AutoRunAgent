"""
守护模式 Agent — 封装 run_query 适配 Core Agent 和 Chat Agent。

复用项目模式的 query.py::run_query，为守护模式提供统一接口。
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from AutoRUN_v1.query import run_query
from AutoRUN_v1.messages.types import Message, UserMessage, create_user_message
from AutoRUN_v1.tools import get_tools
from AutoRUN_v1.tools.base import ToolResult

logger = logging.getLogger(__name__)

# 模块级审批等待注册表（供 daemon_webui.py 的 approval_response 使用）
_pending_approvals: Dict[str, asyncio.Future] = {}

def resolve_approval(approval_id: str, approved: bool, choice: int = 0) -> bool:
    """解析审批请求。由 daemon_webui.py 调用。"""
    future = _pending_approvals.pop(approval_id, None)
    if future and not future.done():
        future.set_result({"approved": approved, "choice": choice})
        return True
    return False

# 守护模式 Agent 可用的工具子集
DAEMON_CORE_TOOLS = {
    "Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch",
    "TaskCreate", "TaskList", "TaskGet", "TaskUpdate",
    "TriggerManage",
}

DAEMON_CHAT_TOOLS = {
    "Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch",
    "TaskCreate", "TaskList", "TaskGet", "TaskUpdate",
    "TriggerManage", "AskUserQuestion",
}


class DaemonAgent:
    """守护模式 Agent 基类，封装 run_query。"""

    def __init__(self, core, agent_type: str = "core"):
        self._core = core
        self._agent_type = agent_type  # "core" | "chat"
        self._conversations: Dict[str, List[Message]] = {}  # chat only

    def _get_system_prompt(self) -> str:
        """获取系统提示词。"""
        from daemon.prompts import build_core_agent_prompt, build_chat_agent_prompt

        if self._agent_type == "core":
            return build_core_agent_prompt(self._core)
        else:
            return build_chat_agent_prompt(self._core)

    def _get_tools(self, ws=None):
        """获取工具集。Chat 模式下替换 AskUserQuestion 为 WebSocket 版本。"""
        enabled = DAEMON_CORE_TOOLS if self._agent_type == "core" else DAEMON_CHAT_TOOLS
        tools = get_tools(enabled_tools=enabled)

        # Chat 模式下，将 AskUserQuestion 替换为 WebSocket 审批版本
        if self._agent_type == "chat" and ws:
            for tool in tools:
                if tool.get("name") == "AskUserQuestion":
                    original_fn = tool["call_fn"]
                    ws_ref = ws
                    async def _ws_ask_user(args, context):
                        questions = args.get("questions", [])
                        if not questions:
                            return await original_fn(args, context)
                        # 通过 WebSocket 发送审批请求，等待用户响应
                        q = questions[0]
                        question_text = q.get("question", "")
                        options = q.get("options", [])
                        header = q.get("header", "确认")
                        approval_id = f"approve_{uuid.uuid4().hex[:8]}"
                        try:
                            future = asyncio.get_running_loop().create_future()
                            _pending_approvals[approval_id] = future
                            await ws_ref.send_json({
                                "type": "ask_approval",
                                "id": approval_id,
                                "question": f"[{header}] {question_text}",
                                "options": [o.get("label", "") for o in options],
                            })
                            # 等待用户响应（最多等 5 分钟）
                            result = await asyncio.wait_for(future, timeout=300)
                            if result.get("approved"):
                                chosen = options[result.get("choice", 0)]
                                return ToolResult(
                                    data=f"用户选择: {chosen.get('label', '')} — {chosen.get('description', '')}",
                                    is_error=False,
                                )
                            else:
                                return ToolResult(
                                    data="用户拒绝了此操作。",
                                    is_error=False,
                                )
                        except asyncio.TimeoutError:
                            _pending_approvals.pop(approval_id, None)
                            return ToolResult(data="审批等待超时。", is_error=True)
                        except Exception as e:
                            _pending_approvals.pop(approval_id, None)
                            logger.warning("WS approval failed: %s", e)
                            return await original_fn(args, context)
                    tool["call_fn"] = _ws_ask_user
                    break

        return tools

    async def run_core(self, event: Any) -> AsyncGenerator[Dict[str, Any], None]:
        """Core Agent 处理一个输入事件。

        Args:
            event: CoreEvent 对象

        Yields:
            与 run_query 相同的事件字典。
        """
        system_prompt = self._get_system_prompt()
        tools = self._get_tools()

        # 构建事件描述
        event_desc = self._format_core_event(event)

        messages = [create_user_message(event_desc)]

        async for event_data in run_query(
            messages=messages,
            system_prompt=system_prompt,
            user_context={"date": datetime.now().strftime("%Y-%m-%d")},
            tools=tools,
        ):
            yield event_data

    async def run_chat(self, conversation_id: str, text: str,
                       ws=None) -> AsyncGenerator[Dict[str, Any], None]:
        """Chat Agent 处理一条用户消息。

        Args:
            conversation_id: 对话 ID（WebSocket 连接标识）
            text: 用户输入文本
            ws: WebSocket 连接（用于 AskUserQuestion 审批）

        Yields:
            与 run_query 相同的事件字典。
        """
        system_prompt = self._get_system_prompt()
        tools = self._get_tools(ws=ws)

        # Daemon Agent 通过 MemorySystem 获取上下文，不维护长对话历史
        # 只保留最近 2 轮交换避免重复 token 消耗
        if conversation_id not in self._conversations:
            self._conversations[conversation_id] = []

        history = self._conversations[conversation_id]
        messages = history[-4:] + [create_user_message(text)] if history else [create_user_message(text)]

        async for event_data in run_query(
            messages=messages,
            system_prompt=system_prompt,
            user_context={"date": datetime.now().strftime("%Y-%m-%d")},
            tools=tools,
        ):
            yield event_data

        # 只保存最近一轮（用户消息），AI 回复通过 MemorySystem 记忆
        self._conversations[conversation_id] = history[-3:] + [create_user_message(text)]

    def _format_core_event(self, event: Any) -> str:
        """格式化 CoreEvent 为 Agent 可理解的文本。"""
        etype = event.type
        data = event.data

        if etype == "startup":
            name = data.get("trigger_name", "守护启动")
            return (
                f"[系统事件] 守护模式启动触发: {name}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"请评估当前环境，判断是否需要执行任何任务。"
            )

        elif etype == "trigger_fired":
            name = data.get("trigger_name", "未知触发器")
            ttype = data.get("trigger_type", "unknown")
            return (
                f"[触发器事件] 触发器触发: {name} (类型: {ttype})\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"请评估此触发事件，判断是否需要行动。"
            )

        elif etype == "subprocess_report":
            task_id = data.get("task_id", "")
            sub_event = data.get("event", {})
            sub_type = sub_event.get("event_type", "")
            message = sub_event.get("message", "")
            error = sub_event.get("error", "")
            parts = [f"[子进程报告] 任务 {task_id[:8]} 报告: {sub_type}"]
            if message:
                parts.append(f"消息: {message}")
            if error:
                parts.append(f"错误: {error}")
            return "\n".join(parts)

        elif etype == "subprocess_done":
            task_id = data.get("task_id", "")
            action = data.get("action", "")
            return f"[子进程完成] 任务 {task_id[:8]} 已完成 (action: {action})"

        elif etype == "chat_push":
            message = data.get("message", "")
            return f"[Chat Agent 推送] {message}"

        else:
            return f"[事件] 类型: {etype}, 数据: {json.dumps(data, ensure_ascii=False)[:500]}"
