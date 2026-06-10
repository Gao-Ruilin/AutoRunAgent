"""
被动诊断反馈处理器。

对应 src/services/lsp/passiveFeedback.ts — 在 LSP 服务器上注册
textDocument/publishDiagnostics 通知处理器，将诊断信息转换为
统一格式并路由到诊断注册表。
"""

import logging
from typing import Any, Dict, List, Optional

from AutoRUN_v1.services.lsp.server_manager import LspServerManager
from AutoRUN_v1.services.lsp.diagnostics import register_pending_lsp_diagnostic

logger = logging.getLogger(__name__)


def _map_lsp_severity(lsp_severity: Optional[int]) -> str:
    """将 LSP DiagnosticSeverity 枚举映射为 CC 风格字符串。

    对应 CC mapLSPSeverity:
    1=Error, 2=Warning, 3=Information, 4=Hint
    """
    if lsp_severity == 1:
        return "Error"
    elif lsp_severity == 2:
        return "Warning"
    elif lsp_severity == 3:
        return "Info"
    elif lsp_severity == 4:
        return "Hint"
    return "Error"  # 默认为 Error


def _format_diagnostics_for_attachment(
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """将 LSP publishDiagnostics 参数转换为 CC DiagnosticFile 格式。

    对应 CC formatDiagnosticsForAttachment:
    - URI: file:// URI 或纯路径 → 统一路径
    - diagnostics: LSP Diagnostic[] → CC Diagnostic[]
    """
    import os
    import urllib.parse

    # 解析 URI
    uri = params.get("uri", "")
    try:
        if uri.startswith("file://"):
            parsed = urllib.parse.urlparse(uri)
            path = urllib.parse.unquote(parsed.path)
            if os.name == "nt" and path.startswith("/"):
                path = path[1:]
            uri = path
    except Exception as e:
        logger.debug(f"Failed to convert URI to file path: {uri}: {e}")
        # 回退到原始 URI

    diagnostics = []
    for diag in params.get("diagnostics", []):
        diagnostics.append({
            "message": diag.get("message", ""),
            "severity": _map_lsp_severity(diag.get("severity")),
            "range": {
                "start": {
                    "line": diag.get("range", {}).get("start", {}).get("line", 0),
                    "character": diag.get("range", {}).get("start", {}).get("character", 0),
                },
                "end": {
                    "line": diag.get("range", {}).get("end", {}).get("line", 0),
                    "character": diag.get("range", {}).get("end", {}).get("character", 0),
                },
            },
            "source": diag.get("source"),
            "code": (
                str(diag["code"])
                if diag.get("code") is not None and diag.get("code") != ""
                else None
            ),
        })

    return [{"uri": uri, "diagnostics": diagnostics}]


def register_lsp_notification_handlers(
    manager: LspServerManager,
) -> Dict[str, Any]:
    """在所有 LSP 服务器上注册 publishDiagnostics 通知处理器。

    对应 CC registerLSPNotificationHandlers:
    1. 遍历所有服务器
    2. 在每个服务器上注册 textDocument/publishDiagnostics 处理器
    3. 处理 invalid params、格式转换、诊断注册
    4. 跟踪连续失败次数（3+ 次时警告）

    Returns:
        {
            "totalServers": int,
            "successCount": int,
            "registrationErrors": [{serverName, error}],
            "diagnosticFailures": {serverName: {count, lastError}},
        }
    """
    servers = manager.get_all_servers()
    registration_errors: List[Dict[str, str]] = []
    success_count = 0
    diagnostic_failures: Dict[str, Dict[str, Any]] = {}

    for server_name, server_instance in servers.items():
        try:
            # 验证 on_notification 方法可用
            if not hasattr(server_instance, "on_notification"):
                error_msg = "Server instance has no on_notification method"
                registration_errors.append({
                    "serverName": server_name,
                    "error": error_msg,
                })
                logger.error(f"{error_msg} for {server_name}")
                continue

            def _make_handler(srv_name: str):
                """创建闭包捕获 server_name 值。"""
                def _handler(params):
                    logger.debug(
                        f"[PASSIVE DIAGNOSTICS] Handler invoked for {srv_name}!"
                    )
                    try:
                        # 验证参数结构
                        if (
                            not params
                            or not isinstance(params, dict)
                            or "uri" not in params
                            or "diagnostics" not in params
                        ):
                            logger.error(
                                f"LSP server {srv_name} sent invalid diagnostic params"
                            )
                            return

                        diag_count = len(params.get("diagnostics", []))
                        logger.debug(
                            f"Received diagnostics from {srv_name}: "
                            f"{diag_count} diagnostic(s) for {params.get('uri')}"
                        )

                        # 转换格式
                        diagnostic_files = _format_diagnostics_for_attachment(params)

                        first_file = diagnostic_files[0] if diagnostic_files else None
                        if (
                            not first_file
                            or not first_file.get("diagnostics")
                        ):
                            logger.debug(
                                f"Skipping empty diagnostics from {srv_name}"
                            )
                            return

                        # 注册到诊断注册表
                        try:
                            register_pending_lsp_diagnostic(
                                server_name=srv_name,
                                files=diagnostic_files,
                            )
                            logger.debug(
                                f"LSP Diagnostics: Registered "
                                f"{len(diagnostic_files)} diagnostic file(s) "
                                f"from {srv_name}"
                            )
                            diagnostic_failures.pop(srv_name, None)
                        except Exception as e:
                            logger.error(
                                f"Error registering LSP diagnostics from {srv_name}: {e}"
                            )
                            failures = diagnostic_failures.get(srv_name, {
                                "count": 0, "lastError": ""
                            })
                            failures["count"] += 1
                            failures["lastError"] = str(e)
                            diagnostic_failures[srv_name] = failures

                            if failures["count"] >= 3:
                                logger.warning(
                                    f"WARNING: LSP diagnostic handler for {srv_name} "
                                    f"has failed {failures['count']} times consecutively. "
                                    f"Last error: {failures['lastError']}"
                                )
                    except Exception as e:
                        logger.exception(
                            f"Unexpected error processing diagnostics from {srv_name}"
                        )
                        failures = diagnostic_failures.get(srv_name, {
                            "count": 0, "lastError": ""
                        })
                        failures["count"] += 1
                        failures["lastError"] = str(e)
                        diagnostic_failures[srv_name] = failures

                        if failures["count"] >= 3:
                            logger.warning(
                                f"WARNING: LSP diagnostic handler for {srv_name} "
                                f"has failed {failures['count']} times consecutively."
                            )

                return _handler

            server_instance.on_notification(
                "textDocument/publishDiagnostics",
                _make_handler(server_name),
            )

            logger.debug(f"Registered diagnostics handler for {server_name}")
            success_count += 1

        except Exception as e:
            registration_errors.append({
                "serverName": server_name,
                "error": str(e),
            })
            logger.error(
                f"Failed to register diagnostics handler for {server_name}: {e}"
            )

    total_servers = len(servers)

    if registration_errors:
        failed_servers = ", ".join(
            f"{e['serverName']} ({e['error']})" for e in registration_errors
        )
        logger.error(
            f"Failed to register diagnostics for "
            f"{len(registration_errors)} LSP server(s): {failed_servers}"
        )
        logger.debug(
            f"LSP notification handler registration: "
            f"{success_count}/{total_servers} succeeded. "
            f"Failed servers: {failed_servers}"
        )
    else:
        logger.debug(
            f"LSP notification handlers registered successfully "
            f"for all {total_servers} server(s)"
        )

    return {
        "totalServers": total_servers,
        "successCount": success_count,
        "registrationErrors": registration_errors,
        "diagnosticFailures": diagnostic_failures,
    }
