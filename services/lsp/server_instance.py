"""
单个语言服务器的生命周期管理器。

对应 src/services/lsp/LSPServerInstance.ts — 负责:
- 服务器进程的启动/停止/崩溃重启
- LSP 初始化握手（initialize → initialized → workspace/didChangeConfiguration）
- 请求转发和通知注册
- ContentModified 错误重试（代码 -32801）
- workspace/configuration 请求自动响应
"""

import asyncio
import logging
import os
import shutil
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from AutoRUN_v1.services.lsp.client import LspClient, LspError

logger = logging.getLogger(__name__)

# 默认超时（毫秒）
DEFAULT_STARTUP_TIMEOUT_MS = 30_000
DEFAULT_SHUTDOWN_TIMEOUT_MS = 5_000
MAX_RESTARTS = 3
MAX_LSP_FILE_SIZE_BYTES = 10_000_000  # 10MB


class LspServerState(str, Enum):
    """服务器状态（对应 CC LspServerState）。"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class LspServerConfig:
    """语言服务器配置（对应 CC ScopedLspServerConfig）。

    关键字段:
    - command: 可执行文件路径
    - args: 命令行参数
    - env: 环境变量
    - extensionToLanguage: 扩展名 → languageId 映射
      （如 {".py": "python", ".ts": "typescript"}）
    - initializationOptions: 初始化时传给服务器的选项
    - settings: 服务器设置
    """

    def __init__(
        self,
        name: str,
        command: str,
        extension_to_language: Dict[str, str],
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        initialization_options: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        startup_timeout_ms: int = DEFAULT_STARTUP_TIMEOUT_MS,
        shutdown_timeout_ms: int = DEFAULT_SHUTDOWN_TIMEOUT_MS,
        restart_on_crash: bool = True,
        max_restarts: int = MAX_RESTARTS,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.extension_to_language = extension_to_language
        self.initialization_options = initialization_options
        self.settings = settings
        self.startup_timeout_ms = startup_timeout_ms
        self.shutdown_timeout_ms = shutdown_timeout_ms
        self.restart_on_crash = restart_on_crash
        self.max_restarts = max_restarts


class LspServerInstance:
    """单个语言服务器进程管理器（对应 CC LSPServerInstance）。

    管理完整的 LSP 服务器生命周期，包括:
    - start(): 启动子进程并完成初始化握手
    - stop(): 优雅关闭（shutdown → exit）
    - sendRequest(): 转发 LSP 请求
    - sendNotification(): 发送 LSP 通知
    - onNotification(): 注册服务器推送通知的处理器
    - onRequest(): 注册服务器请求的处理器

    内置 ContentModified（-32801）重试逻辑和崩溃自动重启。
    """

    def __init__(self, config: LspServerConfig, workspace_root: str):
        self.config = config
        self.workspace_root = workspace_root
        self.state: LspServerState = LspServerState.STOPPED
        self._client: Optional[LspClient] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._restart_count: int = 0
        self._generation: int = 0
        self._server_capabilities: Dict[str, Any] = {}
        self._request_handlers: Dict[str, Callable] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动语言服务器并完成 LSP 初始化握手。

        如果服务器已在运行，直接返回。
        如果之前出错，允许重试。
        """
        async with self._lock:
            if self.state == LspServerState.RUNNING:
                return
            if self.state == LspServerState.STARTING:
                return  # 已经在启动中

            self.state = LspServerState.STARTING
            self._generation += 1
            generation = self._generation

            try:
                await self._do_start()
                self.state = LspServerState.RUNNING
                self._restart_count = 0
                logger.info(
                    f"LSP server '{self.config.name}' started successfully"
                )
            except Exception as e:
                self.state = LspServerState.ERROR
                await self._cleanup()

                if (
                    self.config.restart_on_crash
                    and self._restart_count < self.config.max_restarts
                ):
                    self._restart_count += 1
                    logger.warning(
                        f"LSP server '{self.config.name}' crashed, "
                        f"restarting ({self._restart_count}/{self.config.max_restarts})..."
                    )
                    await self.start()
                else:
                    raise LspStartError(
                        f"Failed to start LSP server '{self.config.name}': {e}"
                    ) from e

    async def _do_start(self) -> None:
        """内部启动逻辑: spawn进程 → 建立连接 → 初始化握手。"""
        # 检查命令是否存在
        if not shutil.which(self.config.command):
            raise LspStartError(
                f"Language server '{self.config.command}' not found on PATH"
            )

        # 启动子进程
        env = os.environ.copy()
        if self.config.env:
            env.update(self.config.env)

        cmd = [self.config.command] + self.config.args
        logger.debug(f"Spawning LSP server: {' '.join(cmd)}")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.workspace_root,
        )

        # 建立 JSON-RPC 客户端
        self._client = LspClient()
        await self._client.connect(self._process)

        # 注册默认的 workspace/configuration 请求处理器
        # 某些服务器（如 TypeScript）即使客户端声明 configuration=false
        # 也会发送此类请求，返回 null 列表满足协议要求
        self._request_handlers["workspace/configuration"] = (
            lambda params: [None] * len(params.get("items", []))
        )

        # 注册 exit 通知
        self.on_notification("exit", self._on_server_exit)

        # LSP 初始化握手
        await self._initialize_handshake()

    async def _initialize_handshake(self) -> None:
        """LSP 初始化握手: initialize → initialized → workspace/didChangeConfiguration。"""
        assert self._client is not None

        root_uri = Path(self.workspace_root).resolve().as_uri()

        init_params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": self.workspace_root,
            "workspaceFolders": [
                {"uri": root_uri, "name": Path(self.workspace_root).name}
            ],
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "implementation": {"linkSupport": True},
                    "callHierarchy": {},
                    "publishDiagnostics": {
                        "tagSupport": {"valueSet": [1, 2]},
                        "codeDescriptionSupport": True,
                    },
                },
                "workspace": {
                    "symbol": {"symbolKind": {"valueSet": list(range(1, 27))}},
                    "configuration": False,
                },
                # UTF-16 位置编码（标准 LSP 要求）
                "offsetEncoding": ["utf-16"],
            },
            "initializationOptions": self.config.initialization_options,
        }

        timeout = self.config.startup_timeout_ms / 1000.0
        try:
            result = await asyncio.wait_for(
                self._client.send_request("initialize", init_params),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise LspStartError(
                f"LSP server '{self.config.name}' initialize timed out"
            )

        self._server_capabilities = cast_dict(result.get("capabilities", {}))

        # initialized 通知
        await self._client.send_notification("initialized", {})

        # workspace/didChangeConfiguration
        if self.config.settings:
            await self._client.send_notification(
                "workspace/didChangeConfiguration",
                {"settings": self.config.settings},
            )

    async def stop(self) -> None:
        """优雅关闭: shutdown 请求 → exit 通知 → 进程终止。"""
        if self.state == LspServerState.STOPPED:
            return

        self.state = LspServerState.STOPPING

        try:
            if self._client and self._client.is_connected:
                # shutdown 请求
                try:
                    await asyncio.wait_for(
                        self._client.send_request("shutdown", {}),
                        timeout=self.config.shutdown_timeout_ms / 1000.0,
                    )
                except Exception:
                    pass
                # exit 通知
                try:
                    await self._client.send_notification("exit", {})
                except Exception:
                    pass
        finally:
            await self._cleanup()
            self.state = LspServerState.STOPPED

    async def _cleanup(self) -> None:
        """清理客户端和进程资源。"""
        if self._client:
            await self._client.close()
            self._client = None

        if self._process:
            try:
                if self._process.returncode is None:
                    self._process.terminate()
                    try:
                        await asyncio.wait_for(
                            self._process.wait(),
                            timeout=self.config.shutdown_timeout_ms / 1000.0,
                        )
                    except asyncio.TimeoutError:
                        self._process.kill()
                        await self._process.wait()
            except Exception:
                pass
            self._process = None

    async def _on_server_exit(self, _params: Any) -> None:
        """服务器主动发送 exit 通知时的回调。"""
        logger.debug(f"LSP server '{self.config.name}' sent exit notification")
        self.state = LspServerState.STOPPED

    # ── 请求 / 通知 ──────────────────────────────────────────────────────

    async def send_request(self, method: str, params: Any = None) -> Any:
        """向 LSP 服务器发送请求并返回结果（对应 CC sendRequest）。

        包含 ContentModified（-32801）错误重试逻辑:
        这种错误通常来自 rust-analyzer 等服务器在索引期间，
        使用指数退避重试（500ms, 1s, 2s），最多 3 次。
        """
        if self.state != LspServerState.RUNNING or self._client is None:
            raise RuntimeError(
                f"LSP server '{self.config.name}' is not running"
            )

        generation = self._generation
        last_error = None

        for attempt in range(4):  # 初始 + 3 次重试
            try:
                result = await self._client.send_request(method, params)
                if generation != self._generation:
                    raise RuntimeError(
                        f"LSP server '{self.config.name}' was restarted"
                    )
                return result
            except LspError as e:
                last_error = e
                # ContentModified (-32801) — 可重试
                if e.code == -32801 and attempt < 3:
                    delay = 500 * (2 ** attempt) / 1000.0  # 500ms, 1s, 2s
                    logger.debug(
                        f"LSP {method} content modified, retrying "
                        f"({attempt + 1}/3) after {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception:
                raise

        # 理论上不会到这里，但确保总有一个异常
        if last_error:
            raise last_error

    async def send_notification(self, method: str, params: Any = None) -> None:
        """发送 LSP 通知（无响应）。"""
        if self.state != LspServerState.RUNNING or not self._client:
            raise RuntimeError(
                f"LSP server '{self.config.name}' is not running"
            )
        await self._client.send_notification(method, params)

    def on_notification(self, method: str, handler: Callable[[Any], None]) -> None:
        """注册服务器推送通知的处理器（对应 CC onNotification）。

        当服务器发送 method 指定的通知时，handler 会被调用。
        """
        if self._client:
            self._client.on_notification(method, handler)

    def on_request(self, method: str, handler: Callable[[Any], Any]) -> None:
        """注册服务器请求的处理器（对应 CC onRequest）。

        用于处理服务器主动发送的请求（如 workspace/configuration）。
        """
        self._request_handlers[method] = handler

    # ── 状态查询 ────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> Dict[str, Any]:
        """服务器能力描述（来自 initialize 响应）。"""
        return self._server_capabilities

    def is_available(self) -> bool:
        """服务器是否可用（运行中）。"""
        return self.state == LspServerState.RUNNING


class LspStartError(Exception):
    """语言服务器启动失败。"""
    pass


def cast_dict(obj: Any) -> Dict[str, Any]:
    """安全转换为 dict 类型。"""
    return obj if isinstance(obj, dict) else {}
