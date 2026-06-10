"""
LSP 诊断信息注册表。

对应 src/services/lsp/LSPDiagnosticRegistry.ts — 收集 textDocument/publishDiagnostics
通知中的诊断信息，进行去重、数量限制和跨轮次跟踪。
"""

import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

# 限制常量（对应 CC 中的同名常量）
MAX_DIAGNOSTICS_PER_FILE = 10
MAX_TOTAL_DIAGNOSTICS = 30
MAX_DELIVERED_FILES = 500


# ── 类型 ────────────────────────────────────────────────────────────────

class PendingLspDiagnostic:
    """待发送的诊断批次（对应 CC PendingLSPDiagnostic）。"""

    def __init__(self, server_name: str, files: List[Dict[str, Any]]):
        self.server_name = server_name
        self.files = files
        self.timestamp: float = 0.0  # 可由外部设置
        self.attachment_sent: bool = False


# ── 全局注册表状态 ──────────────────────────────────────────────────────

# 待发送诊断
_pending_diagnostics: Dict[str, PendingLspDiagnostic] = {}

# 已发送诊断（跨轮次去重，LRU 淘汰）
# file_uri → Set[diagnostic_key]
_delivered_diagnostics: OrderedDict[str, Set[str]] = OrderedDict()


# ── 工具函数 ────────────────────────────────────────────────────────────

def _severity_to_number(severity: Optional[str]) -> int:
    """将 CC 的 severity 字符串映射为数字（对应 CC severityToNumber）。"""
    if severity == "Error":
        return 1
    elif severity == "Warning":
        return 2
    elif severity == "Info":
        return 3
    elif severity == "Hint":
        return 4
    return 4  # 默认最低优先级


def _create_diagnostic_key(diag: Dict[str, Any]) -> str:
    """生成唯一诊断键用于去重（对应 CC createDiagnosticKey）。

    键基于: message + severity + range + source + code
    """
    return json.dumps({
        "message": diag.get("message", ""),
        "severity": diag.get("severity", ""),
        "range": diag.get("range", {}),
        "source": diag.get("source") or None,
        "code": diag.get("code") or None,
    }, sort_keys=True, ensure_ascii=False)


def _uri_to_path(uri: str) -> str:
    """将 file:// URI 转换为文件系统路径。"""
    import urllib.parse
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    import os
    if os.name == "nt" and path.startswith("/"):
        path = path[1:]
    return path


# ── 公共 API ────────────────────────────────────────────────────────────

def register_pending_lsp_diagnostic(
    server_name: str,
    files: List[Dict[str, Any]],
) -> None:
    """注册待发送的 LSP 诊断信息（对应 CC registerPendingLSPDiagnostic）。

    Args:
        server_name: 发送诊断的服务器名称。
        files: DiagnosticFile 列表，每个包含 'uri' 和 'diagnostics' 字段。
    """
    diagnostic_id = str(uuid4())

    logger.debug(
        f"LSP Diagnostics: Registering {len(files)} diagnostic file(s) "
        f"from {server_name} (ID: {diagnostic_id})"
    )

    _pending_diagnostics[diagnostic_id] = PendingLspDiagnostic(
        server_name=server_name,
        files=files,
    )


def check_for_lsp_diagnostics() -> List[Dict[str, Any]]:
    """检查并返回去重、限量后的诊断（对应 CC checkForLSPDiagnostics）。

    Returns:
        [{"serverName": str, "files": [{"uri": str, "diagnostics": [...]}]}]
    """
    logger.debug(
        f"LSP Diagnostics: Checking registry - {len(_pending_diagnostics)} pending"
    )

    if not _pending_diagnostics:
        return []

    # 收集所有待发送的诊断
    all_files: List[Dict[str, Any]] = []
    server_names: Set[str] = set()
    to_mark: List[PendingLspDiagnostic] = []

    for diag_id, diag in list(_pending_diagnostics.items()):
        if not diag.attachment_sent:
            all_files.extend(diag.files)
            server_names.add(diag.server_name)
            to_mark.append(diag)

    if not all_files:
        return []

    # 去重
    deduped_files = _deduplicate_diagnostic_files(all_files)

    # 标记已发送并清除
    for diag in to_mark:
        diag.attachment_sent = True
    for diag_id, diag in list(_pending_diagnostics.items()):
        if diag.attachment_sent:
            del _pending_diagnostics[diag_id]

    original_count = sum(len(f.get("diagnostics", [])) for f in all_files)
    deduped_count = sum(len(f.get("diagnostics", [])) for f in deduped_files)

    if original_count > deduped_count:
        logger.debug(
            f"LSP Diagnostics: Deduplication removed "
            f"{original_count - deduped_count} duplicate diagnostic(s)"
        )

    # 数量限制: 按严重性排序（error 优先），然后截断
    total_diagnostics = 0
    truncated_count = 0

    for file in deduped_files:
        diags = file.get("diagnostics", [])
        if not diags:
            continue

        # 按严重性排序
        diags.sort(key=lambda d: _severity_to_number(d.get("severity", "Hint")))

        # 每个文件最多 MAX_DIAGNOSTICS_PER_FILE 条
        if len(diags) > MAX_DIAGNOSTICS_PER_FILE:
            truncated_count += len(diags) - MAX_DIAGNOSTICS_PER_FILE
            diags = diags[:MAX_DIAGNOSTICS_PER_FILE]
            file["diagnostics"] = diags

        # 总数上限
        remaining = MAX_TOTAL_DIAGNOSTICS - total_diagnostics
        if len(diags) > remaining:
            truncated_count += len(diags) - remaining
            file["diagnostics"] = diags[:remaining]

        total_diagnostics += len(file.get("diagnostics", []))

    # 过滤掉被清空的文件
    deduped_files = [f for f in deduped_files if len(f.get("diagnostics", [])) > 0]

    if truncated_count > 0:
        logger.debug(
            f"LSP Diagnostics: Volume limiting removed {truncated_count} "
            f"diagnostic(s) (max {MAX_DIAGNOSTICS_PER_FILE}/file, "
            f"{MAX_TOTAL_DIAGNOSTICS} total)"
        )

    # 记录已发送诊断（用于跨轮次去重）
    for file in deduped_files:
        uri = file.get("uri", "")
        if uri not in _delivered_diagnostics:
            _delivered_diagnostics[uri] = set()
            # LRU 淘汰
            if len(_delivered_diagnostics) > MAX_DELIVERED_FILES:
                _delivered_diagnostics.popitem(last=False)

        delivered_set = _delivered_diagnostics[uri]
        for diag in file.get("diagnostics", []):
            try:
                delivered_set.add(_create_diagnostic_key(diag))
            except Exception:
                pass  # 记录失败不影响诊断交付

    final_count = sum(len(f.get("diagnostics", [])) for f in deduped_files)

    if final_count == 0:
        logger.debug(
            "LSP Diagnostics: No new diagnostics to deliver "
            "(all filtered by deduplication)"
        )
        return []

    logger.debug(
        f"LSP Diagnostics: Delivering {len(deduped_files)} file(s) with "
        f"{final_count} diagnostic(s) from {len(server_names)} server(s)"
    )

    return [
        {
            "serverName": ", ".join(server_names),
            "files": deduped_files,
        }
    ]


def _deduplicate_diagnostic_files(
    all_files: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """去重诊断文件（对应 CC deduplicateDiagnosticFiles）。

    两阶段去重:
    1. 批内去重 — 同一批次内的重复
    2. 跨轮次去重 — 与之前已发送的诊断重复
    """
    file_map: Dict[str, Set[str]] = {}
    deduped: Dict[str, Dict[str, Any]] = {}

    for file in all_files:
        uri = file.get("uri", "")

        if uri not in file_map:
            file_map[uri] = set()
            deduped[uri] = {"uri": uri, "diagnostics": []}

        seen = file_map[uri]
        previously_delivered = _delivered_diagnostics.get(uri, set())

        for diag in file.get("diagnostics", []):
            try:
                key = _create_diagnostic_key(diag)
                if key in seen or key in previously_delivered:
                    continue
                seen.add(key)
                deduped[uri]["diagnostics"].append(diag)
            except Exception as e:
                # 去重失败时仍保留诊断，避免丢失信息
                logger.debug(f"Failed to deduplicate diagnostic: {e}")
                deduped[uri]["diagnostics"].append(diag)

    return [f for f in deduped.values() if len(f.get("diagnostics", [])) > 0]


def clear_delivered_diagnostics_for_file(file_uri: str) -> None:
    """当文件被编辑时清除其已发送诊断缓存（对应 CC clearDeliveredDiagnosticsForFile）。"""
    if file_uri in _delivered_diagnostics:
        logger.debug(f"LSP Diagnostics: Clearing delivered diagnostics for {file_uri}")
        del _delivered_diagnostics[file_uri]


def clear_all_lsp_diagnostics() -> None:
    """清除所有待发送诊断（对应 CC clearAllLSPDiagnostics）。"""
    count = len(_pending_diagnostics)
    logger.debug(f"LSP Diagnostics: Clearing {count} pending diagnostic(s)")
    _pending_diagnostics.clear()


def reset_all_lsp_diagnostic_state() -> None:
    """重置所有诊断状态（对应 CC resetAllLSPDiagnosticState）。"""
    logger.debug(
        f"LSP Diagnostics: Resetting all state "
        f"({len(_pending_diagnostics)} pending, "
        f"{len(_delivered_diagnostics)} files tracked)"
    )
    _pending_diagnostics.clear()
    _delivered_diagnostics.clear()


def get_pending_lsp_diagnostic_count() -> int:
    """获取待发送诊断数量（对应 CC getPendingLSPDiagnosticCount）。"""
    return len(_pending_diagnostics)
