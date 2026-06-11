"""
Daemon Mode - 守护模式核心 (DaemonCore)。

DaemonCore 是守护模式的中央控制器，职责：
1. Agent Loop: 触发器驱动的主循环，判断情况并执行任务
2. 子进程管理: 创建"项目模式"子进程执行复杂任务，等待 report/crash/超时
3. 记忆管理: 集成多级记忆系统
4. 触发器系统: 集成时间/事件/闹钟触发器
5. 生命周期管理: 启动、暂停、恢复、关闭
6. 崩溃恢复: 状态持久化，重启后从上次状态继续

与项目模式的关系：
- 守护模式是长驻后台进程，项目模式是用户交互式会话
- 守护模式触发后可创建项目模式子进程处理复杂任务
- 不通过轮询跟踪子进程，而是通过子进程的 report/crash/超时事件

设计原则：
- 使用 asyncio 进行异步操作
- 所有状态持久化到 ~/.autorun/daemon/state.json
- token 消耗最小化
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from AutoRUN_v1.utils.config import get_api_key, get_api_url, get_model
from AutoRUN_v1.utils.env_utils import get_autorun_config_dir
from AutoRUN_v1.utils.file_lock import FileLock

from .memory import MemorySystem
from .triggers import TriggerSystem, TimeTrigger, AlarmTrigger, EventTrigger

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────────────

DEFAULT_LOOP_INTERVAL = 5.0  # 主循环检查间隔（秒）
DEFAULT_TASK_TIMEOUT = 15 * 60  # 子进程任务默认超时（秒）
MAX_CONCURRENT_TASKS = 3  # 最大并发子任务数
AUTORUN_ENTRY = [sys.executable, "-m", "AutoRUN_v1.main"]  # 项目模式入口


class DaemonState(Enum):
    """守护进程状态。"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class SubprocessTask:
    """子进程任务的表示。"""
    id: str
    prompt: str  # 传递给项目模式的指令
    state: str = "pending"  # pending | running | completed | crashed | timeout
    pid: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    timeout: int = DEFAULT_TASK_TIMEOUT
    result: str = ""
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        if self.started_at <= 0:
            return 0.0
        if self.finished_at > 0:
            return self.finished_at - self.started_at
        return time.time() - self.started_at

    @property
    def is_timed_out(self) -> bool:
        return self.state == "running" and self.elapsed > self.timeout

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "state": self.state,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timeout": self.timeout,
            "elapsed": self.elapsed,
            "result": self.result[:2000] if self.result else "",
            "error": self.error[:1000] if self.error else "",
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SubprocessTask":
        return cls(
            id=d.get("id", ""),
            prompt=d.get("prompt", ""),
            state=d.get("state", "pending"),
            pid=d.get("pid", 0),
            started_at=d.get("started_at", 0.0),
            finished_at=d.get("finished_at", 0.0),
            timeout=d.get("timeout", DEFAULT_TASK_TIMEOUT),
            result=d.get("result", ""),
            error=d.get("error", ""),
            metadata=d.get("metadata", {}),
        )


class DaemonCore:
    """守护模式核心控制器。

    用法:
        core = DaemonCore()
        await core.start()    # 启动守护循环
        ...
        await core.stop()     # 停止守护循环
    """

    def __init__(self, config_dir: Optional[str] = None):
        self._config_dir = config_dir or os.path.join(
            get_autorun_config_dir(), "daemon"
        )
        os.makedirs(self._config_dir, exist_ok=True)

        # 状态
        self._state: DaemonState = DaemonState.STOPPED
        self._lock = threading.RLock()
        self._session_id = uuid.uuid4().hex[:12]

        # 子系统
        self.memory = MemorySystem(save_dir=self._config_dir)
        self.triggers = TriggerSystem(save_dir=self._config_dir)

        # 子进程任务管理
        self._tasks: Dict[str, SubprocessTask] = {}
        self._task_lock = asyncio.Lock()

        # 循环控制
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._loop_interval = DEFAULT_LOOP_INTERVAL

        # 回调
        self._on_state_change: List[Callable] = []
        self._on_task_complete: List[Callable] = []

        # 调用计数（用于悬浮球显示）
        self._api_call_count: int = 0
        self._trigger_count: int = 0
        self._task_count: int = 0
        self._started_at: float = 0.0

        # 启动时的初始触发（启动即是第一次检查）
        self._needs_initial_trigger: bool = True

        # 崩溃恢复 / 看门狗
        self._crash_count: int = 0
        self._max_crash_restarts: int = 3
        self._watchdog_enabled: bool = True

        # 注册触发器回调
        self.triggers.on_trigger(self._on_trigger_fired)

        # 加载持久化状态
        self._load_state()

    # ── State Persistence ────────────────────────────────────────────────────────

    def _state_path(self) -> str:
        return os.path.join(self._config_dir, "state.json")

    def _load_state(self) -> None:
        """加载持久化状态（崩溃恢复）。"""
        path = self._state_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            with self._lock:
                self._session_id = data.get("session_id", self._session_id)
                self._api_call_count = data.get("api_call_count", 0)
                self._trigger_count = data.get("trigger_count", 0)
                self._task_count = data.get("task_count", 0)
                self._crash_count = data.get("crash_count", 0)

                # 恢复未完成的任务
                for task_data in data.get("pending_tasks", []):
                    task = SubprocessTask.from_dict(task_data)
                    if task.state == "running":
                        # 检查进程是否还在运行
                        if task.pid > 0 and self._is_process_alive(task.pid):
                            self._tasks[task.id] = task
                        else:
                            # 进程已不在，标记为 crashed
                            task.state = "crashed"
                            task.error = "进程在守护重启后未找到（可能已崩溃）"
                            task.finished_at = time.time()
                            self._tasks[task.id] = task
                            self.memory.add(
                                f"子进程任务 '{task.prompt[:100]}' 在重启后发现已崩溃",
                                source="recovery",
                                tags=["crash", "recovery"],
                            )
                    else:
                        self._tasks[task.id] = task

            logger.info(
                "Daemon state loaded: session=%s, pending_tasks=%d",
                self._session_id, len(self._tasks),
            )
        except Exception as e:
            logger.warning("Failed to load daemon state: %s", e)

    def _save_state(self) -> None:
        """持久化当前状态。"""
        with self._lock:
            data = {
                "session_id": self._session_id,
                "state": self._state.value,
                "api_call_count": self._api_call_count,
                "trigger_count": self._trigger_count,
                "task_count": self._task_count,
                "started_at": self._started_at,
                "crash_count": self._crash_count,
                "saved_at": time.time(),
                "pending_tasks": [
                    t.to_dict() for t in self._tasks.values()
                    if t.state in ("pending", "running")
                ],
            }
        try:
            with FileLock(self._state_path()):
                with open(self._state_path(), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save daemon state: %s", e)

    # ── Lifecycle ────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动守护模式。

        启动后进入 Agent Loop，持续运行直到 stop() 被调用。
        """
        if self._state == DaemonState.RUNNING:
            logger.warning("DaemonCore already running")
            return

        if self._crash_count >= self._max_crash_restarts:
            logger.error(
                "Max crash restarts reached (%d/%d), refusing to start",
                self._crash_count, self._max_crash_restarts,
            )
            self._set_state(DaemonState.ERROR)
            return

        self._set_state(DaemonState.STARTING)
        self._stop_event.clear()
        self._started_at = time.time()
        self._needs_initial_trigger = True

        # 清理孤儿进程
        await self._cleanup_orphans()

        # 重置 crash 计数（成功启动后）
        self._crash_count = 0
        self._save_state()

        # 记忆系统记录启动
        self.memory.add(
            f"守护模式启动 (session={self._session_id})",
            source="system",
            tags=["lifecycle", "startup"],
        )

        # 启动 Agent Loop
        self._loop_task = asyncio.create_task(self._agent_loop())
        self._set_state(DaemonState.RUNNING)

        logger.info("DaemonCore started (session=%s)", self._session_id)

    async def stop(self) -> None:
        """停止守护模式。"""
        if self._state in (DaemonState.STOPPED, DaemonState.STOPPING):
            return

        self._set_state(DaemonState.STOPPING)
        logger.info("DaemonCore stopping...")

        # 信号停止事件
        self._stop_event.set()

        # 等待循环退出
        if self._loop_task and not self._loop_task.done():
            try:
                await asyncio.wait_for(self._loop_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except asyncio.CancelledError:
                    pass

        # 保存最终状态
        self._save_state()
        self.memory.save()
        self.triggers.save()

        self._set_state(DaemonState.STOPPED)
        self.memory.add(
            "守护模式已停止",
            source="system",
            tags=["lifecycle", "shutdown"],
        )

        logger.info("DaemonCore stopped")

    async def _cleanup_orphans(self) -> None:
        """清理上次遗留的孤儿子进程。"""
        for task in list(self._tasks.values()):
            if task.state == "running":
                # 检查进程是否还存在
                if not self._is_process_alive(task.pid):
                    task.state = "crashed"
                    task.error = "Orphan process cleaned up on startup"
                    task.finished_at = time.time()
                    logger.warning(
                        "Cleaned up orphan task %s (pid=%d)", task.id, task.pid,
                    )
        self._save_state()

    def _set_state(self, new_state: DaemonState) -> None:
        """设置状态并通知回调。"""
        old_state = self._state
        with self._lock:
            self._state = new_state
        logger.debug("Daemon state: %s -> %s", old_state.value, new_state.value)

        for callback in self._on_state_change:
            try:
                callback(old_state, new_state)
            except Exception as e:
                logger.error("State change callback failed: %s", e)

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == DaemonState.RUNNING

    @property
    def uptime(self) -> float:
        """返回运行时长（秒）。"""
        if self._started_at <= 0:
            return 0.0
        return time.time() - self._started_at

    @property
    def api_call_count(self) -> int:
        return self._api_call_count

    @property
    def trigger_count(self) -> int:
        return self._trigger_count

    @property
    def task_count(self) -> int:
        return self._task_count

    # ── Agent Loop ───────────────────────────────────────────────────────────────

    async def _agent_loop(self) -> None:
        """主 Agent 循环。

        循环执行:
        1. 检查触发器（时间/闹钟/事件）
        2. 检查子任务状态（report/crash/超时）
        3. 处理触发结果
        4. 周期性状态保存
        5. 睡眠等待下一轮
        """
        logger.info("Agent loop started")

        while not self._stop_event.is_set():
            try:
                # 1. 初始触发（启动后立即触发一次）
                if self._needs_initial_trigger:
                    self._needs_initial_trigger = False
                    await self._handle_startup_trigger()

                # 2. 检查并处理记忆压缩需求
                await self.memory.compact_if_needed()

                # 3. 检查触发器
                fired = await self.triggers.check_and_fire()

                # 4. 检查子任务
                await self._check_subprocess_tasks()

                # 6. 保存状态（每5轮保存一次，减少IO）
                if self._trigger_count % 5 == 1:
                    self._save_state()

                # 7. 等待下一轮循环（可被stop提前中断）
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._loop_interval,
                    )
                except asyncio.TimeoutError:
                    pass  # 正常超时，继续循环

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Agent loop crashed: %s", e, exc_info=True)
                self._crash_count += 1
                self._save_state()

                if self._crash_count < self._max_crash_restarts:
                    logger.info(
                        "Restarting daemon (attempt %d/%d)",
                        self._crash_count, self._max_crash_restarts,
                    )
                    await asyncio.sleep(5)
                    await self.start()  # 自动重启
                else:
                    self._set_state(DaemonState.ERROR)
                    self.memory.add(
                        f"守护进程崩溃，已达最大重启次数 ({self._crash_count}/{self._max_crash_restarts})",
                        source="error",
                        tags=["crash", "fatal"],
                    )
                    logger.error("Max restarts reached, daemon stopped")
                    break

        logger.info("Agent loop ended")

    async def _handle_startup_trigger(self) -> None:
        """处理启动触发 — 守护模式启动后的首次检查。"""
        self._trigger_count += 1
        trigger_info = {
            "trigger_id": "startup",
            "trigger_name": "守护启动",
            "trigger_type": "startup",
            "fired_at": time.time(),
        }

        self.memory.add(
            "守护模式首次触发检查",
            source="trigger",
            tags=["startup", "trigger"],
        )

        await self._process_trigger(trigger_info)

    async def _on_trigger_fired(self, trigger: Any, context: Dict[str, Any]) -> None:
        """触发器回调 — 由 TriggerSystem 调用。"""
        self._trigger_count += 1
        trigger_info = {
            "trigger_id": getattr(trigger, "id", ""),
            "trigger_name": getattr(trigger, "name", ""),
            "trigger_type": context.get("trigger_type", "unknown"),
            "fired_at": context.get("fired_at", time.time()),
        }

        trigger_desc = trigger_info["trigger_name"]
        self.memory.add(
            f"触发器触发: {trigger_desc}",
            source="trigger",
            tags=["trigger", trigger_info["trigger_type"]],
        )

        await self._process_trigger(trigger_info)

    async def _process_trigger(self, trigger_info: Dict[str, Any]) -> None:
        """处理触发事件 — Agent Loop 的核心决策逻辑。

        这是守护模式真正"思考"的地方：
        1. 回顾近期记忆
        2. 判断当前情况
        3. 决定是否需要执行任务
        4. 如果任务复杂，创建子进程处理
        5. 如果任务简单，直接处理
        """
        # 构建触发上下文字符串
        context_parts = [
            f"触发类型: {trigger_info['trigger_type']}",
            f"触发名称: {trigger_info['trigger_name']}",
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"运行时长: {self.uptime:.0f}秒",
            f"已完成任务数: {self._task_count}",
        ]

        # 添加运行中和待处理的任务信息
        active_tasks = [
            t for t in self._tasks.values()
            if t.state in ("pending", "running")
        ]
        if active_tasks:
            context_parts.append(f"活跃任务: {len(active_tasks)}")
            for t in active_tasks[:5]:
                context_parts.append(f"  - [{t.state}] {t.prompt[:100]} (已运行{t.elapsed:.0f}秒)")

        context_str = "\n".join(context_parts)

        # 添加到记忆
        self.memory.add(context_str, source="agent_loop", tags=["context", "check"])

        # 获取记忆提示词
        memory_prompt = self.memory.get_memory_prompt()

        # TODO: 如果记忆提示词足够丰富，调用 LLM 判断是否需要执行任务
        # 当前阶段：基于规则进行简单判断

        # 检查是否有挂起的任务（崩溃恢复的场景）
        pending = [t for t in self._tasks.values() if t.state == "pending"]
        for task in pending:
            await self._launch_subprocess_task(task)

    # ── Subprocess Management ────────────────────────────────────────────────────

    async def _launch_subprocess_task(self, task: SubprocessTask) -> None:
        """启动子进程执行复杂任务。

        子进程以项目模式（无交互）运行，传入任务指令。
        守护模式等待子进程完成（通过 report/crash/超时事件），
        不轮询子进程状态。
        """


        async with self._task_lock:
            # 检查并发限制
            running = [t for t in self._tasks.values() if t.state == "running"]
            if len(running) >= MAX_CONCURRENT_TASKS:
                logger.info(
                    "Task %s queued: %d running (max=%d)",
                    task.id, len(running), MAX_CONCURRENT_TASKS,
                )
                return  # 保持 pending 状态，下次循环重试

            task.state = "running"
            task.started_at = time.time()
            self._task_count += 1

        logger.info("Launching subprocess task: %s", task.id)

        try:
            # 构建子进程命令
            # 使用 autorun 命令，通过管道传入任务
            cmd = [sys.executable, "-c", f"""
import sys, os
sys.path.insert(0, r'{os.path.dirname(os.path.dirname(__file__))}')

# 设置环境
os.environ.setdefault("AUTORUN_DEV", "1")  # 跳过首次配置检查

# 传入任务描述
task_prompt = '''{task.prompt}'''

# TODO: 阶段2实现子进程的完整任务执行
# 子进程将运行一个简化的项目模式会话：
# 1. 读取任务
# 2. 调用 LLM 完成任务
# 3. 通过 stdout 输出结果
# 4. 退出

print("DAEMON_TASK_RESULT: 子进程执行框架已就绪（待阶段2实现完整 Agent 集成）")
sys.exit(0)
"""]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            task.pid = proc.pid
            self._save_state()

            # 等待子进程完成（带超时）
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=task.timeout,
                )
                task.finished_at = time.time()

                stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

                if proc.returncode == 0:
                    task.state = "completed"
                    task.result = stdout
                    logger.info("Task %s completed (rc=%d)", task.id, proc.returncode)
                else:
                    task.state = "crashed"
                    task.error = f"Exit code: {proc.returncode}\nStderr: {stderr[:1000]}"
                    logger.warning("Task %s crashed (rc=%d)", task.id, proc.returncode)

            except asyncio.TimeoutError:
                task.finished_at = time.time()
                task.state = "timeout"
                task.error = f"超时 ({task.timeout}秒)"
                logger.warning("Task %s timed out after %ds", task.id, task.timeout)

                # 杀死超时进程
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

            # 记录到记忆
            self.memory.add(
                f"任务 '{task.prompt[:100]}' {task.state}: {task.result[:300] if task.result else task.error[:300]}",
                source="task",
                tags=["task", task.state],
            )

            # 通知回调
            for callback in self._on_task_complete:
                try:
                    callback(task)
                except Exception as e:
                    logger.error("Task complete callback failed: %s", e)

        except Exception as e:
            task.state = "crashed"
            task.error = str(e)
            task.finished_at = time.time()
            logger.error("Task %s launch failed: %s", task.id, e)

        finally:
            self._save_state()

    async def _check_subprocess_tasks(self) -> None:
        """检查子进程任务状态（超时检测和清理）。"""
        async with self._task_lock:
            for task in list(self._tasks.values()):
                if task.state == "running" and task.is_timed_out:
                    task.state = "timeout"
                    task.finished_at = time.time()
                    task.error = f"超时检测: 运行 {task.elapsed:.0f}秒 > {task.timeout}秒限制"
                    logger.warning("Task %s detected as timeout", task.id)

                    self.memory.add(
                        f"任务超时: '{task.prompt[:100]}' (运行{task.elapsed:.0f}秒)",
                        source="timeout",
                        tags=["timeout", "task"],
                    )

                    # 尝试杀死进程
                    if task.pid > 0:
                        try:
                            os.kill(task.pid, signal.SIGTERM)
                        except Exception:
                            pass

            # 清理旧任务（已完成超过1小时的）
            cutoff = time.time() - 3600
            to_remove = [
                tid for tid, t in self._tasks.items()
                if t.state in ("completed", "crashed", "timeout")
                and t.finished_at > 0
                and t.finished_at < cutoff
            ]
            for tid in to_remove:
                del self._tasks[tid]

    async def submit_task(self, prompt: str, timeout: int = DEFAULT_TASK_TIMEOUT,
                          metadata: Optional[Dict[str, Any]] = None) -> SubprocessTask:
        """提交一个任务给守护模式执行。

        Args:
            prompt: 任务描述/指令。
            timeout: 超时时间（秒）。
            metadata: 附加元数据。

        Returns:
            SubprocessTask 对象。
        """
        task = SubprocessTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            prompt=prompt,
            timeout=timeout,
            metadata=metadata or {},
        )
        async with self._task_lock:
            self._tasks[task.id] = task

        # 标记需要立即检查
        self._needs_initial_trigger = True

        self._save_state()
        logger.info("Task submitted: %s", task.id)
        return task

    def get_task(self, task_id: str) -> Optional[SubprocessTask]:
        """获取指定任务。"""
        return self._tasks.get(task_id)

    def get_all_tasks(self) -> List[SubprocessTask]:
        """获取所有任务。"""
        return list(self._tasks.values())

    def cancel_task(self, task_id: str) -> bool:
        """取消指定任务。"""
        task = self._tasks.get(task_id)
        if task and task.state in ("pending", "running"):
            task.state = "crashed"
            task.error = "用户取消"
            task.finished_at = time.time()
            if task.pid > 0:
                try:
                    os.kill(task.pid, signal.SIGTERM)
                except Exception:
                    pass
            self._save_state()
            return True
        return False

    # ── Callbacks ────────────────────────────────────────────────────────────────

    def on_state_changed(self, callback: Callable) -> None:
        """注册状态变更回调。"""
        self._on_state_change.append(callback)

    def on_task_completed(self, callback: Callable) -> None:
        """注册任务完成回调。"""
        self._on_task_complete.append(callback)

    # ── Process Utility ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """检查进程是否存活。"""
        if pid <= 0:
            return False
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    # ── Status / Info ────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """获取守护模式完整状态（用于 WebUI 和悬浮球）。"""
        with self._lock:
            return {
                "state": self._state.value,
                "session_id": self._session_id,
                "uptime": self.uptime,
                "started_at": self._started_at,
                "api_call_count": self._api_call_count,
                "trigger_count": self._trigger_count,
                "task_count": self._task_count,
                "active_tasks": len([
                    t for t in self._tasks.values()
                    if t.state in ("pending", "running")
                ]),
                "total_tasks": len(self._tasks),
                "memory_stats": self.memory.get_stats(),
                "trigger_count_total": len(self.triggers.get_all_triggers()),
                "loop_interval": self._loop_interval,
                "sleeping": self.triggers.is_sleeping,
            }

    # ── Sleep / Wake ─────────────────────────────────────────────────────────────

    async def sleep(self, duration_seconds: float = 0) -> None:
        """进入休眠模式。"""
        self.triggers.sleep(duration_seconds)
        self._set_state(DaemonState.SLEEPING)
        self.memory.add(
            f"进入休眠模式 ({duration_seconds}秒)" if duration_seconds > 0
            else "进入无限期休眠",
            source="system",
            tags=["lifecycle", "sleep"],
        )

    async def wake(self) -> None:
        """唤醒。"""
        self.triggers.wake()
        self._set_state(DaemonState.RUNNING)
        self.memory.add(
            "从休眠模式唤醒",
            source="system",
            tags=["lifecycle", "wake"],
        )
        # 立即触发一次检查
        self._needs_initial_trigger = True

    # ── Auto-start ─────────────────────────────────────────────────────────────────

    def enable_autostart(self) -> bool:
        """启用开机自启。"""
        import sys
        autorun_path = sys.executable
        daemon_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "run_daemon.py",
        )

        if sys.platform == "win32":
            # Windows: 注册表 Run 键
            import winreg
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE,
                )
                winreg.SetValueEx(
                    key, "AutoRUN_Daemon", 0, winreg.REG_SZ,
                    f'"{autorun_path}" "{daemon_script}"',
                )
                winreg.CloseKey(key)
                logger.info("Auto-start enabled (Windows Registry)")
                return True
            except Exception as e:
                logger.error("Failed to enable autostart: %s", e)
                return False

        elif sys.platform == "darwin":
            # macOS: LaunchAgent
            plist_dir = os.path.expanduser("~/Library/LaunchAgents")
            os.makedirs(plist_dir, exist_ok=True)
            plist_path = os.path.join(plist_dir, "com.autorun.daemon.plist")
            plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.autorun.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{autorun_path}</string>
        <string>{daemon_script}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>'''
            try:
                with open(plist_path, "w") as f:
                    f.write(plist_content)
                logger.info("Auto-start enabled (macOS LaunchAgent)")
                return True
            except Exception as e:
                logger.error("Failed to enable autostart: %s", e)
                return False

        else:
            # Linux: autostart .desktop
            autostart_dir = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart_dir, exist_ok=True)
            desktop_path = os.path.join(
                autostart_dir, "autorun-daemon.desktop",
            )
            desktop_content = f"""[Desktop Entry]
Type=Application
Name=AutoRUN Daemon
Exec={autorun_path} {daemon_script}
X-GNOME-Autostart-enabled=true
"""
            try:
                with open(desktop_path, "w") as f:
                    f.write(desktop_content)
                os.chmod(desktop_path, 0o755)
                logger.info("Auto-start enabled (Linux autostart)")
                return True
            except Exception as e:
                logger.error("Failed to enable autostart: %s", e)
                return False

    def disable_autostart(self) -> bool:
        """禁用开机自启。"""
        import sys

        if sys.platform == "win32":
            import winreg
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE,
                )
                winreg.DeleteValue(key, "AutoRUN_Daemon")
                winreg.CloseKey(key)
                return True
            except FileNotFoundError:
                return True  # Already not set
            except Exception as e:
                logger.error("Failed to disable autostart: %s", e)
                return False

        elif sys.platform == "darwin":
            plist_path = os.path.expanduser(
                "~/Library/LaunchAgents/com.autorun.daemon.plist",
            )
            try:
                os.unlink(plist_path)
                return True
            except FileNotFoundError:
                return True
            except Exception as e:
                logger.error("Failed to disable autostart: %s", e)
                return False

        else:
            desktop_path = os.path.expanduser(
                "~/.config/autostart/autorun-daemon.desktop",
            )
            try:
                os.unlink(desktop_path)
                return True
            except FileNotFoundError:
                return True
            except Exception as e:
                logger.error("Failed to disable autostart: %s", e)
                return False


# ── Singleton (optional) ────────────────────────────────────────────────────────

_daemon_core: Optional[DaemonCore] = None
_daemon_lock = threading.Lock()


def get_daemon_core() -> DaemonCore:
    """获取全局 DaemonCore 单例（用于悬浮球/WebUI 访问）。"""
    global _daemon_core
    with _daemon_lock:
        if _daemon_core is None:
            _daemon_core = DaemonCore()
        return _daemon_core


def set_daemon_core(core: DaemonCore) -> None:
    """设置全局 DaemonCore 实例。"""
    global _daemon_core
    with _daemon_lock:
        _daemon_core = core


# ── Standalone Entry Point ──────────────────────────────────────────────────────

async def run_daemon() -> None:
    """守护模式独立入口（用于测试和开发）。

    可直接运行:
        python -m AutoRUN_v1.daemon.daemon_core
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    core = DaemonCore()
    set_daemon_core(core)

    logger.info("=== AutoRUN Daemon Mode ===")
    logger.info("Session: %s", core._session_id)
    logger.info("Config dir: %s", core._config_dir)

    # 注册信号处理
    def _signal_handler(sig, frame):
        logger.info("Received signal %s, stopping...", sig)
        asyncio.create_task(core.stop())

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        await core.start()

        # 保持运行
        while core.is_running:
            await asyncio.sleep(1.0)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    finally:
        if core.is_running:
            await core.stop()
        logger.info("Daemon exited.")


if __name__ == "__main__":
    asyncio.run(run_daemon())
