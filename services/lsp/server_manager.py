"""
多语言服务器管理器。

对应 src/services/lsp/LSPServerManager.ts — 管理多个 LSP 服务器实例，
按文件扩展名路由请求，处理文档同步（didOpen/didChange/didSave/didClose）。

API 完全对应 CC 的 LSPServerManager 类型:
- initialize(): 加载所有配置的 LSP 服务器
- shutdown(): 关闭所有服务器并清理状态
- getServerForFile(filePath): 按扩展名查找服务器
- ensureServerStarted(filePath): 确保服务器已启动
- sendRequest(filePath, method, params): 路由请求到正确的服务器
- openFile/changeFile/saveFile/closeFile: 文档生命周期同步
- isFileOpen(filePath): 检查文件是否已在服务器上打开
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from AutoRUN_v1.services.lsp.server_instance import (
    LspServerConfig,
    LspServerInstance,
    LspServerState,
)

logger = logging.getLogger(__name__)


class LspServerManager:
    """多 LSP 服务器管理器（完全对应 CC LSPServerManager）。"""

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root

        # 服务器实例（name → instance）
        self._servers: Dict[str, LspServerInstance] = {}

        # 扩展名 → 服务器名称列表（先注册的优先）
        self._extension_map: Dict[str, List[str]] = {}

        # 已在服务器上打开的文件（URI → server name）
        self._opened_files: Dict[str, str] = {}

        self._lock: asyncio.Lock = asyncio.Lock()

    # ── 服务器注册 ──────────────────────────────────────────────────────

    def register_server(self, config: LspServerConfig) -> None:
        """注册语言服务器配置（对应 CC initialize() 中的注册逻辑）。"""
        instance = LspServerInstance(config, self.workspace_root)
        self._servers[config.name] = instance

        # 注册 workspace/configuration 请求处理器
        # 某些服务器（如 TypeScript）即使声明 configuration=false 也会发送此请求
        instance.on_request(
            "workspace/configuration",
            lambda _params: None,  # 返回 null/None 列表
        )

        # 从 extension_to_language 提取扩展名 → 服务器映射
        for ext in config.extension_to_language:
            normalized = ext.lower()
            if normalized not in self._extension_map:
                self._extension_map[normalized] = []
            self._extension_map[normalized].append(config.name)

        logger.debug(
            f"Registered LSP server '{config.name}' "
            f"for {list(config.extension_to_language.keys())}"
        )

    def register_servers(self, configs: List[LspServerConfig]) -> None:
        """批量注册服务器配置。"""
        for config in configs:
            self.register_server(config)

    # ── 初始化 / 关闭 ────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """初始化管理器（对应 CC initialize()）。

        目前仅记录日志 — 实际启动是懒加载的。
        """
        logger.info(
            f"LSP server manager initialized: "
            f"{len(self._servers)} servers for {len(self._extension_map)} extensions"
        )

    async def shutdown(self) -> None:
        """关闭所有运行的服务器并清理状态（对应 CC shutdown()）。"""
        to_stop = [
            (name, inst) for name, inst in self._servers.items()
            if inst.state in (LspServerState.RUNNING, LspServerState.ERROR)
        ]

        results = await asyncio.gather(
            *[inst.stop() for _, inst in to_stop],
            return_exceptions=True,
        )

        self._servers.clear()
        self._extension_map.clear()
        self._opened_files.clear()

        errors = [
            f"{name}: {str(results[i])}"
            for i, (name, _) in enumerate(to_stop)
            if isinstance(results[i], Exception)
        ]

        if errors:
            err = RuntimeError(
                f"Failed to stop {len(errors)} LSP server(s): {'; '.join(errors)}"
            )
            logger.error(str(err))
            raise err

    # ── 服务器查找 ──────────────────────────────────────────────────────

    def get_server_for_file(self, file_path: str) -> Optional[LspServerInstance]:
        """按文件扩展名查找对应的 LSP 服务器（对应 CC getServerForFile）。"""
        ext = Path(file_path).suffix.lower()
        server_names = self._extension_map.get(ext, [])
        if not server_names:
            return None
        # 使用第一个注册的服务器（优先级最高）
        server_name = server_names[0]
        return self._servers.get(server_name)

    def get_all_servers(self) -> Dict[str, LspServerInstance]:
        """返回所有服务器实例（对应 CC getAllServers）。"""
        return self._servers

    # ── 服务器启动 ──────────────────────────────────────────────────────

    async def ensure_server_started(
        self, file_path: str
    ) -> Optional[LspServerInstance]:
        """确保文件对应的服务器已启动（对应 CC ensureServerStarted）。"""
        server = self.get_server_for_file(file_path)
        if server is None:
            return None

        if server.state in (LspServerState.STOPPED, LspServerState.ERROR):
            try:
                await server.start()
            except Exception as e:
                logger.error(
                    f"Failed to start LSP server for file {file_path}: {e}"
                )
                raise

        return server

    # ── 请求路由 ────────────────────────────────────────────────────────

    async def send_request(
        self, file_path: str, method: str, params: Any = None
    ) -> Any:
        """向负责该文件的服务器发送 LSP 请求（对应 CC sendRequest）。

        Returns:
            服务器的响应结果，或 None（如果没有可用的服务器）。
        """
        server = await self.ensure_server_started(file_path)
        if server is None:
            return None

        try:
            return await server.send_request(method, params)
        except Exception as e:
            logger.error(
                f"LSP request failed for {file_path}, "
                f"method '{method}': {e}"
            )
            raise

    # ── 文档生命周期同步 ─────────────────────────────────────────────────

    def is_file_open(self, file_path: str) -> bool:
        """检查文件是否已在 LSP 服务器上打开（对应 CC isFileOpen）。"""
        file_uri = self._path_to_uri(file_path)
        return file_uri in self._opened_files

    async def open_file(self, file_path: str, content: str) -> None:
        """同步文件打开到 LSP 服务器（对应 CC openFile — didOpen 通知）。

        如果文件尚未打开，先启动服务器，读取内容，
        然后发送 didOpen 通知。
        """
        server = await self.ensure_server_started(file_path)
        if server is None:
            return

        file_uri = self._path_to_uri(file_path)

        # 如果已在此服务器上打开，跳过
        if self._opened_files.get(file_uri) == server.config.name:
            logger.debug(f"LSP: File already open, skipping didOpen for {file_path}")
            return

        # 从 extension_to_language 获取 languageId
        ext = Path(file_path).suffix.lower()
        language_id = server.config.extension_to_language.get(ext, "plaintext")

        try:
            await server.send_notification("textDocument/didOpen", {
                "textDocument": {
                    "uri": file_uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": content,
                },
            })
            self._opened_files[file_uri] = server.config.name
            logger.debug(
                f"LSP: Sent didOpen for {file_path} (languageId: {language_id})"
            )
        except Exception as e:
            error_msg = f"Failed to sync file open {file_path}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    async def change_file(self, file_path: str, content: str) -> None:
        """同步文件变更到 LSP 服务器（对应 CC changeFile — didChange 通知）。"""
        server = self.get_server_for_file(file_path)
        if server is None or server.state != LspServerState.RUNNING:
            # 如果服务器未运行，回退到 openFile
            return await self.open_file(file_path, content)

        file_uri = self._path_to_uri(file_path)

        # 如果文件未在此服务器上打开，先打开
        if self._opened_files.get(file_uri) != server.config.name:
            return await self.open_file(file_path, content)

        try:
            await server.send_notification("textDocument/didChange", {
                "textDocument": {
                    "uri": file_uri,
                    "version": 1,
                },
                "contentChanges": [{"text": content}],
            })
            logger.debug(f"LSP: Sent didChange for {file_path}")
        except Exception as e:
            error_msg = f"Failed to sync file change {file_path}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    async def save_file(self, file_path: str) -> None:
        """同步文件保存到 LSP 服务器（对应 CC saveFile — didSave 通知）。"""
        server = self.get_server_for_file(file_path)
        if server is None or server.state != LspServerState.RUNNING:
            return

        file_uri = self._path_to_uri(file_path)

        try:
            await server.send_notification("textDocument/didSave", {
                "textDocument": {"uri": file_uri},
            })
            logger.debug(f"LSP: Sent didSave for {file_path}")
        except Exception as e:
            error_msg = f"Failed to sync file save {file_path}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    async def close_file(self, file_path: str) -> None:
        """同步文件关闭到 LSP 服务器（对应 CC closeFile — didClose 通知）。"""
        server = self.get_server_for_file(file_path)
        if server is None or server.state != LspServerState.RUNNING:
            return

        file_uri = self._path_to_uri(file_path)

        try:
            await server.send_notification("textDocument/didClose", {
                "textDocument": {"uri": file_uri},
            })
            self._opened_files.pop(file_uri, None)
            logger.debug(f"LSP: Sent didClose for {file_path}")
        except Exception as e:
            error_msg = f"Failed to sync file close {file_path}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    # ── 路径工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _path_to_uri(file_path: str) -> str:
        """文件路径 → file:// URI。"""
        return Path(file_path).resolve().as_uri()

    @staticmethod
    def _uri_to_path(uri: str) -> str:
        """file:// URI → 文件路径。"""
        import urllib.parse
        parsed = urllib.parse.urlparse(uri)
        path = urllib.parse.unquote(parsed.path)
        if os.name == "nt" and path.startswith("/"):
            path = path[1:]
        return path
