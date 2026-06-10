"""
统一 API 客户端 — 直接调用 OpenAI 或 Anthropic 兼容 API。

不再使用转发服务器，用户自行提供:
- model: 模型名称 (如 gpt-4o, claude-sonnet-4-6)
- api_url: API 基础 URL (如 https://api.openai.com)
- api_key: API 密钥

支持两种 API 格式:
- openai: 使用 /v1/chat/completions 端点
- anthropic: 使用 /v1/messages 端点
"""

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from AutoRUN_v1.utils.config import (
    check_config,
    get_api_key,
    get_api_type,
    get_api_url,
    get_model,
)

logger = logging.getLogger(__name__)

# ── Retry configuration ─────────────────────────────────────────────────

MAX_RETRIES = 2           # 最多重试 2 次（共 3 次尝试）
RETRY_BASE_DELAY = 1.0    # 基础延迟秒数（指数退避: 1s, 2s）


class _RetryableError(Exception):
    """可重试的 API 错误（5xx、网络错误等）。"""
    pass


def _is_retryable_http_status(status_code: int) -> bool:
    """判断 HTTP 状态码是否可重试。"""
    return status_code >= 500 or status_code in (429, 408)


def _ensure_configured():
    """确保 API 配置完整，否则抛出详细错误。"""
    result = check_config()
    if not result["ok"]:
        raise ValueError(
            f"API 配置不完整: {result['error']}\n"
            f"请使用以下命令设置:\n"
            f"  /api type <openai|anthropic>  — 设置 API 类型\n"
            f"  /api url <api_url>            — 设置 API URL\n"
            f"  /api key <api_key>            — 设置 API 密钥\n"
            f"  /model <model_name>           — 设置模型名称"
        )


class OpenAICompatibleClient:
    """OpenAI 兼容 API 客户端。

    调用 /v1/chat/completions 端点，将流式响应转换为内部事件格式。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def stream_message(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False,
        **kwargs,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """使用 OpenAI 格式发送流式聊天请求。

        Yields:
            内部事件字典 (text_delta, tool_use_start, input_json_delta,
            content_block_stop, message_delta, message_stop, error)
        """
        effective_model = model or self.model

        # 构建 OpenAI 格式请求消息
        openai_messages = []
        if system_prompt:
            openai_messages.append({
                "role": "system",
                "content": system_prompt,
            })

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, list):
                # ── Convert Anthropic-style content blocks to OpenAI format ──
                if role == "assistant":
                    text_parts = []
                    tool_calls = []
                    reasoning_parts = []
                    for part in content:
                        p = part if isinstance(part, dict) else {}
                        if p.get("type") == "tool_use":
                            tool_calls.append({
                                "id": p.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": p.get("name", ""),
                                    "arguments": json.dumps(p.get("input", {}), ensure_ascii=False),
                                },
                            })
                        elif p.get("type") == "text":
                            text_parts.append(p.get("text", ""))
                        elif p.get("type") in ("thinking", "redacted_thinking"):
                            # DeepSeek requires reasoning_content to be passed back
                            if p.get("thinking"):
                                reasoning_parts.append(p.get("thinking", ""))
                        else:
                            text_parts.append(str(part))
                    msg_out = {"role": "assistant"}
                    text_content = "\n".join(text_parts)
                    if text_content or not tool_calls:
                        msg_out["content"] = text_content
                    if reasoning_parts:
                        msg_out["reasoning_content"] = "\n".join(reasoning_parts)
                    if tool_calls:
                        msg_out["tool_calls"] = tool_calls
                    openai_messages.append(msg_out)
                    continue

                if role == "user":
                    has_tool_result = any(
                        (isinstance(p, dict) and p.get("type") == "tool_result")
                        for p in content
                    )
                    if has_tool_result:
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "tool_result":
                                rc = part.get("content", "")
                                openai_messages.append({
                                    "role": "tool",
                                    "tool_call_id": part.get("tool_use_id", ""),
                                    "content": rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False),
                                })
                            elif isinstance(part, dict) and part.get("type") == "text":
                                openai_messages.append({
                                    "role": "user",
                                    "content": part.get("text", ""),
                                })
                        continue

                # Plain content list (text blocks only — both APIs use same format)
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") in ("text",):
                            parts.append(part)
                        elif part.get("type") in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
                            continue
                        else:
                            parts.append(part)
                    else:
                        parts.append({"type": "text", "text": str(part)})
                openai_messages.append({"role": role, "content": parts})
            else:
                openai_messages.append({"role": role, "content": content})

        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if disable_thinking:
            body["thinking"] = {"type": "disabled"}

        # Only include max_tokens if explicitly provided by caller
        # When not provided, let the API provider use its own default
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        if tools:
            openai_tools = []
            for t in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {
                            "type": "object",
                            "properties": {},
                        }),
                    },
                })
            body["tools"] = openai_tools

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # ── Retry loop ──────────────────────────────────────────────────
        last_error: Optional[str] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=headers,
                    ) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            error_msg = error_text.decode("utf-8", errors="replace")[:500]
                            full_msg = f"OpenAI API {response.status_code}: {error_msg}"
                            if _is_retryable_http_status(response.status_code) and attempt < MAX_RETRIES:
                                raise _RetryableError(full_msg)
                            yield {
                                "type": "error",
                                "error": full_msg,
                                "is_api_error": True,
                            }
                            return

                        # ── 解析 SSE 流 ──────────────────────────────────
                        tool_calls_map: Dict[int, Dict[str, Any]] = {}
                        content_text = ""
                        finish_reason = None
                        usage = {}
                        tool_started: set = set()
                        thinking_started = False

                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data: "):
                                continue

                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break

                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            choices = chunk.get("choices", [])
                            if not choices:
                                if "usage" in chunk:
                                    usage = _convert_openai_usage(chunk["usage"])
                                continue

                            delta = choices[0].get("delta", {})
                            finish = choices[0].get("finish_reason")

                            # ── 推理内容增量 (DeepSeek thinking mode) ──
                            rc = delta.get("reasoning_content")
                            if rc:
                                if not thinking_started:
                                    thinking_started = True
                                    yield {"type": "thinking_start"}
                                yield {"type": "thinking_delta", "thinking": rc}

                            # ── 文本增量 ──
                            if "content" in delta and delta["content"]:
                                thinking_started = False
                                content_text += delta["content"]
                                yield {"type": "text_delta", "text": delta["content"]}

                            # ── 工具调用 ──
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)

                                    if idx not in tool_calls_map:
                                        tc_id = tc.get("id", "")
                                        fn = tc.get("function", {})
                                        tc_name = fn.get("name", "")
                                        tc_args = fn.get("arguments", "")
                                        tool_calls_map[idx] = {
                                            "id": tc_id,
                                            "name": tc_name,
                                            "arguments": tc_args,
                                        }
                                    else:
                                        fn = tc.get("function", {})
                                        args_delta = fn.get("arguments", "")
                                        tool_calls_map[idx]["arguments"] += args_delta

                                    entry = tool_calls_map[idx]
                                    if entry["id"] and idx not in tool_started:
                                        tool_started.add(idx)
                                        yield {
                                            "type": "tool_use_start",
                                            "content_block": {
                                                "type": "tool_use",
                                                "id": entry["id"],
                                                "name": entry["name"],
                                            },
                                        }

                                    fn_delta = tc.get("function", {})
                                    args_part = fn_delta.get("arguments", "")
                                    if args_part:
                                        yield {"type": "input_json_delta", "partial_json": args_part}

                                if finish == "tool_calls":
                                    finish_reason = "tool_use"

                            # ── 结束原因 ──
                            if finish and finish != "tool_calls":
                                finish_reason = finish

                        # ── 流结束：关闭未完成的 tool calls ──
                        for idx in sorted(tool_calls_map.keys()):
                            yield {"type": "content_block_stop"}

                        # ── 发送结束事件 ──
                        stop_reason_map = {
                            "stop": "end_turn",
                            "tool_calls": "tool_use",
                            "length": "max_tokens",
                            "content_filter": "content_filter",
                        }
                        yield {
                            "type": "message_delta",
                            "stop_reason": stop_reason_map.get(finish_reason, finish_reason),
                            "usage": usage,
                        }
                        yield {"type": "message_stop"}

                return  # 成功，退出重试循环

            except _RetryableError as e:
                last_error = str(e)
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"OpenAI API call failed (attempt {attempt+1}/{MAX_RETRIES+1}), "
                    f"retrying in {wait}s: {e}"
                )
                yield {
                    "type": "retry_attempt",
                    "attempt": attempt + 1,
                    "max_retries": MAX_RETRIES,
                    "wait": wait,
                    "error": last_error,
                }
                await asyncio.sleep(wait)

            except httpx.HTTPError as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    wait = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"OpenAI HTTP error (attempt {attempt+1}/{MAX_RETRIES+1}), "
                        f"retrying in {wait}s: {e}"
                    )
                    yield {
                        "type": "retry_attempt",
                        "attempt": attempt + 1,
                        "max_retries": MAX_RETRIES,
                        "wait": wait,
                        "error": last_error,
                    }
                    await asyncio.sleep(wait)
                    continue
                logger.debug(f"OpenAI HTTP error (exhausted retries): {e}")
                yield {"type": "error", "error": str(e), "is_api_error": True}
                return

            except Exception as e:
                logger.debug(f"OpenAI API error: {e}")
                yield {"type": "error", "error": str(e), "is_api_error": True}
                return

        # 重试耗尽
        yield {
            "type": "error",
            "error": f"API call failed after {MAX_RETRIES+1} attempts: {last_error}",
            "is_api_error": True,
        }


class AnthropicClient:
    """Anthropic API 客户端。

    调用 /v1/messages 端点，流式解析 SSE 事件。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def stream_message(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        disable_thinking: bool = False,
        **kwargs,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """使用 Anthropic 格式发送流式消息请求。

        Yields:
            内部事件字典。
        """
        effective_model = model or self.model

        anthropic_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            anthropic_messages.append({"role": role, "content": content})

        # Calculate max_tokens dynamically if not explicitly set.
        # Anthropic requires max_tokens — use a generous limit.
        if max_tokens is None:
            from AutoRUN_v1.utils.tokens import get_max_output_tokens
            max_tokens = get_max_output_tokens(messages, system_prompt)

        body: Dict[str, Any] = {
            "model": effective_model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
            "stream": True,
        }

        if system_prompt:
            body["system"] = system_prompt if isinstance(system_prompt, str) else "\n".join(system_prompt)

        if tools:
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {
                        "type": "object",
                        "properties": {},
                    }),
                }
                for t in tools
            ]

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # ── Retry loop ──────────────────────────────────────────────────
        last_error: Optional[str] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=headers,
                    ) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            error_msg = error_text.decode("utf-8", errors="replace")[:500]
                            full_msg = f"Anthropic API {response.status_code}: {error_msg}"
                            if _is_retryable_http_status(response.status_code) and attempt < MAX_RETRIES:
                                raise _RetryableError(full_msg)
                            yield {
                                "type": "error",
                                "error": full_msg,
                                "is_api_error": True,
                            }
                            return

                        event_type = ""
                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                                continue
                            if line.startswith("data: "):
                                data_str = line[6:].strip()
                                if not data_str:
                                    continue
                                try:
                                    data = json.loads(data_str)
                                except json.JSONDecodeError:
                                    continue

                                if event_type == "content_block_start":
                                    block = data.get("content_block", {})
                                    if block.get("type") == "tool_use":
                                        yield {
                                            "type": "tool_use_start",
                                            "content_block": block,
                                        }
                                    elif block.get("type") == "text":
                                        yield {"type": "text_block_start"}

                                elif event_type == "content_block_delta":
                                    delta = data.get("delta", {})
                                    dt = delta.get("type", "")
                                    if dt == "text_delta":
                                        yield {"type": "text_delta", "text": delta.get("text", "")}
                                    elif dt == "input_json_delta":
                                        yield {
                                            "type": "input_json_delta",
                                            "partial_json": delta.get("partial_json", ""),
                                        }
                                    elif dt == "thinking_delta":
                                        yield {
                                            "type": "thinking_delta",
                                            "thinking": delta.get("thinking", ""),
                                        }

                                elif event_type == "content_block_stop":
                                    yield {"type": "content_block_stop"}

                                elif event_type == "message_delta":
                                    delta = data.get("delta", {})
                                    msg_usage = data.get("usage", {})
                                    yield {
                                        "type": "message_delta",
                                        "stop_reason": delta.get("stop_reason"),
                                        "usage": _convert_anthropic_usage(msg_usage),
                                    }

                                elif event_type == "message_stop":
                                    yield {"type": "message_stop"}

                                elif event_type == "error":
                                    err_data = data.get("error", {})
                                    yield {
                                        "type": "error",
                                        "error": err_data.get("message", str(data)),
                                        "is_api_error": True,
                                    }

                return  # 成功，退出重试循环

            except _RetryableError as e:
                last_error = str(e)
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Anthropic API call failed (attempt {attempt+1}/{MAX_RETRIES+1}), "
                    f"retrying in {wait}s: {e}"
                )
                yield {
                    "type": "retry_attempt",
                    "attempt": attempt + 1,
                    "max_retries": MAX_RETRIES,
                    "wait": wait,
                    "error": last_error,
                }
                await asyncio.sleep(wait)

            except httpx.HTTPError as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    wait = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Anthropic HTTP error (attempt {attempt+1}/{MAX_RETRIES+1}), "
                        f"retrying in {wait}s: {e}"
                    )
                    yield {
                        "type": "retry_attempt",
                        "attempt": attempt + 1,
                        "max_retries": MAX_RETRIES,
                        "wait": wait,
                        "error": last_error,
                    }
                    await asyncio.sleep(wait)
                    continue
                logger.debug(f"Anthropic HTTP error (exhausted retries): {e}")
                yield {"type": "error", "error": str(e), "is_api_error": True}
                return

            except Exception as e:
                logger.debug(f"Anthropic API error: {e}")
                yield {"type": "error", "error": str(e), "is_api_error": True}
                return

        # 重试耗尽
        yield {
            "type": "error",
            "error": f"API call failed after {MAX_RETRIES+1} attempts: {last_error}",
            "is_api_error": True,
        }


# ── Utility ──────────────────────────────────────────────────────────────────

def _convert_openai_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def _convert_anthropic_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


# ── Factory ──────────────────────────────────────────────────────────────────

def create_client() -> Any:
    """根据配置创建适当的 API 客户端。"""
    _ensure_configured()

    api_type = get_api_type()
    api_key = get_api_key()
    api_url = get_api_url()
    model = get_model()

    if api_type == "openai":
        return OpenAICompatibleClient(api_key, api_url, model)
    elif api_type == "anthropic":
        return AnthropicClient(api_key, api_url, model)
    else:
        raise ValueError(f"Unsupported API type: {api_type}")


# ── Global singleton ─────────────────────────────────────────────────────────

_client: Any = None


def get_client() -> Any:
    """获取或创建 API 客户端单例。"""
    global _client
    if _client is None:
        _client = create_client()
    return _client


def reset_client() -> None:
    """重置客户端单例（配置变更后调用）。"""
    global _client
    _client = None
