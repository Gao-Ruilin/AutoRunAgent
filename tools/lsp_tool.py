"""
LSP 工具 — 让 AI 调用语言服务器进行代码智能操作。

对应 src/tools/LSPTool/LSPTool.ts — 支持 9 种 LSP 操作:
goToDefinition, findReferences, hover, documentSymbol,
workspaceSymbol, goToImplementation, prepareCallHierarchy,
incomingCalls, outgoingCalls

完全匹配 CC 的 LSPTool 实现:
- isEnabled() → is_lsp_connected()
- validateInput() → 检查文件存在性和合法性
- call() → 完整流程（等待初始化、打开文件、发送请求、过滤 git-ignored、格式化）
- 返回格式 → {operation, result, filePath, resultCount, fileCount}
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# 最大文件大小（10MB，对应 CC MAX_LSP_FILE_SIZE_BYTES）
MAX_LSP_FILE_SIZE_BYTES = 10_000_000

# git check-ignore 批处理大小和超时（对应 CC BATCH_SIZE=50, timeout=5s）
GIT_CHECK_IGNORE_BATCH_SIZE = 50
GIT_CHECK_IGNORE_TIMEOUT = 5

# 操作 → LSP 方法映射
_OPERATION_METHOD_MAP = {
    "goToDefinition": "textDocument/definition",
    "findReferences": "textDocument/references",
    "hover": "textDocument/hover",
    "documentSymbol": "textDocument/documentSymbol",
    "workspaceSymbol": "workspace/symbol",
    "goToImplementation": "textDocument/implementation",
    "prepareCallHierarchy": "textDocument/prepareCallHierarchy",
    "incomingCalls": "textDocument/prepareCallHierarchy",  # 二步操作，第一步
    "outgoingCalls": "textDocument/prepareCallHierarchy",  # 二步操作，第一步
}

# 需要过滤 git-ignored 文件的操作（对应 CC 中的判断条件）
_GIT_FILTER_OPERATIONS = {
    "findReferences", "goToDefinition", "goToImplementation", "workspaceSymbol",
}

LSP_TOOL_NAME = "LSP"

LSP_DESCRIPTION = """Interact with Language Server Protocol (LSP) servers to get code intelligence features.

Supported operations:
- goToDefinition: Find where a symbol is defined
- findReferences: Find all references to a symbol
- hover: Get hover information (documentation, type info) for a symbol
- documentSymbol: Get all symbols (functions, classes, variables) in a document
- workspaceSymbol: Search for symbols across the entire workspace
- goToImplementation: Find implementations of an interface or abstract method
- prepareCallHierarchy: Get call hierarchy item at a position (functions/methods)
- incomingCalls: Find all functions/methods that call the function at a position
- outgoingCalls: Find all functions/methods called by the function at a position

All operations require:
- filePath: The file to operate on
- line: The line number (1-based, as shown in editors)
- character: The character offset (1-based, as shown in editors)

Note: LSP servers must be configured for the file type. If no server is available, an error will be returned."""


class LSPTool(Tool):
    """LSP 代码智能工具（完全对应 CC LSPTool）。

    通过语言服务器协议提供代码智能查询功能。
    """

    @property
    def name(self) -> str:
        return LSP_TOOL_NAME

    @property
    def description(self) -> str:
        return LSP_DESCRIPTION

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "The LSP operation to perform",
                    "enum": list(_OPERATION_METHOD_MAP.keys()),
                },
                "filePath": {
                    "type": "string",
                    "description": "The absolute or relative path to the file",
                },
                "line": {
                    "type": "integer",
                    "description": "The line number (1-based, as shown in editors)",
                    "minimum": 1,
                },
                "character": {
                    "type": "integer",
                    "description": "The character offset (1-based, as shown in editors)",
                    "minimum": 1,
                },
            },
            "required": ["operation", "filePath", "line", "character"],
        }

    @property
    def search_hint(self) -> Optional[str]:
        return "code intelligence (definitions, references, symbols, hover, call hierarchy)"

    def is_enabled(self) -> bool:
        """检查是否有可用的 LSP 服务器（对应 CC isEnabled() → isLspConnected()）。"""
        try:
            from AutoRUN_v1.services.lsp.manager import is_lsp_connected
            return is_lsp_connected()
        except Exception:
            return False

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    def is_concurrency_safe(self, args: Dict[str, Any]) -> bool:
        return True

    def is_destructive(self, args: Dict[str, Any]) -> bool:
        return False

    async def validate_input(
        self, args: Dict[str, Any], context: ToolContext
    ) -> Dict[str, Any]:
        """验证输入参数（对应 CC validateInput）。

        错误码（对应 CC）:
        1 = 文件不存在
        2 = 路径不是普通文件
        3 = schema 验证失败
        4 = 无法访问文件
        """
        operation = args.get("operation", "")
        file_path = args.get("filePath", "")

        if operation not in _OPERATION_METHOD_MAP:
            return {
                "result": False,
                "message": f"Invalid operation: '{operation}'",
                "errorCode": 3,
            }

        # 解析路径
        absolute_path = self._resolve_path(file_path, context)
        if absolute_path is None:
            return {
                "result": False,
                "message": f"File does not exist: {file_path}",
                "errorCode": 1,
            }

        # UNC 路径防护 — 跳过文件系统操作
        if absolute_path.startswith("\\\\") or absolute_path.startswith("//"):
            return {"result": True}

        # 检查文件状态
        try:
            p = Path(absolute_path)
            if not p.is_file():
                return {
                    "result": False,
                    "message": f"Path is not a file: {file_path}",
                    "errorCode": 2,
                }
        except (OSError, PermissionError) as e:
            logger.error(
                f"Failed to access file stats for LSP operation "
                f"on {file_path}: {e}"
            )
            return {
                "result": False,
                "message": f"Cannot access file: {file_path}. {e}",
                "errorCode": 4,
            }

        return {"result": True}

    async def call(
        self, args: Dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """执行 LSP 操作（完全对应 CC call()）。"""
        operation = args.get("operation", "")
        file_path = args.get("filePath", "")

        absolute_path = self._resolve_path(file_path, context)
        if absolute_path is None:
            return ToolResult(data=json.dumps({
                "operation": operation,
                "result": f"File does not exist: {file_path}",
                "filePath": file_path,
            }))

        cwd = context.cwd or os.getcwd()

        # 步骤 1: 等待 LSP 初始化完成（如果正在进行中）
        try:
            from AutoRUN_v1.services.lsp.manager import (
                get_initialization_status,
                wait_for_initialization,
                get_lsp_server_manager,
            )

            status = get_initialization_status()
            if status["status"] == "pending":
                await wait_for_initialization()

            # 步骤 2: 获取管理器
            manager = get_lsp_server_manager()
            if manager is None:
                logger.error(
                    "LSP server manager not initialized when tool was called"
                )
                return ToolResult(data=json.dumps({
                    "operation": operation,
                    "result": (
                        "LSP server manager not initialized. "
                        "This may indicate a startup issue."
                    ),
                    "filePath": file_path,
                }))
        except ImportError:
            return ToolResult(data=json.dumps({
                "operation": operation,
                "result": "LSP service not available.",
                "filePath": file_path,
            }))

        try:
            # 步骤 3: 映射操作到 LSP 方法和参数
            method, params = self._get_method_and_params(
                operation, absolute_path, args
            )

            # 步骤 4: 自动打开文件（如果尚未在服务器上打开）
            if not manager.is_file_open(absolute_path):
                try:
                    p = Path(absolute_path)
                    file_size = p.stat().st_size
                    if file_size > MAX_LSP_FILE_SIZE_BYTES:
                        size_mb = file_size / 1_000_000
                        return ToolResult(data=json.dumps({
                            "operation": operation,
                            "result": (
                                f"File too large for LSP analysis "
                                f"({size_mb:.0f}MB exceeds 10MB limit)"
                            ),
                            "filePath": file_path,
                        }))
                    content = p.read_text(encoding="utf-8", errors="replace")
                    await manager.open_file(absolute_path, content)
                except (OSError, IOError) as e:
                    return ToolResult(data=json.dumps({
                        "operation": operation,
                        "result": f"Cannot read file: {e}",
                        "filePath": file_path,
                    }))

            # 步骤 5: 发送 LSP 请求
            result = await manager.send_request(
                absolute_path, method, params
            )

            # 步骤 6: 处理 undefined 结果（没有可用服务器）
            if result is None:
                ext = Path(absolute_path).suffix
                logger.debug(
                    f"No LSP server available for file type {ext} "
                    f"for operation {operation} on file {file_path}"
                )
                return ToolResult(data=json.dumps({
                    "operation": operation,
                    "result": (
                        f"No LSP server available for file type: {ext}"
                    ),
                    "filePath": file_path,
                }))

            # 步骤 7: 二步调用层次解析（incomingCalls / outgoingCalls）
            if operation in ("incomingCalls", "outgoingCalls"):
                call_items = result if isinstance(result, list) else []
                if not call_items:
                    return ToolResult(data=json.dumps({
                        "operation": operation,
                        "result": "No call hierarchy item found at this position",
                        "filePath": file_path,
                        "resultCount": 0,
                        "fileCount": 0,
                    }))

                call_method = (
                    "callHierarchy/incomingCalls"
                    if operation == "incomingCalls"
                    else "callHierarchy/outgoingCalls"
                )

                result = await manager.send_request(
                    absolute_path, call_method,
                    {"item": call_items[0]},  # 使用第一个 CallHierarchyItem
                )

                if result is None:
                    logger.debug(
                        f"LSP server returned undefined for {call_method} "
                        f"on {file_path}"
                    )

            # 步骤 8: 过滤 git-ignored 文件中的位置
            if (
                result
                and isinstance(result, list)
                and operation in _GIT_FILTER_OPERATIONS
            ):
                result = await self._filter_git_ignored_locations(
                    result, operation, cwd
                )

            # 步骤 9: 格式化结果
            formatted, result_count, file_count = self._format_result(
                operation, result, cwd
            )

            return ToolResult(data=json.dumps({
                "operation": operation,
                "result": formatted,
                "filePath": file_path,
                "resultCount": result_count,
                "fileCount": file_count,
            }))

        except Exception as e:
            logger.error(
                f"LSP tool request failed for {operation} "
                f"on {file_path}: {e}"
            )
            return ToolResult(data=json.dumps({
                "operation": operation,
                "result": f"Error performing {operation}: {e}",
                "filePath": file_path,
            }))

    # ── 路径解析 ────────────────────────────────────────────────────────

    def _resolve_path(self, file_path: str, context: ToolContext) -> Optional[str]:
        """解析文件路径为绝对路径。"""
        p = Path(file_path)
        if p.is_absolute():
            return str(p) if p.exists() else None

        cwd = context.cwd or os.getcwd()
        resolved = Path(cwd) / file_path
        return str(resolved) if resolved.exists() else None

    # ── 方法映射（对应 CC getMethodAndParams）───────────────────────────

    def _get_method_and_params(
        self,
        operation: str,
        absolute_path: str,
        args: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        """将操作映射到 LSP 方法和参数（对应 CC getMethodAndParams）。

        将 1-based 行/列转换为 0-based（LSP 协议要求）。
        """
        uri = Path(absolute_path).resolve().as_uri()
        line = args.get("line", 1)
        character = args.get("character", 1)
        position = {
            "line": line - 1,  # 1-based → 0-based
            "character": character - 1,
        }

        method = _OPERATION_METHOD_MAP[operation]

        if operation == "goToDefinition":
            params = {"textDocument": {"uri": uri}, "position": position}
        elif operation == "findReferences":
            params = {
                "textDocument": {"uri": uri},
                "position": position,
                "context": {"includeDeclaration": True},
            }
        elif operation == "hover":
            params = {"textDocument": {"uri": uri}, "position": position}
        elif operation == "documentSymbol":
            params = {"textDocument": {"uri": uri}}
        elif operation == "workspaceSymbol":
            params = {"query": ""}  # 空 query 返回所有符号
        elif operation == "goToImplementation":
            params = {"textDocument": {"uri": uri}, "position": position}
        elif operation == "prepareCallHierarchy":
            params = {"textDocument": {"uri": uri}, "position": position}
        elif operation in ("incomingCalls", "outgoingCalls"):
            # 第一步: prepareCallHierarchy
            params = {"textDocument": {"uri": uri}, "position": position}
        else:
            params = {"textDocument": {"uri": uri}, "position": position}

        return method, params

    # ── Git-ignored 过滤（对应 CC filterGitIgnoredLocations）─────────────

    async def _filter_git_ignored_locations(
        self,
        result: List[Any],
        operation: str,
        cwd: str,
    ) -> List[Any]:
        """过滤 git-ignored 文件中的位置（对应 CC filterGitIgnoredLocations）。

        仅对 location-based 操作（findReferences, goToDefinition,
        goToImplementation, workspaceSymbol）进行过滤。
        使用 git check-ignore 批量检查（每批 50 个，5 秒超时）。
        """
        if not result:
            return result

        # 提取所有 URI → 文件路径映射
        uri_to_path: Dict[str, str] = {}
        for item in result:
            location = item
            if operation == "workspaceSymbol" and isinstance(item, dict):
                location = item.get("location", {})
            uri = location.get("uri", "")
            if uri and uri not in uri_to_path:
                uri_to_path[uri] = self._uri_to_file_path(uri)

        unique_paths = list(set(uri_to_path.values()))
        if not unique_paths:
            return result

        # 批量 git check-ignore
        ignored_paths: set[str] = set()
        for i in range(0, len(unique_paths), GIT_CHECK_IGNORE_BATCH_SIZE):
            batch = unique_paths[i:i + GIT_CHECK_IGNORE_BATCH_SIZE]
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "check-ignore", *batch,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                try:
                    stdout, _ = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=GIT_CHECK_IGNORE_TIMEOUT,
                    )
                    # 退出码 0 表示至少一个路径被忽略
                    if proc.returncode == 0 and stdout:
                        for line in stdout.decode("utf-8").split("\n"):
                            trimmed = line.strip()
                            if trimmed:
                                ignored_paths.add(trimmed)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except FileNotFoundError:
                # git 未安装 — 不过滤
                return result
            except Exception:
                continue

        if not ignored_paths:
            return result

        # 过滤: 仅保留未被忽略的项
        if operation == "workspaceSymbol":
            return [
                s for s in result
                if not (
                    s.get("location", {}).get("uri")
                    and uri_to_path.get(s["location"]["uri"]) in ignored_paths
                )
            ]
        else:
            return [
                item for item in result
                if not (
                    self._to_location(item).get("uri")
                    and uri_to_path.get(
                        self._to_location(item)["uri"]
                    ) in ignored_paths
                )
            ]

    @staticmethod
    def _uri_to_file_path(uri: str) -> str:
        """file:// URI → 文件系统路径。"""
        import urllib.parse
        parsed = urllib.parse.urlparse(uri)
        path = urllib.parse.unquote(parsed.path)
        if os.name == "nt" and path.startswith("/"):
            path = path[1:]
        return path

    @staticmethod
    def _to_location(item: Any) -> Dict[str, Any]:
        """从 Location 或 LocationLink 中提取 Location。"""
        if isinstance(item, dict):
            # LocationLink 格式: {targetUri, targetRange, ...}
            if "targetUri" in item:
                return {
                    "uri": item.get("targetUri", ""),
                    "range": item.get("targetRange", {}),
                }
            # 已有 uri 字段
            if "uri" in item:
                return item
        return {}

    # ── 结果格式化（对应 CC formatters.ts）──────────────────────────────

    def _format_result(
        self, operation: str, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 LSP 结果（对应 CC formatResult）。

        Returns:
            (formatted_text, result_count, file_count)
        """
        if result is None:
            return f"[{operation}] No results.", 0, 0

        formatters = {
            "goToDefinition": self._fmt_go_to_definition,
            "findReferences": self._fmt_find_references,
            "hover": self._fmt_hover,
            "documentSymbol": self._fmt_document_symbol,
            "workspaceSymbol": self._fmt_workspace_symbol,
            "goToImplementation": self._fmt_go_to_implementation,
            "prepareCallHierarchy": self._fmt_prepare_call_hierarchy,
            "incomingCalls": self._fmt_incoming_calls,
            "outgoingCalls": self._fmt_outgoing_calls,
        }

        formatter = formatters.get(operation, self._fmt_default)
        return formatter(result, cwd)

    def _format_uri(self, uri: str, cwd: str) -> str:
        """格式化 URI 为相对路径（对应 CC formatUri）。"""
        file_path = self._uri_to_file_path(uri)
        try:
            return str(Path(file_path).relative_to(cwd))
        except ValueError:
            return file_path

    def _fmt_location(self, loc: Dict[str, Any], cwd: str) -> str:
        """格式化单个位置（对应 CC formatLocation）。"""
        uri = loc.get("uri", "")
        rng = loc.get("range", {})
        start = rng.get("start", {})
        path_str = self._format_uri(uri, cwd) if uri else "?"
        return (
            f"{path_str}:"
            f"{start.get('line', 0) + 1}:"
            f"{start.get('character', 0) + 1}"
        )

    @staticmethod
    def _symbol_kind_name(kind: int) -> str:
        """LSP SymbolKind → 可读名称（对应 CC symbolKindToString）。"""
        names = {
            1: "File", 2: "Module", 3: "Namespace", 4: "Package",
            5: "Class", 6: "Method", 7: "Property", 8: "Field",
            9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
            13: "Variable", 14: "Constant", 15: "String", 16: "Number",
            17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
            21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
            25: "Operator", 26: "TypeParameter",
        }
        return names.get(kind, f"Symbol({kind})")

    # ── 各操作的格式化器 ────────────────────────────────────────────────

    def _fmt_go_to_definition(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 goToDefinition 结果（对应 CC formatGoToDefinitionResult）。"""
        # 处理 LocationLink
        if isinstance(result, dict) and "targetUri" in result and "targetRange" in result:
            loc = {"uri": result["targetUri"], "range": result["targetRange"]}
            return f"[Go to Definition]\n{self._fmt_location(loc, cwd)}", 1, 1

        # 处理数组
        if isinstance(result, list):
            locations = []
            for item in result:
                if isinstance(item, dict):
                    if "targetUri" in item:
                        locations.append({
                            "uri": item["targetUri"],
                            "range": item["targetRange"],
                        })
                    elif "uri" in item:
                        locations.append(item)

            if not locations:
                return "[Go to Definition] Not found.", 0, 0

            lines = [f"[Go to Definition] {len(locations)} location(s):"]
            for loc in locations[:50]:
                lines.append(f"  {self._fmt_location(loc, cwd)}")

            file_count = len(set(l.get("uri", "") for l in locations))
            return "\n".join(lines), len(locations), file_count

        # 处理单个 Location
        if isinstance(result, dict) and "uri" in result:
            return (
                f"[Go to Definition]\n{self._fmt_location(result, cwd)}",
                1, 1,
            )

        return "[Go to Definition] Not found.", 0, 0

    def _fmt_find_references(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 findReferences 结果（对应 CC formatFindReferencesResult）。"""
        locations = result if isinstance(result, list) else []
        if not locations:
            return "[Find References] No references found.", 0, 0

        # 按文件分组
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for loc in locations:
            if isinstance(loc, dict) and "uri" in loc:
                file_path = self._format_uri(loc["uri"], cwd)
                by_file.setdefault(file_path, []).append(loc)

        lines = [f"[Find References] {len(locations)} reference(s) in {len(by_file)} file(s):"]
        for file_path, locs in sorted(by_file.items()):
            lines.append(f"  {file_path}:")
            for loc in locs[:20]:
                rng = loc.get("range", {})
                start = rng.get("start", {})
                lines.append(
                    f"    line {start.get('line', 0) + 1}, "
                    f"char {start.get('character', 0) + 1}"
                )

        return "\n".join(lines), len(locations), len(by_file)

    def _fmt_hover(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 hover 结果（对应 CC formatHoverResult）。"""
        if not result or not isinstance(result, dict):
            return "[Hover] No information available.", 0, 0

        contents = result.get("contents", "")
        if isinstance(contents, dict):
            # MarkupContent
            value = contents.get("value", "")
            kind = contents.get("kind", "plaintext")
            return f"[Hover]\n```{kind}\n{value}\n```", 0, 0
        elif isinstance(contents, list):
            parts = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    lang = item.get("language", "")
                    value = item.get("value", "")
                    if lang:
                        parts.append(f"```{lang}\n{value}\n```")
                    else:
                        parts.append(value)
            return "[Hover]\n" + "\n---\n".join(parts), 0, 0
        elif contents:
            return f"[Hover]\n{contents}", 0, 0

        return "[Hover] No information available.", 0, 0

    def _fmt_document_symbol(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 documentSymbol 结果（对应 CC formatDocumentSymbolResult）。"""
        symbols = result if isinstance(result, list) else []
        if not symbols:
            return "[Document Symbols] No symbols found.", 0, 0

        # 检测是层级结构还是平铺结构
        # 层级结构: DocumentSymbol[]（有 children 字段）
        # 平铺结构: SymbolInformation[]（有 location 字段）
        lines = []
        symbol_count = 0

        def _render_hierarchical(sym_list: list, indent: int = 0) -> None:
            nonlocal symbol_count
            prefix = "  " * indent
            for s in sym_list:
                if isinstance(s, dict):
                    name = s.get("name", "?")
                    kind = self._symbol_kind_name(s.get("kind", 0))
                    rng = s.get("range", {})
                    start = rng.get("start", {})
                    line = start.get("line", 0) + 1
                    lines.append(f"{prefix}- {kind} **{name}** (line {line})")
                    symbol_count += 1
                    children = s.get("children", [])
                    if children:
                        _render_hierarchical(children, indent + 1)

        if symbols and isinstance(symbols[0], dict) and "children" in symbols[0]:
            # 层级结构
            lines.append(
                f"[Document Symbols] {len(symbols)} top-level symbol(s):"
            )
            _render_hierarchical(symbols)
        else:
            # 平铺结构（SymbolInformation）
            by_file: Dict[str, List[Dict[str, Any]]] = {}
            for s in symbols:
                if isinstance(s, dict):
                    loc = s.get("location", {})
                    uri = loc.get("uri", "")
                    file_path = self._format_uri(uri, cwd) if uri else "?"
                    by_file.setdefault(file_path, []).append(s)

            lines.append(
                f"[Document Symbols] {len(symbols)} symbol(s) "
                f"in {len(by_file)} file(s):"
            )
            for file_path, syms in sorted(by_file.items()):
                lines.append(f"  {file_path}:")
                for s in syms:
                    name = s.get("name", "?")
                    kind = self._symbol_kind_name(s.get("kind", 0))
                    loc = s.get("location", {})
                    rng = loc.get("range", {})
                    start = rng.get("start", {})
                    lines.append(
                        f"    - {kind} **{name}** "
                        f"(line {start.get('line', 0) + 1})"
                    )
                    symbol_count += 1

        return "\n".join(lines), symbol_count, 1

    def _fmt_workspace_symbol(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 workspaceSymbol 结果（对应 CC formatWorkspaceSymbolResult）。"""
        symbols = result if isinstance(result, list) else []
        if not symbols:
            return "[Workspace Symbols] No symbols found.", 0, 0

        # 按文件分组
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for s in symbols:
            if isinstance(s, dict):
                loc = s.get("location", {})
                uri = loc.get("uri", "")
                file_path = self._format_uri(uri, cwd) if uri else "?"
                by_file.setdefault(file_path, []).append(s)

        lines = [
            f"[Workspace Symbols] {len(symbols)} symbol(s) "
            f"in {len(by_file)} file(s):"
        ]
        for file_path, syms in sorted(by_file.items()):
            lines.append(f"  {file_path}:")
            for s in syms:
                name = s.get("name", "?")
                kind = self._symbol_kind_name(s.get("kind", 0))
                loc = s.get("location", {})
                rng = loc.get("range", {})
                start = rng.get("start", {})
                lines.append(
                    f"    - {kind} **{name}** "
                    f"(line {start.get('line', 0) + 1})"
                )

        file_count = len(by_file)
        return "\n".join(lines), len(symbols), file_count

    def _fmt_go_to_implementation(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 goToImplementation（复用 goToDefinition 格式）。"""
        return self._fmt_go_to_definition(result, cwd)

    def _fmt_prepare_call_hierarchy(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 prepareCallHierarchy 结果。"""
        items = result if isinstance(result, list) else []
        if not items:
            return "[Call Hierarchy] No items found.", 0, 0

        lines = [f"[Call Hierarchy] {len(items)} item(s):"]
        for item in items:
            if isinstance(item, dict):
                name = item.get("name", "?")
                kind = self._symbol_kind_name(item.get("kind", 0))
                loc = self._fmt_location(item, cwd) if "uri" in item else ""
                lines.append(f"  - {kind} **{name}** {loc}")

        return "\n".join(lines), len(items), 1

    def _fmt_incoming_calls(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 incomingCalls 结果（对应 CC formatIncomingCallsResult）。"""
        return self._fmt_call_hierarchy(
            result, cwd, "Incoming", "from", "fromRanges"
        )

    def _fmt_outgoing_calls(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """格式化 outgoingCalls 结果（对应 CC formatOutgoingCallsResult）。"""
        return self._fmt_call_hierarchy(
            result, cwd, "Outgoing", "to", "fromRanges"
        )

    def _fmt_call_hierarchy(
        self, result: Any, cwd: str,
        label: str, target_key: str, ranges_key: str,
    ) -> Tuple[str, int, int]:
        """通用调用层次格式化。"""
        calls = result if isinstance(result, list) else []
        if not calls:
            return f"[Call Hierarchy] No {label.lower()} calls.", 0, 0

        # 按文件分组
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for call in calls:
            target = call.get(target_key, {}) if isinstance(call, dict) else {}
            uri = target.get("uri", "")
            file_path = self._format_uri(uri, cwd) if uri else "?"
            by_file.setdefault(file_path, []).append(call)

        lines = [
            f"[Call Hierarchy] {len(calls)} {label.lower()} call(s) "
            f"in {len(by_file)} file(s):"
        ]
        for file_path, file_calls in sorted(by_file.items()):
            lines.append(f"  {file_path}:")
            for call in file_calls:
                target = call.get(target_key, {}) if isinstance(call, dict) else {}
                name = target.get("name", "?")
                kind = self._symbol_kind_name(target.get("kind", 0))
                from_ranges = call.get(ranges_key, []) if isinstance(call, dict) else []
                range_strs = []
                for r in from_ranges[:3]:
                    rs = r.get("start", {})
                    range_strs.append(f"line {rs.get('line', 0) + 1}")
                r_str = ", ".join(range_strs) if range_strs else ""
                range_info = f" [from: {r_str}]" if r_str else ""
                lines.append(f"    - {kind} **{name}**{range_info}")

        return "\n".join(lines), len(calls), len(by_file)

    def _fmt_default(
        self, result: Any, cwd: str
    ) -> Tuple[str, int, int]:
        """默认格式化（fallback）。"""
        return json.dumps(result, indent=2, ensure_ascii=False), 0, 0
