"""
Daemon Mode - 多级记忆系统。

记忆分级：
- 短期记忆(short_term): 保留最新12小时内容，超过15000字触发压缩
  压缩时保留最近2000字符原始内容
- 中期记忆(mid_term): 保留最近15天，攒够10条超时内容再处理（存储额度20000字）
- 长期记忆(long_term): 永远不删除（存储额度10000字）

提示词缓存优化：
- 每12小时或重启时重建提示词
- 增量更新时先追加到末尾标记，等重建时再整理

所有记忆持久化到 ~/.autorun/daemon/memory.json，崩溃后可恢复。
"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from AutoRUN_v1.utils.env_utils import get_autorun_config_dir
from AutoRUN_v1.utils.file_lock import FileLock

logger = logging.getLogger(__name__)


# ── Configuration Constants ─────────────────────────────────────────────────────

# 短期记忆
SHORT_TERM_MAX_AGE_HOURS = 12          # 保留最新12小时
SHORT_TERM_MAX_CHARS = 15000           # 超过15000字符触发压缩
SHORT_TERM_KEEP_RECENT_CHARS = 2000    # 压缩时保留最近2000字符

# 中期记忆
MID_TERM_MAX_AGE_DAYS = 15             # 保留最近15天
MID_TERM_BATCH_SIZE = 10               # 攒够10条再处理
MID_TERM_MAX_CHARS = 20000             # 存储额度20000字

# 长期记忆
LONG_TERM_MAX_CHARS = 10000            # 存储额度10000字

# 提示词缓存
PROMPT_REBUILD_INTERVAL_HOURS = 12     # 每12小时重建提示词


@dataclass
class MemoryEntry:
    """单条记忆条目。"""
    id: str
    content: str
    timestamp: float  # epoch seconds
    source: str = ""  # 来源：user/system/agent/trigger
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def age_hours(self) -> float:
        """返回年龄（小时）。"""
        return (time.time() - self.timestamp) / 3600.0

    def age_days(self) -> float:
        """返回年龄（天）。"""
        return (time.time() - self.timestamp) / 86400.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "timestamp": self.timestamp,
            "source": self.source,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryEntry":
        return cls(
            id=d.get("id", ""),
            content=d.get("content", ""),
            timestamp=d.get("timestamp", time.time()),
            source=d.get("source", ""),
            tags=d.get("tags", []),
            metadata=d.get("metadata", {}),
        )


class MemorySystem:
    """多级记忆系统。

    线程安全。自动管理短期/中期/长期记忆的生命周期。
    """

    def __init__(self, save_dir: Optional[str] = None):
        self._lock = threading.RLock()
        self._save_dir = save_dir or os.path.join(
            get_autorun_config_dir(), "daemon"
        )
        os.makedirs(self._save_dir, exist_ok=True)

        # 三级记忆存储
        self._short_term: List[MemoryEntry] = []
        self._mid_term: List[MemoryEntry] = []
        self._long_term: List[MemoryEntry] = []

        # 提示词缓存
        self._cached_prompt: Optional[str] = None
        self._prompt_built_at: float = 0.0
        self._pending_append: List[str] = []  # 增量追加的待整理内容

        # 持久化路径
        self._save_path = os.path.join(self._save_dir, "memory.json")

        # 压缩标记（用于非异步上下文的延迟压缩）
        self._needs_compact: bool = False

        # 加载已有记忆
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载记忆。"""
        if not os.path.exists(self._save_path):
            return
        try:
            with open(self._save_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self._short_term = [
                    MemoryEntry.from_dict(e) for e in data.get("short_term", [])
                ]
                self._mid_term = [
                    MemoryEntry.from_dict(e) for e in data.get("mid_term", [])
                ]
                self._long_term = [
                    MemoryEntry.from_dict(e) for e in data.get("long_term", [])
                ]
                self._pending_append = data.get("pending_append", [])
            logger.info(
                "Memory loaded: short=%d, mid=%d, long=%d",
                len(self._short_term), len(self._mid_term), len(self._long_term),
            )
        except Exception as e:
            logger.warning("Failed to load memory: %s", e)

    def save(self) -> None:
        """持久化记忆到磁盘。"""
        with self._lock:
            data = {
                "short_term": [e.to_dict() for e in self._short_term],
                "mid_term": [e.to_dict() for e in self._mid_term],
                "long_term": [e.to_dict() for e in self._long_term],
                "pending_append": self._pending_append,
                "saved_at": time.time(),
            }
        try:
            save_path = self._save_path
            with FileLock(save_path):
                with open(save_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save memory: %s", e)

    # ── Core Operations ──────────────────────────────────────────────────────────

    def add(self, content: str, source: str = "system",
            tags: Optional[List[str]] = None,
            metadata: Optional[Dict[str, Any]] = None) -> str:
        """添加一条记忆。

        Returns:
            记忆条目的 ID。
        """
        import uuid
        entry = MemoryEntry(
            id=uuid.uuid4().hex[:12],
            content=content,
            timestamp=time.time(),
            source=source,
            tags=tags or [],
            metadata=metadata or {},
        )
        with self._lock:
            self._short_term.append(entry)
            # 增量追加到待整理列表
            tag_str = f"[{source}]" if source else ""
            self._pending_append.append(f"{tag_str} {content[:500]}")

        # 保存
        self.save()

        # 检查是否需要压缩
        self._check_compact()

        return entry.id

    def _check_compact(self) -> None:
        """检查短期记忆是否需要压缩并执行。

        如果在异步上下文中（有运行中的事件循环），异步执行压缩。
        否则标记需要压缩，由调用方通过 compact_if_needed() 主动触发。
        """
        with self._lock:
            total_chars = sum(len(e.content) for e in self._short_term)

            if total_chars > SHORT_TERM_MAX_CHARS:
                # 尝试异步执行
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.ensure_future(self._compact_short_term(), loop=loop)
                except RuntimeError:
                    # 没有运行中的事件循环 — 标记需要压缩
                    self._needs_compact = True

    async def compact_if_needed(self) -> None:
        """检查并进行记忆压缩（供异步上下文调用）。

        在守护模式 Agent Loop 中周期性调用，确保非异步上下文中标记的
        压缩需求能在异步循环中得到处理。
        """
        if self._needs_compact:
            self._needs_compact = False
            await self._compact_short_term()

    async def _compact_short_term(self) -> None:
        """压缩短期记忆。

        保留最近2000字符原始内容，其余压缩后转移到中期记忆。
        """
        with self._lock:
            if len(self._short_term) <= 1:
                return

            total_chars = sum(len(e.content) for e in self._short_term)
            if total_chars <= SHORT_TERM_MAX_CHARS:
                return

        # 调用压缩（使用本地摘要，避免额外API调用）
        try:
            summary = await self._summarize_memories(
                self._short_term,
                keep_recent_chars=SHORT_TERM_KEEP_RECENT_CHARS,
            )

            with self._lock:
                if summary:
                    # 生成压缩条目，转移到中期记忆
                    compact_entry = MemoryEntry(
                        id=f"compact_{int(time.time())}",
                        content=summary,
                        timestamp=time.time(),
                        source="compact",
                        tags=["compressed"],
                        metadata={
                            "original_count": len(self._short_term),
                            "compressed_at": time.time(),
                        },
                    )
                    self._mid_term.append(compact_entry)

                    # 保留最近N字符的原始内容
                    recent_chars = 0
                    keep_from = len(self._short_term)
                    for i in range(len(self._short_term) - 1, -1, -1):
                        recent_chars += len(self._short_term[i].content)
                        keep_from = i
                        if recent_chars >= SHORT_TERM_KEEP_RECENT_CHARS:
                            break

                    self._short_term = self._short_term[keep_from:]

                logger.info(
                    "Short-term memory compacted: kept %d entries",
                    len(self._short_term),
                )

            # 标记提示词需要重建
            self._cached_prompt = None

            # 检查中期记忆
            await self._check_mid_term()

            # 保存
            self.save()

        except Exception as e:
            logger.warning("Memory compaction failed: %s", e)

    async def _summarize_memories(
        self, entries: List[MemoryEntry], keep_recent_chars: int = 0
    ) -> str:
        """对记忆条目进行摘要。

        如果记忆条目较少（<= 5条），使用规则摘要。
        否则尝试调用 LLM 生成摘要（fallback 到规则摘要）。
        """
        if not entries:
            return ""

        # 确定哪些要压缩
        recent_chars = 0
        cutoff = len(entries)
        for i in range(len(entries) - 1, -1, -1):
            recent_chars += len(entries[i].content)
            cutoff = i
            if recent_chars >= keep_recent_chars:
                break

        to_compress = entries[:cutoff]
        if not to_compress:
            return ""

        # 规则摘要
        return self._local_memory_summary(to_compress)

    def _local_memory_summary(self, entries: List[MemoryEntry]) -> str:
        """本地规则摘要（无需 API 调用）。"""
        parts = []
        sources: Dict[str, int] = {}
        total_chars = 0

        for e in entries:
            sources[e.source] = sources.get(e.source, 0) + 1
            total_chars += len(e.content)

        parts.append(
            f"[记忆压缩] {len(entries)} 条，共约 {total_chars} 字符，"
            f"时间段: {datetime.fromtimestamp(entries[0].timestamp).strftime('%H:%M')}"
            f" - {datetime.fromtimestamp(entries[-1].timestamp).strftime('%H:%M')}"
        )

        if sources:
            src_str = ", ".join(f"{k}:{v}" for k, v in sources.items())
            parts.append(f"来源分布: {src_str}")

        # 提取关键内容片段（每条取前150字符）
        highlights = []
        for e in entries:
            snippet = e.content[:150].replace("\n", " ").strip()
            if snippet:
                highlights.append(f"- [{e.source}] {snippet}")
            if len(highlights) >= 15:
                highlights.append("- ... (更多内容已截断)")
                break

        if highlights:
            parts.append("内容摘要:")
            parts.extend(highlights)

        return "\n".join(parts)

    async def _check_mid_term(self) -> None:
        """检查中期记忆是否需要处理。"""
        with self._lock:
            # 移除超过15天的条目
            cutoff = time.time() - MID_TERM_MAX_AGE_DAYS * 86400
            expired = [e for e in self._mid_term if e.timestamp < cutoff]
            self._mid_term = [e for e in self._mid_term if e.timestamp >= cutoff]

            # 攒够10条则压缩
            if len(self._mid_term) >= MID_TERM_BATCH_SIZE:
                # 检查总字符数
                total = sum(len(e.content) for e in self._mid_term)
                if total > MID_TERM_MAX_CHARS:
                    await self._compact_mid_term()

    async def _compact_mid_term(self) -> None:
        """压缩中期记忆 — 提取关键信息转移到长期记忆。"""
        with self._lock:
            if len(self._mid_term) <= 1:
                return

            # 摘要中期记忆
            summary = self._local_memory_summary(self._mid_term)

            # 转移到长期记忆
            long_entry = MemoryEntry(
                id=f"long_{int(time.time())}",
                content=summary,
                timestamp=time.time(),
                source="mid_term_compact",
                tags=["long_term", "compressed"],
                metadata={
                    "original_count": len(self._mid_term),
                    "compressed_at": time.time(),
                },
            )
            self._long_term.append(long_entry)

            # 检查长期记忆额度
            self._trim_long_term()

            # 清空中期记忆（已转移）
            self._mid_term = []

            logger.info("Mid-term memory compacted to long-term")

            self._cached_prompt = None

    def _trim_long_term(self) -> None:
        """修剪长期记忆，保持在额度内。"""
        total = sum(len(e.content) for e in self._long_term)
        while total > LONG_TERM_MAX_CHARS and len(self._long_term) > 1:
            # 移除最旧的
            removed = self._long_term.pop(0)
            total -= len(removed.content)

    # ── Prompt Building ──────────────────────────────────────────────────────────

    def get_memory_prompt(self) -> str:
        """获取记忆提示词（用于注入到 Agent 的系统提示词中）。

        遵循提示词缓存优化：
        - 如果未超过12小时且有缓存，返回缓存版本
        - 否则重建提示词
        """
        now = time.time()
        with self._lock:
            if (self._cached_prompt is not None and
                    now - self._prompt_built_at < PROMPT_REBUILD_INTERVAL_HOURS * 3600
                    and not self._pending_append):
                return self._cached_prompt

        # 重建提示词
        prompt = self._build_memory_prompt()
        with self._lock:
            self._cached_prompt = prompt
            self._prompt_built_at = now
            self._pending_append = []  # 清除待整理增量

        return prompt

    def _build_memory_prompt(self) -> str:
        """构建完整的记忆提示词。"""
        with self._lock:
            parts = [
                "# 守护模式记忆系统",
                "",
                "以下是守护模式的多级记忆内容。守护模式持续监控环境并在触发时执行任务。",
                "",
            ]

            # ── 长期记忆（优先展示） ──
            if self._long_term:
                parts.append("## 长期记忆（核心知识，永久保留）")
                parts.append("")
                for i, entry in enumerate(self._long_term):
                    ts = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M")
                    parts.append(f"### 长期记忆 #{i+1} ({ts})")
                    parts.append(entry.content[:1500])  # 每条截断
                    parts.append("")
                parts.append("")

            # ── 中期记忆 ──
            if self._mid_term:
                parts.append("## 中期记忆（近期重要事件）")
                parts.append("")
                for entry in self._mid_term:
                    ts = datetime.fromtimestamp(entry.timestamp).strftime("%m-%d %H:%M")
                    parts.append(f"- [{ts}] [{entry.source}] {entry.content[:300]}")
                parts.append("")

            # ── 短期记忆 ──
            if self._short_term:
                parts.append("## 短期记忆（最近交互）")
                parts.append("")
                for entry in self._short_term[-20:]:  # 最近20条
                    ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M")
                    parts.append(f"- [{ts}] [{entry.source}] {entry.content[:200]}")
                parts.append("")

            # ── 待整理增量 ──
            if self._pending_append:
                parts.append("## 增量更新（待整理）")
                parts.append("")
                for item in self._pending_append[-10:]:
                    parts.append(f"- {item[:200]}")

            if len(parts) <= 2:
                return ""  # 没有记忆内容

            return "\n".join(parts)

    # ── Query / Management ───────────────────────────────────────────────────────

    def get_all_entries(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有记忆条目（用于 WebUI 显示）。"""
        with self._lock:
            return {
                "short_term": [e.to_dict() for e in self._short_term],
                "mid_term": [e.to_dict() for e in self._mid_term],
                "long_term": [e.to_dict() for e in self._long_term],
            }

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计信息。"""
        with self._lock:
            st_chars = sum(len(e.content) for e in self._short_term)
            mt_chars = sum(len(e.content) for e in self._mid_term)
            lt_chars = sum(len(e.content) for e in self._long_term)
            return {
                "short_term_count": len(self._short_term),
                "short_term_chars": st_chars,
                "short_term_max_chars": SHORT_TERM_MAX_CHARS,
                "mid_term_count": len(self._mid_term),
                "mid_term_chars": mt_chars,
                "mid_term_max_chars": MID_TERM_MAX_CHARS,
                "long_term_count": len(self._long_term),
                "long_term_chars": lt_chars,
                "long_term_max_chars": LONG_TERM_MAX_CHARS,
                "prompt_cached": self._cached_prompt is not None,
                "prompt_age_hours": (time.time() - self._prompt_built_at) / 3600.0
                if self._prompt_built_at > 0 else 0,
                "pending_append_count": len(self._pending_append),
                "needs_compact": self._needs_compact,
            }

    def clear_short_term(self) -> None:
        """清除短期记忆。"""
        with self._lock:
            self._short_term = []
            self._cached_prompt = None
        self.save()

    def clear_mid_term(self) -> None:
        """清除中期记忆。"""
        with self._lock:
            self._mid_term = []
            self._cached_prompt = None
        self.save()

    def clear_long_term(self) -> None:
        """清除长期记忆。"""
        with self._lock:
            self._long_term = []
            self._cached_prompt = None
        self.save()

    def clear_all(self) -> None:
        """清除所有记忆。"""
        with self._lock:
            self._short_term = []
            self._mid_term = []
            self._long_term = []
            self._cached_prompt = None
            self._pending_append = []
        self.save()

    def delete_entry(self, level: str, entry_id: str) -> bool:
        """删除指定记忆条目。"""
        with self._lock:
            if level == "short_term":
                before = len(self._short_term)
                self._short_term = [e for e in self._short_term if e.id != entry_id]
                if len(self._short_term) < before:
                    self._cached_prompt = None
                    self.save()
                    return True
            elif level == "mid_term":
                before = len(self._mid_term)
                self._mid_term = [e for e in self._mid_term if e.id != entry_id]
                if len(self._mid_term) < before:
                    self._cached_prompt = None
                    self.save()
                    return True
            elif level == "long_term":
                before = len(self._long_term)
                self._long_term = [e for e in self._long_term if e.id != entry_id]
                if len(self._long_term) < before:
                    self._cached_prompt = None
                    self.save()
                    return True
        return False
