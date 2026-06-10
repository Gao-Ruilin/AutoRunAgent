"""
后台文件索引服务。

在项目根目录 .autorun/index/ 下维护文件清单和 LLM 生成的摘要。
使用低模型后台异步构建和更新，不阻塞主对话流程。

检测机制:
- 定时 60s MD5 轮询（纯程序逻辑，不调用 AI）
- Agent 编辑/写入文件后主动通知
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

SCRIPT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
    ".bash", ".zsh", ".ps1", ".sql", ".r", ".m", ".lua", ".ex", ".exs",
    ".elm", ".clj", ".cljs", ".erl", ".hrl", ".hs", ".ml", ".mli", ".fs",
    ".fsx", ".dart", ".jl", ".nim", ".zig", ".v", ".sv", ".sc", ".sbt",
}

ASSET_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".bmp", ".webp",
    ".ttf", ".woff", ".woff2", ".eot", ".otf",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".webm", ".avi",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
}

DOC_EXTENSIONS = {
    ".md", ".rst", ".txt", ".cfg", ".ini", ".toml", ".yaml", ".yml",
    ".json", ".xml", ".csv", ".htm", ".html", ".css",
}

EXCLUDED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".autorun",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "dist", "build", ".wrangler", ".claude", ".next", ".nuxt",
}

EXCLUDED_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dll", ".pyd", ".exe", ".bin", ".lock",
    ".class", ".o", ".a", ".lib", ".dylib",
}

MAX_INITIAL_FILES = 500          # 初始构建文件上限
BATCH_SIZE = 15                  # 每批发送给低模型的文件数
POLL_INTERVAL_SEC = 60           # MD5 轮询间隔
MAX_FILE_SIZE_FOR_SUMMARY = 200 * 1024  # 摘要的最大文件大小 (200KB)
READ_HEAD_LINES = 150            # 生成摘要时读取文件头部行数

MAX_CODE_LINES = 10000           # 超过此行数的代码文件跳过索引
DATA_EXTENSIONS = {".json", ".csv", ".xml", ".yaml", ".yml"}  # 数据文件扩展名
DATA_SAMPLE_THRESHOLD = 1000     # 超过此行数的数据文件触发随机采样
DATA_SAMPLE_SEGMENTS = 10        # 采样段数
DATA_SAMPLE_LINES = 100          # 每段行数

MAX_PRIORITY_FILES = 200          # 注入上下文时默认只取优先级最高的 N 个文件
PRIORITY_FILE_NAMES = {
    # 说明/配置文件 → 高优先级（文件名匹配）
    "README.md", "README", "README.rst", "README.txt",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements-dev.txt",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".gitignore", ".env.example", ".env.template",
    "package.json", "tsconfig.json",
    "Cargo.toml", "go.mod", "CMakeLists.txt",
}
PRIORITY_WARNING = (
    "⚠️ 文件数量超过限制({})，当前仅显示优先级最高的{}个文件。"
    "可用 /index full 查看完整索引。"
)

# ── 数据类 ───────────────────────────────────────────────────────────────────


@dataclass
class FileEntry:
    """单个文件的索引条目。"""
    rel_path: str           # 相对项目根目录的路径
    abs_path: str           # 绝对路径
    md5: str                # MD5 十六进制摘要
    file_type: str          # "dir" | "script" | "asset" | "doc" | "other"
    size: int               # 字节数
    mtime: float            # os.stat().st_mtime


@dataclass
class IndexManifest:
    """索引清单。"""
    version: int = 2
    project_root: str = ""
    last_full_scan: str = ""  # ISO 时间戳
    files: Dict[str, FileEntry] = field(default_factory=dict)  # rel_path → FileEntry


@dataclass
class FileSummary:
    """文件的结构化摘要。"""
    dependencies: str = ""    # 依赖的包/模块
    functionality: str = ""   # 主要功能
    logic: str = ""           # 核心逻辑
    notes: str = ""           # 注意事项
    relationships: str = ""   # 和其他文件的关系


# ── 工具函数 ─────────────────────────────────────────────────────────────────


def _compute_md5(file_path: str) -> str:
    """计算文件的 MD5 哈希值。"""
    h = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except (IOError, OSError):
        return ""
    return h.hexdigest()


def _classify_file(file_path: str) -> str:
    """根据扩展名分类文件。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in SCRIPT_EXTENSIONS:
        return "script"
    if ext in ASSET_EXTENSIONS:
        return "asset"
    if ext in DOC_EXTENSIONS:
        return "doc"
    return "other"


def _is_data_file(file_path: str) -> bool:
    """判断是否为数据文件（JSON/CSV/XML/YAML等）。"""
    return os.path.splitext(file_path)[1].lower() in DATA_EXTENSIONS


def _count_lines(file_path: str) -> int:
    """统计文件行数。"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _sample_data_file(file_path: str) -> str:
    """对大数据文件随机采样。>1000行时取10段不重复100行片段。"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""

    total = len(lines)
    if total <= DATA_SAMPLE_THRESHOLD:
        return "".join(lines)

    step = total // DATA_SAMPLE_SEGMENTS
    sampled_parts = [
        f"[该文件共 {total} 行，以下为随机采样的 {DATA_SAMPLE_SEGMENTS} 段 × {DATA_SAMPLE_LINES} 行]\n"
    ]
    for i in range(DATA_SAMPLE_SEGMENTS):
        max_start = (i + 1) * step - DATA_SAMPLE_LINES
        min_start = i * step
        if max_start <= min_start:
            max_start = min_start + 1
        start = random.randint(min_start, max(max_start - 1, min_start))
        sampled_parts.append(f"...(第{start + 1}行起)...\n")
        sampled_parts.append("".join(lines[start:start + DATA_SAMPLE_LINES]))
    return "".join(sampled_parts)


def _should_skip(path: str, root: str) -> bool:
    """判断是否应跳过此文件/目录。"""
    rel = os.path.relpath(path, root)
    parts = rel.replace("\\", "/").split("/")

    # 跳过隐藏文件和排除目录
    for part in parts:
        if part.startswith(".") and part not in (".autorun",):
            return True
        if part in EXCLUDED_DIRS:
            return True

    # 跳过排除的扩展名
    ext = os.path.splitext(path)[1].lower()
    if ext in EXCLUDED_EXTENSIONS:
        return True

    return False


# ── 文件索引器 ───────────────────────────────────────────────────────────────


class FileIndexer:
    """后台文件索引服务。

    生命周期:
      1. QueryEngine.initialize() 中创建
      2. 检查 .autorun/index/ 是否存在
      3. 存在 → 加载 → 启动定时轮询
      4. 不存在 → 通知 UI 层询问用户
      5. 用户确认 → 后台构建 → 启动轮询
    """

    def __init__(self, project_root: str, state: Any = None):
        self._project_root = os.path.abspath(project_root)
        self._state = state
        self._index_dir = os.path.join(self._project_root, ".autorun", "index")
        self._manifest_path = os.path.join(self._index_dir, "manifest.json")
        self._summaries_dir = os.path.join(self._index_dir, "summaries")

        # 运行时状态
        self._manifest: Optional[IndexManifest] = None
        self._summaries: Dict[str, FileSummary] = {}  # md5 → FileSummary
        self._pending_updates: Set[str] = set()  # 工具推送的变更路径
        self._lock = asyncio.Lock()
        self._poll_task: Optional[asyncio.Task] = None
        self._build_task: Optional[asyncio.Task] = None
        self._version: int = 0  # 索引版本号，摘要/清单变更时自增

        self._is_building = False
        self._is_ready = False
        self._user_wants_index: Optional[bool] = None  # None = 未询问
        self._user_declined = False

        # 进度追踪
        self._scanned_count = 0
        self._scanned_total_estimate = 0
        self._summary_done = 0
        self._summary_total = 0
        self._current_stage = ""  # "scanning" | "summarizing" | ""

        # 启用状态（是否注入提示词）
        self._enabled = True

        # 优先级裁剪标记（文件数超过 MAX_PRIORITY_FILES 时为 True）
        self._was_priority_trimmed = False

    # ── 属性 ──────────────────────────────────────────────────────────────

    @property
    def version(self) -> int:
        """索引版本号，摘要/清单变更时自增。QueryEngine 用此判断是否需要刷新提示词。"""
        return self._version

    @property
    def is_ready(self) -> bool:
        """索引是否已就绪可注入。"""
        return self._is_ready and self._manifest is not None

    @property
    def is_building(self) -> bool:
        """是否正在构建中。"""
        return self._is_building

    @property
    def file_count(self) -> int:
        """已索引的文件数。"""
        if self._manifest:
            return len(self._manifest.files)
        return 0

    @property
    def enabled(self) -> bool:
        """索引是否启用（是否注入系统提示词）。"""
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        """设置索引启用状态。"""
        self._enabled = value

    @property
    def progress(self) -> dict:
        """返回当前构建进度，供前端轮询。"""
        if not self._is_building:
            return {
                "is_building": False,
                "ready": self._is_ready,
                "file_count": self.file_count,
            }
        return {
            "is_building": True,
            "ready": False,
            "file_count": self.file_count,
            "stage": self._current_stage,
            "scanned": self._scanned_count,
            "scanned_total": self._scanned_total_estimate,
            "summary_done": self._summary_done,
            "summary_total": self._summary_total,
        }

    # ── 加载已有索引 ──────────────────────────────────────────────────────

    def load_existing(self) -> bool:
        """从 .autorun/index/ 加载已有索引。返回是否成功加载。"""
        if not os.path.isfile(self._manifest_path):
            return False

        try:
            with open(self._manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.debug("Failed to load index manifest: %s", e)
            return False

        version = data.get("version", 1)
        if version not in (1, 2):
            logger.debug("Index manifest version mismatch: %d, skipping", version)
            return False

        manifest = IndexManifest(
            version=2,
            project_root=data.get("project_root", ""),
            last_full_scan=data.get("last_full_scan", ""),
        )

        for rel_path, entry_data in data.get("files", {}).items():
            abs_path = os.path.join(self._project_root, rel_path)
            if not os.path.isfile(abs_path):
                continue

            entry = FileEntry(
                rel_path=rel_path,
                abs_path=abs_path,
                md5=entry_data.get("md5", ""),
                file_type=entry_data.get("type", "other"),
                size=entry_data.get("size", 0),
                mtime=entry_data.get("mtime", 0.0),
            )
            manifest.files[rel_path] = entry

        # 加载 JSON 摘要文件
        summaries: Dict[str, FileSummary] = {}
        if os.path.isdir(self._summaries_dir):
            for summary_file in os.listdir(self._summaries_dir):
                if summary_file.endswith(".json"):
                    md5_key = summary_file[:-5]
                    filepath = os.path.join(self._summaries_dir, summary_file)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            data_fs = json.load(f)
                        summaries[md5_key] = FileSummary(
                            dependencies=data_fs.get("dependencies", ""),
                            functionality=data_fs.get("functionality", ""),
                            logic=data_fs.get("logic", ""),
                            notes=data_fs.get("notes", ""),
                            relationships=data_fs.get("relationships", ""),
                        )
                    except (json.JSONDecodeError, IOError, OSError):
                        pass

        self._manifest = manifest
        self._summaries = summaries
        self._is_ready = True
        self._user_wants_index = True
        self._version = 1

        # 加载后按优先级裁剪
        trimmed = self._trim_by_priority(manifest)

        logger.debug("Loaded index: %d files, %d summaries (trimmed: %d)",
                     len(manifest.files), len(summaries), trimmed)
        return True

    # ── 用户提示 ──────────────────────────────────────────────────────────

    def needs_prompt(self) -> bool:
        """是否需要询问用户是否构建索引。"""
        return (not self._is_ready
                and not self._is_building
                and self._user_wants_index is None
                and not self._user_declined)

    def mark_user_response(self, accepted: bool) -> None:
        """记录用户对索引构建的回复。"""
        self._user_wants_index = accepted
        if not accepted:
            self._user_declined = True
            return
        # 防重入：已在构建中或已就绪则跳过
        if self._is_building or self._is_ready:
            return
        # 重置进度计数器
        self._scanned_count = 0
        self._scanned_total_estimate = 0
        self._summary_done = 0
        self._summary_total = 0
        self._current_stage = ""
        # 启动后台构建
        self._is_building = True
        self._build_task = asyncio.create_task(self._build_initial())

    # ── 初始构建 ──────────────────────────────────────────────────────────

    async def _build_initial(self) -> None:
        """后台构建初始索引（异步，非阻塞）。"""
        try:
            logger.info("Starting initial index build for %s", self._project_root)
            start_time = time.time()

            # 1. 扫描项目
            self._current_stage = "scanning"
            manifest = IndexManifest(
                project_root=self._project_root,
                last_full_scan=datetime_iso(),
            )
            await self._scan_project(manifest)

            if not manifest.files:
                logger.info("No files found to index")
                self._is_building = False
                self._is_ready = True
                return

            # 按优先级裁剪到 MAX_PRIORITY_FILES
            trimmed = self._trim_by_priority(manifest)

            logger.info("Scanned %d files in %.1fs (trimmed: %d)",
                        len(manifest.files), time.time() - start_time, trimmed)

            # 2. 批量生成摘要
            self._current_stage = "summarizing"
            self._scanned_total_estimate = len(manifest.files)
            await self._generate_summaries(manifest)

            # 3. 确保目录存在
            os.makedirs(self._summaries_dir, exist_ok=True)

            # 4. 写盘
            self._save_manifest(manifest)
            self._save_summaries()

            # 5. 激活
            self._manifest = manifest
            self._is_ready = True
            self._is_building = False
            self._version += 1

            # 6. 启动轮询
            self.start_polling()

            logger.info("Index build complete: %d files in %.1fs",
                        len(manifest.files), time.time() - start_time)

        except Exception:
            logger.debug("Index build failed", exc_info=True)
            self._is_building = False
            # 即使构建失败，也标记为就绪（无摘要模式）
            self._is_ready = True

    async def _scan_project(self, manifest: IndexManifest) -> None:
        """扫描项目目录，收集文件信息。"""
        count = 0
        max_files = MAX_INITIAL_FILES

        for dirpath, dirnames, filenames in os.walk(self._project_root):
            # 过滤目录
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in EXCLUDED_DIRS
                or d == ".autorun"  # .autorun 需要扫描（检查和写入索引）
            ]

            # 跳过 .autorun 内部子目录
            rel_dir = os.path.relpath(dirpath, self._project_root)
            if rel_dir.startswith(".autorun") and rel_dir != ".autorun":
                continue

            for filename in filenames:
                if count >= max_files:
                    return

                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, self._project_root).replace("\\", "/")

                if _should_skip(abs_path, self._project_root):
                    continue

                try:
                    stat = os.stat(abs_path)
                except OSError:
                    continue

                entry = FileEntry(
                    rel_path=rel_path,
                    abs_path=abs_path,
                    md5=_compute_md5(abs_path),
                    file_type=_classify_file(abs_path),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
                manifest.files[rel_path] = entry
                count += 1
                self._scanned_count = count

    def _trim_by_priority(self, manifest: IndexManifest) -> int:
        """按优先级排序 manifest.files 并裁剪到 MAX_PRIORITY_FILES。

        返回被裁剪掉的文件数量。
        """
        if len(manifest.files) <= MAX_PRIORITY_FILES:
            self._was_priority_trimmed = False
            return 0

        # 按优先级排序
        sorted_files = sorted(
            manifest.files.items(),
            key=lambda item: _compute_priority_key(item[0], item[1].file_type),
        )

        # 保留前 MAX_PRIORITY_FILES 个
        kept = dict(sorted_files[:MAX_PRIORITY_FILES])
        removed_count = len(manifest.files) - len(kept)
        manifest.files = kept
        self._was_priority_trimmed = True

        logger.info(
            "Priority trim: kept %d/%d files, removed %d lower-priority files",
            MAX_PRIORITY_FILES, MAX_PRIORITY_FILES + removed_count, removed_count,
        )
        return removed_count

    async def _generate_summaries(self, manifest: IndexManifest) -> None:
        """使用低模型批量生成文件摘要。"""
        # 收集需要摘要的文件（排除太大的、二进制的、已用相同 MD5 缓存的）
        pending: List[FileEntry] = []

        for entry in manifest.files.values():
            if entry.file_type in ("dir", "other"):
                continue
            if entry.size > MAX_FILE_SIZE_FOR_SUMMARY:
                continue
            if entry.md5 in self._summaries:
                # 已有摘要缓存
                continue
            if entry.file_type == "asset":
                # 素材文件：仅记录元数据，不调用 AI
                ext = os.path.splitext(entry.rel_path)[1].lower()
                self._summaries[entry.md5] = FileSummary(
                    functionality=f"素材文件 ({ext}, {_format_size(entry.size)})"
                )
                continue
            if entry.file_type == "script":
                # 代码文件超过 MAX_CODE_LINES 行则跳过
                if _count_lines(entry.abs_path) > MAX_CODE_LINES:
                    continue
            pending.append(entry)

        if not pending:
            return

        logger.info("Generating summaries for %d files via low model", len(pending))

        # 初始化进度
        self._summary_total = len(pending)
        self._summary_done = 0

        # 双并发处理：按 BATCH_SIZE 切分批次，2 个并发
        batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
        semaphore = asyncio.Semaphore(2)

        async def _process_batch(batch: List[FileEntry]) -> None:
            async with semaphore:
                try:
                    batch_summaries = await self._call_low_model_for_batch(batch)
                    async with self._lock:
                        self._summaries.update(batch_summaries)
                        self._summary_done += len(batch_summaries)
                except Exception:
                    logger.debug("Summary batch failed", exc_info=True)
                    # 为失败的批次生成占位摘要
                    async with self._lock:
                        for entry in batch:
                            if entry.md5 not in self._summaries:
                                self._summaries[entry.md5] = FileSummary(
                                    functionality=f"{entry.file_type} 文件 ({_format_size(entry.size)})"
                                )
                        self._summary_done += len(batch)

        await asyncio.gather(*[_process_batch(b) for b in batches])

    async def _call_low_model_for_batch(self, entries: List[FileEntry]) -> Dict[str, FileSummary]:
        """调用低模型为一组文件生成摘要。

        返回 {md5: FileSummary} 字典。
        """
        from AutoRUN_v1.utils.config import get_api_key, get_api_url, get_api_type, get_model
        from AutoRUN_v1.utils.model_resolver import resolve_low_model

        api_key = get_api_key()
        api_url = get_api_url()
        api_type = get_api_type()
        main_model = get_model()

        if not api_key or not api_url or not main_model:
            raise RuntimeError("API not configured")

        low_model, extra_kwargs = resolve_low_model(main_model, api_type)
        logger.debug("Using low model for summaries: %s (extra: %s)",
                     low_model, extra_kwargs)

        # 构建批次提示词
        prompt = self._build_batch_prompt(entries)

        if api_type == "openai":
            result_text = await self._call_openai_for_summary(
                api_key, api_url, low_model, prompt, extra_kwargs
            )
        elif api_type == "anthropic":
            result_text = await self._call_anthropic_for_summary(
                api_key, api_url, low_model, prompt
            )
        else:
            raise RuntimeError(f"Unsupported API type: {api_type}")

        # 解析结果
        return self._parse_batch_response(result_text, entries)

    def _build_batch_prompt(self, entries: List[FileEntry]) -> str:
        """构建批量摘要提示词（结构化5字段输出）。"""
        parts = [
            "你是一个代码库分析器。请为以下每个文件分析并输出结构化摘要，",
            "包含5个维度：依赖(dependencies)、功能(functionality)、逻辑(logic)、",
            "注意事项(notes)、和其他文件的关系(relationships)。\n",
        ]

        for entry in entries:
            parts.append(f"<file path=\"{entry.rel_path}\" type=\"{entry.file_type}\">")
            if entry.file_type == "script" and entry.size < MAX_FILE_SIZE_FOR_SUMMARY:
                content = _read_file_head(entry.abs_path, READ_HEAD_LINES)
                if content:
                    parts.append(content[:3000])
            elif entry.file_type == "doc" and entry.size < MAX_FILE_SIZE_FOR_SUMMARY:
                if _is_data_file(entry.abs_path):
                    # 数据文件：随机采样
                    content = _sample_data_file(entry.abs_path)
                    if content:
                        parts.append(content[:5000])
                else:
                    content = _read_file_head(entry.abs_path, 80)
                    if content:
                        parts.append(content[:2000])
            elif entry.file_type == "asset":
                ext = os.path.splitext(entry.rel_path)[1].lower()
                parts.append(f"[素材文件: {ext}, {_format_size(entry.size)}]")
            else:
                parts.append(f"[{entry.file_type} 文件: {_format_size(entry.size)}]")
            parts.append("</file>\n")

        parts.append(
            "请为每个文件输出以下格式（每个字段一行，中文，简洁但涵盖关键信息）：\n"
            "<file path=\"文件路径\">\n"
            "<dependencies>依赖了哪些模块/包/库（如果是数据/素材文件则填\"无\"）</dependencies>\n"
            "<functionality>文件的主要功能（15-50字）</functionality>\n"
            "<logic>核心逻辑流程或数据结构的简要描述（20-60字）</logic>\n"
            "<notes>需要注意的事项、限制、或特殊约定（10-40字）</notes>\n"
            "<relationships>与项目中其他文件的关系，如被谁调用/调用谁（10-50字）</relationships>\n"
            "</file>\n"
            "不要添加额外说明，只输出上述格式的 file 标签。"
        )
        return "\n".join(parts)

    async def _call_openai_for_summary(
        self, api_key: str, api_url: str, model: str,
        prompt: str, extra_kwargs: Optional[Dict[str, Any]] = None
    ) -> str:
        """通过 OpenAI 兼容 API 生成摘要。"""
        import httpx

        url = api_url.rstrip("/") + "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8192,
            "temperature": 0.3,
        }
        # 合并额外参数（如 reasoning_effort）
        if extra_kwargs:
            body.update(extra_kwargs)

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

    async def _call_anthropic_for_summary(
        self, api_key: str, api_url: str, model: str, prompt: str
    ) -> str:
        """通过 Anthropic 兼容 API 生成摘要。"""
        import httpx

        url = api_url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "system": "你是一个代码库分析器。使用中文回复。",
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8192,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data["content"][0]["text"].strip()

    @staticmethod
    def _extract_tag(text: str, tag: str) -> str:
        """从文本中提取指定标签的内容。"""
        pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
        match = pattern.search(text)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _parse_batch_response(text: str, entries: List[FileEntry]) -> Dict[str, FileSummary]:
        """解析低模型返回的批量结构化摘要。"""
        results: Dict[str, FileSummary] = {}

        for entry in entries:
            # 提取该文件的 <file path="...">...</file> 块
            pattern = re.compile(
                rf'<file\s+path="{re.escape(entry.rel_path)}">(.*?)</file>',
                re.DOTALL,
            )
            match = pattern.search(text)
            if match:
                inner = match.group(1)
                results[entry.md5] = FileSummary(
                    dependencies=FileIndexer._extract_tag(inner, "dependencies"),
                    functionality=FileIndexer._extract_tag(inner, "functionality"),
                    logic=FileIndexer._extract_tag(inner, "logic"),
                    notes=FileIndexer._extract_tag(inner, "notes"),
                    relationships=FileIndexer._extract_tag(inner, "relationships"),
                )
            else:
                # 兜底：尝试旧格式 <summary path="...">...</summary>
                old_pattern = re.compile(
                    rf'<summary\s+path="{re.escape(entry.rel_path)}">(.*?)</summary>',
                    re.DOTALL,
                )
                old_match = old_pattern.search(text)
                if old_match:
                    results[entry.md5] = FileSummary(
                        functionality=old_match.group(1).strip(),
                    )
                else:
                    results[entry.md5] = FileSummary()

        return results

    # ── 保存 ───────────────────────────────────────────────────────────────

    def _save_manifest(self, manifest: Optional[IndexManifest] = None) -> None:
        """保存清单到磁盘。"""
        if manifest is None:
            manifest = self._manifest
        if manifest is None:
            return

        os.makedirs(self._index_dir, exist_ok=True)

        data = {
            "version": 2,
            "project_root": manifest.project_root,
            "last_full_scan": manifest.last_full_scan,
            "files": {
                rel_path: {
                    "md5": entry.md5,
                    "type": entry.file_type,
                    "size": entry.size,
                    "mtime": entry.mtime,
                }
                for rel_path, entry in manifest.files.items()
            },
        }

        try:
            with open(self._manifest_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.debug("Failed to save manifest: %s", e)

    def _save_summaries(self) -> None:
        """保存所有摘要到磁盘（JSON 格式）。"""
        os.makedirs(self._summaries_dir, exist_ok=True)
        for md5_key, fs in self._summaries.items():
            path = os.path.join(self._summaries_dir, f"{md5_key}.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({
                        "dependencies": fs.dependencies,
                        "functionality": fs.functionality,
                        "logic": fs.logic,
                        "notes": fs.notes,
                        "relationships": fs.relationships,
                    }, f, ensure_ascii=False)
            except IOError as e:
                logger.debug("Failed to save summary %s: %s", md5_key, e)

    # ── 定时轮询 ──────────────────────────────────────────────────────────

    def start_polling(self) -> None:
        """启动定时 MD5 轮询（取消之前的任务）。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """后台轮询循环：每 60s 检查文件变化。"""
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._check_changes()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Poll loop error", exc_info=True)

    async def _check_changes(self) -> None:
        """检查 manifest 中所有文件的 MD5 变化。"""
        if not self._manifest:
            return

        async with self._lock:
            changed_entries: List[FileEntry] = []
            removed_paths: List[str] = []
            new_entries: List[FileEntry] = []

            # 1. 检查已有文件
            for rel_path, entry in list(self._manifest.files.items()):
                abs_path = os.path.join(self._project_root, rel_path)

                if not os.path.isfile(abs_path):
                    removed_paths.append(rel_path)
                    continue

                current_md5 = _compute_md5(abs_path)
                if current_md5 and current_md5 != entry.md5:
                    entry.md5 = current_md5
                    try:
                        stat = os.stat(abs_path)
                        entry.size = stat.st_size
                        entry.mtime = stat.st_mtime
                    except OSError:
                        pass
                    changed_entries.append(entry)

            # 2. 处理工具推送的变更
            for rel_path in list(self._pending_updates):
                abs_path = os.path.join(self._project_root, rel_path)
                if os.path.isfile(abs_path):
                    md5 = _compute_md5(abs_path)
                    if rel_path in self._manifest.files:
                        entry = self._manifest.files[rel_path]
                        if md5 != entry.md5:
                            entry.md5 = md5
                            changed_entries.append(entry)
                    else:
                        try:
                            stat = os.stat(abs_path)
                            entry = FileEntry(
                                rel_path=rel_path,
                                abs_path=abs_path,
                                md5=md5,
                                file_type=_classify_file(abs_path),
                                size=stat.st_size,
                                mtime=stat.st_mtime,
                            )
                            new_entries.append(entry)
                        except OSError:
                            pass
                elif rel_path in self._manifest.files:
                    removed_paths.append(rel_path)
                self._pending_updates.discard(rel_path)

            # 3. 移除已删除的文件
            for rel_path in removed_paths:
                del self._manifest.files[rel_path]

            # 4. 添加新文件
            for entry in new_entries:
                self._manifest.files[entry.rel_path] = entry

            # 5. 合并变更
            all_changed = changed_entries + new_entries

            if removed_paths:
                logger.debug("Indexer: %d files removed", len(removed_paths))
            if all_changed:
                logger.info("Indexer: %d files changed, re-summarizing", len(all_changed))
                try:
                    await self._generate_summaries_for_entries(all_changed)
                except Exception:
                    logger.debug("Re-summarization failed", exc_info=True)

            # 6. 按优先级裁剪
            if new_entries or all_changed:
                self._trim_by_priority(self._manifest)

            # 更新扫描时间并保存
            if all_changed or removed_paths:
                self._manifest.last_full_scan = datetime_iso()
                self._save_manifest()
                self._save_summaries()
                self._version += 1

    async def _generate_summaries_for_entries(self, entries: List[FileEntry]) -> None:
        """为变更的文件重新生成摘要（双并发）。"""
        pending = []
        for e in entries:
            if e.file_type in ("dir", "other"):
                continue
            if e.size > MAX_FILE_SIZE_FOR_SUMMARY:
                continue
            if e.file_type == "script" and _count_lines(e.abs_path) > MAX_CODE_LINES:
                continue
            if e.file_type == "asset":
                ext = os.path.splitext(e.rel_path)[1].lower()
                self._summaries[e.md5] = FileSummary(
                    functionality=f"素材文件 ({ext}, {_format_size(e.size)})"
                )
                continue
            pending.append(e)

        if not pending:
            return

        batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
        semaphore = asyncio.Semaphore(2)

        async def _process(batch: List[FileEntry]) -> None:
            async with semaphore:
                try:
                    batch_summaries = await self._call_low_model_for_batch(batch)
                    async with self._lock:
                        self._summaries.update(batch_summaries)
                except Exception:
                    logger.debug("Re-summary batch failed", exc_info=True)

        await asyncio.gather(*[_process(b) for b in batches])

    # ── 推送通知 ──────────────────────────────────────────────────────────

    def notify_file_changed(self, file_path: str) -> None:
        """收到文件变更通知（由 Write/Edit 工具调用）。"""
        try:
            rel_path = os.path.relpath(file_path, self._project_root).replace("\\", "/")
            # 跳过排除目录中的文件
            if _should_skip(file_path, self._project_root):
                return
            self._pending_updates.add(rel_path)
        except (ValueError, OSError):
            pass  # 文件不在项目内

    # ── 上下文注入 ─────────────────────────────────────────────────────────

    def get_injectable_context(self) -> str:
        """返回注入系统提示词的索引文本（含5字段结构化摘要）。"""
        if not self._manifest or not self._is_ready:
            return ""

        script_entries: List[Tuple[str, FileEntry]] = []
        doc_entries: List[Tuple[str, FileEntry]] = []
        asset_entries: List[Tuple[str, FileEntry]] = []
        other_entries: List[Tuple[str, FileEntry]] = []

        for rel_path, entry in self._manifest.files.items():
            if entry.file_type == "script":
                script_entries.append((rel_path, entry))
            elif entry.file_type == "doc":
                doc_entries.append((rel_path, entry))
            elif entry.file_type == "asset":
                asset_entries.append((rel_path, entry))
            else:
                other_entries.append((rel_path, entry))

        lines = []

        # 优先级裁剪警告（开头）
        if self._was_priority_trimmed:
            lines.append(PRIORITY_WARNING.format(
                MAX_PRIORITY_FILES, MAX_PRIORITY_FILES
            ))
            lines.append("")

        # 目录结构
        lines.append("## 项目文件列表\n")
        lines.append("```")
        tree = self._build_tree()
        lines.append(tree if tree else "(空项目)")
        lines.append("```\n")

        # 文件摘要（按优先级：script → doc → asset → other）
        lines.append("## 关键文件摘要\n")

        for label, entries in [
            ("脚本文件", script_entries),
            ("文档文件", doc_entries),
            ("素材文件", asset_entries),
            ("其他文件", other_entries),
        ]:
            if not entries:
                continue
            lines.append(f"### {label}")
            for rel_path, entry in entries:
                fs = self._summaries.get(entry.md5)
                if fs:
                    lines.append(f"- `{rel_path}`")
                    if fs.functionality:
                        lines.append(f"  - 功能：{fs.functionality}")
                    if fs.dependencies and fs.dependencies != "无":
                        lines.append(f"  - 依赖：{fs.dependencies}")
                    if fs.logic:
                        lines.append(f"  - 逻辑：{fs.logic}")
                    if fs.notes:
                        lines.append(f"  - 注意：{fs.notes}")
                    if fs.relationships:
                        lines.append(f"  - 关系：{fs.relationships}")
                else:
                    lines.append(f"- `{rel_path}` ({entry.file_type}, {_format_size(entry.size)})")
            lines.append("")

        # 优先级裁剪警告（结尾，再次醒目提示）
        if self._was_priority_trimmed:
            lines.append("---")
            lines.append(PRIORITY_WARNING.format(
                MAX_PRIORITY_FILES, MAX_PRIORITY_FILES
            ))

        return "\n".join(lines)

    def _build_tree(self) -> str:
        """构建简单的目录树文本。"""
        if not self._manifest:
            return ""

        # 收集所有目录
        dirs: Set[str] = set()
        for rel_path in self._manifest.files:
            parent = os.path.dirname(rel_path)
            while parent:
                dirs.add(parent)
                parent = os.path.dirname(parent)

        tree_parts = [os.path.basename(self._project_root) or self._project_root]

        # 按层级排序
        sorted_dirs = sorted(dirs)
        sorted_files = sorted(self._manifest.files.keys())

        for d in sorted_dirs:
            depth = d.count("/") + 1
            prefix = "  " * depth
            tree_parts.append(f"{prefix}{os.path.basename(d)}/")

        for f in sorted_files:
            depth = f.count("/") + 1
            prefix = "  " * depth
            file_type = self._manifest.files[f].file_type
            icon = {"script": "⚙", "doc": "📄", "asset": "🖼", "other": "·"}.get(file_type, "·")
            tree_parts.append(f"{prefix}{icon} {os.path.basename(f)}")

        return "\n".join(tree_parts)

    # ── 清理 ───────────────────────────────────────────────────────────────

    @staticmethod
    def reinit_for_cwd(new_cwd: str, state: Any = None) -> "FileIndexer":
        """为新的工作目录重新创建索引器。

        向上查找最近的包含 .autorun/index/ 的目录作为项目根目录。
        如果没找到，则直接用 new_cwd 作为项目根目录。
        返回新实例。
        """
        project_root = resolve_project_root(new_cwd)

        indexer = FileIndexer(project_root=project_root, state=state)
        if indexer.load_existing():
            indexer.start_polling()
        if state is not None:
            # 如果已有旧索引器，关闭它
            old = getattr(state, "indexer", None)
            if old is not None:
                old.shutdown()
            state.indexer = indexer
        return indexer

    def shutdown(self) -> None:
        """停止轮询并清理资源。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None
        if self._build_task and not self._build_task.done():
            self._build_task.cancel()

    def delete_index(self) -> bool:
        """删除整个索引目录并重置状态。返回 True 表示成功。"""
        self.shutdown()

        import shutil
        try:
            if os.path.isdir(self._index_dir):
                shutil.rmtree(self._index_dir)
        except OSError as e:
            logger.debug("Failed to delete index directory: %s", e)
            return False

        # 重置状态
        self._manifest = None
        self._summaries.clear()
        self._pending_updates.clear()
        self._is_ready = False
        self._is_building = False
        self._user_wants_index = None
        self._user_declined = False
        self._version = 0
        self._was_priority_trimmed = False
        self._scanned_count = 0
        self._scanned_total_estimate = 0
        self._summary_done = 0
        self._summary_total = 0
        self._current_stage = ""

        return True


# ── 工具函数 ─────────────────────────────────────────────────────────────────


def resolve_project_root(start_dir: str) -> str:
    """从 start_dir 向上查找最近的包含 .autorun/index/ 的目录作为项目根目录。

    遍历父目录，找到第一个含 .autorun/index/manifest.json 的目录。
    如果没找到则返回 start_dir 本身。
    """
    current = os.path.abspath(start_dir)
    drive = os.path.splitdrive(current)[0] + os.sep
    while True:
        candidate = os.path.join(current, ".autorun", "index", "manifest.json")
        if os.path.isfile(candidate):
            return current
        parent = os.path.dirname(current)
        if parent == current or parent == drive:
            return start_dir
        current = parent


def _read_file_head(file_path: str, max_lines: int) -> str:
    """读取文件的前 N 行。"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
            return "\n".join(lines)
    except Exception:
        return ""


def _format_size(size: int) -> str:
    """格式化文件大小。"""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"


def quick_estimate(project_root: str, stop_at: int = 201) -> int:
    """快速估算项目根目录下可索引的文件/目录数（不超过 stop_at）。

    纯文件系统操作，不构建索引，仅用于提前预警。
    排除隐藏目录、node_modules 等。（与 _scan_project 行为一致）
    """
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(project_root):
            # 过滤目录（与 _scan_project 一致）
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in EXCLUDED_DIRS
            ]
            rel_dir = os.path.relpath(dirpath, project_root)
            if rel_dir.startswith(".autorun"):
                continue

            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                if _should_skip(abs_path, project_root):
                    continue
                count += 1
                if count >= stop_at:
                    return count

            for dirname in dirnames:
                abs_path = os.path.join(dirpath, dirname)
                if _should_skip(abs_path, project_root):
                    continue
                count += 1
                if count >= stop_at:
                    return count
    except (OSError, PermissionError):
        pass
    return count


def datetime_iso() -> str:
    """返回带时区的 ISO 时间戳。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _compute_priority_key(rel_path: str, file_type: str) -> tuple:
    """计算文件优先级排序键（升序，越小优先级越高）。

    规则：
      1. PRIORITY_FILE_NAMES 中的文件名 → 最高优先级
      2. 深度越浅 → 优先级越高
      3. 文件类型：script > doc > other > asset
      4. 同优先级按路径字母序
    """
    basename = os.path.basename(rel_path)
    depth = rel_path.count("/")

    # 高优先级文件名排在前面
    is_priority_file = 0 if basename in PRIORITY_FILE_NAMES else 1

    # 文件类型排序权重（越小优先级越高）
    type_rank = {"script": 0, "doc": 1, "other": 2}.get(file_type, 3)  # asset=3

    return (is_priority_file, depth, type_rank, rel_path)
