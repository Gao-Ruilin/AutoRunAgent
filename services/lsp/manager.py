"""
全局 LSP 管理器单例。

对应 src/services/lsp/manager.ts — 提供模块级别的:
- initializeLspServerManager(): 异步初始化
- reinitializeLspServerManager(): 重新初始化（刷新配置）
- shutdownLspServerManager(): 关闭
- getLspServerManager(): 获取单例
- getInitializationStatus(): 初始化状态
- isLspConnected(): 检查连接状态
- waitForInitialization(): 等待初始化完成
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from AutoRUN_v1.services.lsp.server_manager import LspServerManager
from AutoRUN_v1.services.lsp.server_instance import LspServerConfig, LspServerState

logger = logging.getLogger(__name__)

# ── 模块级单例状态（对应 CC manager.ts 的模块变量）──────────────────────

_lsp_manager: Optional[LspServerManager] = None
_initialization_state: str = "not-started"  # 'not-started' | 'pending' | 'success' | 'failed'
_initialization_error: Optional[Exception] = None
_initialization_generation: int = 0
_initialization_promise: Optional[asyncio.Task] = None


# ── 重置（仅供测试）─────────────────────────────────────────────────────

def _reset_lsp_manager_for_testing() -> None:
    """重置 LSP 管理器状态（仅供测试，对应 CC _resetLspManagerForTesting）。"""
    global _initialization_state, _initialization_error, \
           _initialization_promise, _initialization_generation
    _initialization_state = "not-started"
    _initialization_error = None
    _initialization_promise = None
    _initialization_generation += 1


# ── 公共 API ────────────────────────────────────────────────────────────

def get_lsp_server_manager() -> Optional[LspServerManager]:
    """获取全局 LSP 管理器单例（对应 CC getLspServerManager）。

    如果初始化失败，返回 None。
    """
    if _initialization_state == "failed":
        return None
    return _lsp_manager


def get_initialization_status() -> Dict[str, Any]:
    """获取初始化状态（对应 CC getInitializationStatus）。"""
    if _initialization_state == "failed":
        return {
            "status": "failed",
            "error": _initialization_error or RuntimeError("Initialization failed"),
        }
    if _initialization_state == "not-started":
        return {"status": "not-started"}
    if _initialization_state == "pending":
        return {"status": "pending"}
    return {"status": "success"}


def is_lsp_connected() -> bool:
    """是否有至少一个 LSP 服务器处于非错误状态（对应 CC isLspConnected）。"""
    if _initialization_state == "failed":
        return False
    manager = get_lsp_server_manager()
    if manager is None:
        return False
    servers = manager.get_all_servers()
    if len(servers) == 0:
        return False
    for server in servers.values():
        if server.state != LspServerState.ERROR:
            return True
    return False


async def wait_for_initialization() -> None:
    """等待 LSP 初始化完成（对应 CC waitForInitialization）。

    如果已成功或失败，立即返回。
    如果正在进行中，等待完成。
    如果未启动，立即返回。
    """
    if _initialization_state in ("success", "failed"):
        return
    if _initialization_state == "pending" and _initialization_promise is not None:
        try:
            await _initialization_promise
        except Exception:
            pass  # 初始化失败不影响调用者


def initialize_lsp_server_manager(
    workspace_root: Optional[str] = None,
    extra_servers: Optional[List[LspServerConfig]] = None,
) -> Optional[LspServerManager]:
    """初始化全局 LSP 管理器（对应 CC initializeLspServerManager）。

    异步初始化，不阻塞调用者。成功后会注册被动诊断通知处理器。

    Args:
        workspace_root: 工作区根目录，默认 CWD。
        extra_servers: 用户自定义的额外服务器配置。

    Returns:
        如果跳过初始化，返回 None；否则返回管理器实例。
    """
    global _lsp_manager, _initialization_state, _initialization_error, \
           _initialization_generation, _initialization_promise

    # bare mode 跳过
    from AutoRUN_v1.utils.env_utils import is_env_truthy
    if is_env_truthy(os.environ.get("AUTORUN_SIMPLE")):
        logger.debug("[LSP MANAGER] Skipping LSP in bare/simple mode")
        return None

    if workspace_root is None:
        workspace_root = os.getcwd()

    # 如果已初始化或正在初始化，跳过
    if _lsp_manager is not None and _initialization_state != "failed":
        logger.debug("[LSP MANAGER] Already initialized or initializing, skipping")
        return _lsp_manager

    # 如果之前失败了，重置状态以便重试
    if _initialization_state == "failed":
        _lsp_manager = None
        _initialization_error = None

    # 创建管理器并标记为 pending
    _lsp_manager = LspServerManager(workspace_root)
    _initialization_state = "pending"
    logger.debug("[LSP MANAGER] Created manager instance, state=pending")

    # 注册内置 + 用户自定义服务器
    _register_default_servers(_lsp_manager)
    if extra_servers:
        _lsp_manager.register_servers(extra_servers)

    # 递增 generation 以废弃任何正在进行的初始化
    _initialization_generation += 1
    current_generation = _initialization_generation

    logger.debug(
        f"[LSP MANAGER] Starting async initialization (gen {current_generation})"
    )

    # 异步初始化
    async def _do_initialize() -> None:
        global _initialization_state, _initialization_error, _lsp_manager

        try:
            await _lsp_manager_async.initialize()

            # 只在仍是当前 generation 时更新状态
            if current_generation == _initialization_generation:
                _initialization_state = "success"
                logger.info("LSP server manager initialized successfully")

                # 注册被动诊断通知处理器
                if _lsp_manager_async is not None:
                    _register_passive_handlers(_lsp_manager_async)

        except Exception as e:
            if current_generation == _initialization_generation:
                _initialization_state = "failed"
                _initialization_error = e
                _lsp_manager = None
                logger.error(f"Failed to initialize LSP server manager: {e}")

    _lsp_manager_async = _lsp_manager
    _initialization_promise = asyncio.ensure_future(_do_initialize())

    return _lsp_manager


def reinitialize_lsp_server_manager(
    extra_servers: Optional[List[LspServerConfig]] = None,
) -> None:
    """重新初始化 LSP 管理器（对应 CC reinitializeLspServerManager）。

    用于 /reload-plugins 等场景，刷新服务器配置。
    """
    global _lsp_manager, _initialization_state, _initialization_error

    if _initialization_state == "not-started":
        return  # 从未启动过，不主动初始化

    logger.debug("[LSP MANAGER] reinitializeLspServerManager() called")

    # 尽力关闭旧的服务器实例（fire-and-forget）
    if _lsp_manager is not None:
        async def _shutdown_old():
            try:
                await _lsp_manager.shutdown()
            except Exception as e:
                logger.debug(f"[LSP MANAGER] old instance shutdown failed: {e}")

        asyncio.ensure_future(_shutdown_old())

    # 重置状态，让 initialize 能够重新执行
    _lsp_manager = None
    _initialization_state = "not-started"
    _initialization_error = None

    initialize_lsp_server_manager(extra_servers=extra_servers)


async def shutdown_lsp_server_manager() -> None:
    """关闭全局 LSP 管理器（对应 CC shutdownLspServerManager）。"""
    global _lsp_manager, _initialization_state, _initialization_error, \
           _initialization_promise, _initialization_generation

    if _lsp_manager is None:
        return

    try:
        await _lsp_manager.shutdown()
        logger.info("LSP server manager shut down successfully")
    except Exception as e:
        logger.error(f"Failed to shutdown LSP server manager: {e}")
    finally:
        _lsp_manager = None
        _initialization_state = "not-started"
        _initialization_error = None
        _initialization_promise = None
        _initialization_generation += 1


# ── 内部工具函数 ────────────────────────────────────────────────────────

def _register_default_servers(manager: LspServerManager) -> None:
    """注册内置的常用语言服务器配置。

    对应 CC 通过插件系统加载的服务器配置。
    这里作为内置默认值，同时可以被 extra_servers 覆盖。
    """
    configs: List[LspServerConfig] = []

    # Python — jedi-language-server (pip install jedi-language-server)
    configs.append(LspServerConfig(
        name="python",
        command="jedi-language-server",
        extension_to_language={".py": "python"},
        initialization_options={
            "diagnostics": {"enable": True},
        },
    ))

    # TypeScript/JavaScript — typescript-language-server
    configs.append(LspServerConfig(
        name="typescript",
        command="typescript-language-server",
        args=["--stdio"],
        extension_to_language={
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
        },
    ))

    # Rust — rust-analyzer
    configs.append(LspServerConfig(
        name="rust",
        command="rust-analyzer",
        extension_to_language={".rs": "rust"},
    ))

    # Go — gopls
    configs.append(LspServerConfig(
        name="go",
        command="gopls",
        extension_to_language={".go": "go"},
    ))

    # Python 备用 — pyright
    configs.append(LspServerConfig(
        name="pyright",
        command="pyright-langserver",
        args=["--stdio"],
        extension_to_language={".py": "python"},
    ))

    # C/C++ — clangd
    configs.append(LspServerConfig(
        name="clangd",
        command="clangd",
        extension_to_language={
            ".c": "c", ".h": "c",
            ".cpp": "cpp", ".hpp": "cpp",
            ".cc": "cpp", ".cxx": "cpp",
        },
    ))

    # JSON — vscode-json-languageserver
    configs.append(LspServerConfig(
        name="json",
        command="vscode-json-languageserver",
        args=["--stdio"],
        extension_to_language={".json": "json"},
    ))

    # Lua — lua-language-server
    configs.append(LspServerConfig(
        name="lua",
        command="lua-language-server",
        extension_to_language={".lua": "lua"},
    ))

    # Ruby — solargraph
    configs.append(LspServerConfig(
        name="ruby",
        command="solargraph",
        args=["stdio"],
        extension_to_language={".rb": "ruby"},
    ))

    manager.register_servers(configs)


def _register_passive_handlers(manager: LspServerManager) -> None:
    """注册被动诊断通知处理器（对应 CC registerLSPNotificationHandlers）。"""
    from AutoRUN_v1.services.lsp.passive_feedback import \
        register_lsp_notification_handlers
    try:
        register_lsp_notification_handlers(manager)
    except Exception as e:
        logger.debug(f"Failed to register passive LSP handlers: {e}")
