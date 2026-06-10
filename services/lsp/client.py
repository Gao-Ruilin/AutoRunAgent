"""
底层 LSP JSON-RPC 客户端，通过 stdio 与语言服务器通信。

对应 src/services/lsp/LSPClient.ts — 实现 JSON-RPC 2.0 协议、
Content-Length 消息帧、请求/响应匹配、通知处理。
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional, Union

logger = logging.getLogger(__name__)

# Content-Type header 常量（部分服务器要求）
LSP_MESSAGE_HEADER = "Content-Length: {}\r\n\r\n"


class LspError(Exception):
    """LSP JSON-RPC 错误响应。"""

    def __init__(self, error: Dict[str, Any]):
        self.code: int = error.get("code", 0)
        self.message: str = error.get("message", "Unknown error")
        self.data: Any = error.get("data")
        super().__init__(f"LSP Error [{self.code}]: {self.message}")


class LspClient:
    """面向单个语言服务器的低层 JSON-RPC 客户端。

    通过 asyncio subprocess stdio 管道通信，处理 Content-Length
    帧协议、请求/响应对应、以及服务器主动推送的通知。
    """

    def __init__(self):
        self._request_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._notification_handlers: Dict[str, Callable[[Any], None]] = {}
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._buffer: bytes = b""

    async def connect(self, process: asyncio.subprocess.Process) -> None:
        """连接到已启动的服务器进程。

        Args:
            process: 已启动的 asyncio subprocess，其 stdout/stdin
                     将用于双向 JSON-RPC 通信。
        """
        self._process = process
        self._reader = process.stdout
        self._writer = process.stdin
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """持续读取服务器 stdin，按 Content-Length 帧解析并分发消息。"""
        try:
            while self._reader is not None:
                # 解析 header
                content_length = await self._read_headers()
                if content_length is None:
                    return  # EOF

                # 读取 body
                body = await self._read_body(content_length)
                if body is None:
                    return  # EOF

                try:
                    message: Dict[str, Any] = json.loads(body)
                except json.JSONDecodeError:
                    logger.warning(f"LSP client received invalid JSON: {body[:200]}")
                    continue

                self._dispatch(message)
        except asyncio.CancelledError:
            pass
        except asyncio.IncompleteReadError:
            logger.debug("LSP read loop ended (process exited)")
        except Exception:
            logger.exception("LSP read loop error")
        finally:
            # 清理所有未完成的请求
            for future in self._pending.values():
                if not future.done():
                    future.cancel()
            self._pending.clear()

    async def _read_headers(self) -> Optional[int]:
        """读取 LSP header，返回 Content-Length 值；EOF 时返回 None。"""
        headers: Dict[str, str] = {}
        while self._reader is not None:
            line = await self._reader.readline()
            if not line:
                return None
            line_str = line.decode("utf-8").rstrip("\r\n")
            if not line_str:
                break
            if ": " in line_str:
                key, value = line_str.split(": ", 1)
                headers[key.lower()] = value

        length_str = headers.get("content-length", "0")
        try:
            return int(length_str)
        except ValueError:
            logger.warning(f"Invalid Content-Length: {length_str}")
            return 0

    async def _read_body(self, content_length: int) -> Optional[str]:
        """读取指定长度的消息体；EOF 时返回 None。"""
        if content_length <= 0:
            return ""
        try:
            data = await self._reader.readexactly(content_length)
            return data.decode("utf-8")
        except asyncio.IncompleteReadError:
            return None

    def _dispatch(self, message: Dict[str, Any]) -> None:
        """根据消息类型分发到 pending future 或 notification handler。"""
        msg_id = message.get("id")
        msg_method = message.get("method")

        if msg_id is not None and msg_method is None:
            # 响应消息
            future = self._pending.pop(msg_id, None)
            if future is None:
                logger.debug(f"LSP received response for unknown request id={msg_id}")
                return
            if not future.done():
                if "error" in message:
                    future.set_exception(LspError(message["error"]))
                else:
                    future.set_result(message.get("result"))
        elif msg_method is not None:
            # 通知（或服务端请求，这里先统一当通知处理）
            handler = self._notification_handlers.get(msg_method)
            if handler:
                try:
                    result = handler(message.get("params", {}))
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception:
                    logger.exception(
                        f"Error in notification handler for {msg_method}"
                    )

    # ── 公共 API ────────────────────────────────────────────────────────

    async def send_request(self, method: str, params: Any = None) -> Any:
        """发送 LSP 请求，等待并返回结果。

        Args:
            method: LSP 方法名（如 'textDocument/definition'）。
            params: 请求参数。

        Returns:
            服务器的响应结果。

        Raises:
            LspError: 服务器返回了错误响应。
        """
        self._request_id += 1
        request_id = self._request_id

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        await self._write_message(message)

        try:
            return await future
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

    async def send_notification(self, method: str, params: Any = None) -> None:
        """发送 LSP 通知（无响应）。

        Args:
            method: LSP 通知方法名（如 'textDocument/didOpen'）。
            params: 通知参数。
        """
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self._write_message(message)

    def on_notification(self, method: str, handler: Callable[[Any], None]) -> None:
        """注册通知处理器。

        Args:
            method: LSP 通知方法名。
            handler: 当收到该通知时调用的回调函数，接收 params dict。
        """
        self._notification_handlers[method] = handler

    async def _write_message(self, message: Dict[str, Any]) -> None:
        """以 Content-Length 帧格式发送 JSON 消息。"""
        body = json.dumps(message, ensure_ascii=False)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        self._writer.write(header.encode("utf-8") + body.encode("utf-8"))
        await self._writer.drain()

    async def close(self) -> None:
        """关闭客户端连接，取消所有未完成的请求。"""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

        self._reader = None
        self._writer = None

    @property
    def is_connected(self) -> bool:
        """客户端是否仍然连接（reader 存在）。"""
        return self._reader is not None
