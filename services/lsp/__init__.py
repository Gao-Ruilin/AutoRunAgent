"""
LSP (Language Server Protocol) 服务层。

对应 src/services/lsp/ — 提供语言服务器的发现、生命周期管理、
JSON-RPC 通信、诊断信息收集。

架构（对应 CC 源码）:
- client.py: 底层 LSP JSON-RPC 客户端（stdio，对应 LSPClient.ts）
- server_instance.py: 单服务器生命周期（对应 LSPServerInstance.ts）
- server_manager.py: 多服务器管理器（对应 LSPServerManager.ts）
- diagnostics.py: 诊断注册表（对应 LSPDiagnosticRegistry.ts）
- passive_feedback.py: 被动诊断通知处理器（对应 passiveFeedback.ts）
- manager.py: 全局单例入口（对应 manager.ts）
"""

from AutoRUN_v1.services.lsp.manager import (
    get_lsp_server_manager,
    get_initialization_status,
    initialize_lsp_server_manager,
    reinitialize_lsp_server_manager,
    shutdown_lsp_server_manager,
    is_lsp_connected,
    wait_for_initialization,
)

from AutoRUN_v1.services.lsp.server_manager import (
    LspServerManager,
)

from AutoRUN_v1.services.lsp.server_instance import (
    LspServerConfig,
    LspServerInstance,
    LspServerState,
    LspStartError,
)

from AutoRUN_v1.services.lsp.client import (
    LspClient,
    LspError,
)

from AutoRUN_v1.services.lsp.diagnostics import (
    register_pending_lsp_diagnostic,
    check_for_lsp_diagnostics,
    clear_all_lsp_diagnostics,
    clear_delivered_diagnostics_for_file,
    reset_all_lsp_diagnostic_state,
    get_pending_lsp_diagnostic_count,
)

from AutoRUN_v1.services.lsp.passive_feedback import (
    register_lsp_notification_handlers,
)

__all__ = [
    # Manager (global singleton — 对应 manager.ts)
    "get_lsp_server_manager",
    "get_initialization_status",
    "initialize_lsp_server_manager",
    "reinitialize_lsp_server_manager",
    "shutdown_lsp_server_manager",
    "is_lsp_connected",
    "wait_for_initialization",
    # Server manager (对应 LSPServerManager.ts)
    "LspServerManager",
    # Server instance (对应 LSPServerInstance.ts)
    "LspServerConfig",
    "LspServerInstance",
    "LspServerState",
    "LspStartError",
    # Client (对应 LSPClient.ts)
    "LspClient",
    "LspError",
    # Diagnostics (对应 LSPDiagnosticRegistry.ts)
    "register_pending_lsp_diagnostic",
    "check_for_lsp_diagnostics",
    "clear_all_lsp_diagnostics",
    "clear_delivered_diagnostics_for_file",
    "reset_all_lsp_diagnostic_state",
    "get_pending_lsp_diagnostic_count",
    # Passive feedback (对应 passiveFeedback.ts)
    "register_lsp_notification_handlers",
]
