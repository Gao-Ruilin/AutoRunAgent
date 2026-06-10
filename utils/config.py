"""
配置管理 — 用户自定义 API 配置。

用户必须自行提供:
- API 类型 (openai / anthropic)
- API URL (完整的基础 URL，如 https://api.openai.com)
- API Key
- 模型名称

没有任何预设值。
"""

import json
import os
from typing import Any, Dict, Optional

from .env_utils import get_autorun_config_dir
from .file_lock import FileLock


def ensure_config_dir() -> str:
    config_dir = get_autorun_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    return config_dir


def load_global_config() -> Dict[str, Any]:
    config_dir = ensure_config_dir()
    config_path = os.path.join(config_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_global_config(config: Dict[str, Any]) -> None:
    config_dir = ensure_config_dir()
    config_path = os.path.join(config_dir, "config.json")
    with FileLock(config_path):
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)


def get_setting(key: str, default: Any = None) -> Any:
    config = load_global_config()
    return config.get(key, default)


def set_setting(key: str, value: Any) -> None:
    config = load_global_config()
    config[key] = value
    save_global_config(config)


def get_api_key() -> Optional[str]:
    """获取 API Key。检查: AUTORUN_API_KEY 环境变量 -> config.json。"""
    key = os.environ.get("AUTORUN_API_KEY")
    if key:
        return key
    return get_setting("api_key")


def save_api_key(key: str) -> None:
    set_setting("api_key", key)


def get_api_url() -> Optional[str]:
    """获取 API URL。检查: AUTORUN_API_URL 环境变量 -> config.json。"""
    url = os.environ.get("AUTORUN_API_URL")
    if url:
        return url
    return get_setting("api_url")


def set_api_url(url: str) -> None:
    set_setting("api_url", url)


def get_model() -> Optional[str]:
    """获取模型名称。检查: AUTORUN_MODEL 环境变量 -> config.json。无默认值。"""
    model = os.environ.get("AUTORUN_MODEL")
    if model:
        return model
    return get_setting("model")


def set_model(model: str) -> None:
    set_setting("model", model)


def get_context_window() -> int:
    """获取上下文窗口大小（tokens 数）。默认 500000。"""
    n = os.environ.get("AUTORUN_CONTEXT_WINDOW")
    if n:
        try:
            return int(n)
        except ValueError:
            pass
    val = get_setting("context_window")
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return 500000


async def fetch_model_context_window(
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    api_type: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[int]:
    """通过 API 获取模型的最大上下文窗口。

    OpenAI 格式：GET /v1/models/{model} → 解析 max_context_length / context_window。
    Anthropic 格式：无标准 models endpoint，直接返回 None。

    失败/超时均返回 None，由调用方 fallback 到 get_context_window()。
    """
    import httpx

    key = api_key or get_api_key()
    url = api_url or get_api_url()
    atype = (api_type or get_api_type()).lower()
    m = model or get_model()

    if not key or not url or not m:
        return None

    if atype != "openai":
        # Anthropic 没有标准的 /v1/models 信息端点
        return None

    try:
        models_url = url.rstrip("/") + "/v1/models/" + m
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            # 尝试各个可能的字段
            for field in ("max_context_length", "context_window", "max_tokens",
                          "context_length", "max_input_tokens"):
                val = data.get(field)
                if val is not None:
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        continue
            # 某些提供商把信息嵌套在 capabilities 里
            caps = data.get("capabilities") or data.get("model_info") or {}
            if isinstance(caps, dict):
                for field in ("max_context_length", "context_window", "max_input_tokens"):
                    val = caps.get(field)
                    if val is not None:
                        try:
                            return int(val)
                        except (ValueError, TypeError):
                            continue
    except Exception:
        pass
    return None


async def resolve_context_window() -> int:
    """解析上下文窗口大小：先尝试 API 查询，失败回退到配置默认。"""
    try:
        from_api = await fetch_model_context_window()
        if from_api and from_api > 0:
            return from_api
    except Exception:
        pass
    return get_context_window()


def set_context_window(tokens: int) -> None:
    """设置上下文窗口大小。"""
    if tokens < 1000:
        raise ValueError("Context window must be at least 1000 tokens")
    set_setting("context_window", tokens)


def get_api_type() -> str:
    """获取 API 类型 (openai/anthropic)。默认为 openai。"""
    at = os.environ.get("AUTORUN_API_TYPE")
    if at:
        return at.lower()
    return get_setting("api_type", "openai")


def set_api_type(api_type: str) -> None:
    api_type = api_type.lower()
    if api_type not in ("openai", "anthropic"):
        raise ValueError("API type must be 'openai' or 'anthropic'")
    set_setting("api_type", api_type)


def check_config() -> Dict[str, Any]:
    """检查配置完整性，返回结果。"""
    api_key = get_api_key()
    api_url = get_api_url()
    model = get_model()

    if not api_key:
        return {"ok": False, "error": "API Key not set. Use /api key <your_key>"}
    if not api_url:
        return {"ok": False, "error": "API URL not set. Use /api url <your_url>"}
    if not model:
        return {"ok": False, "error": "Model not set. Use /model <model_name>"}

    return {"ok": True, "api_type": get_api_type(), "api_url": api_url, "model": model}


def get_ocr_model_id() -> str:
    """获取 OCR 模型 ID。检查 AUTORUN_OCR_MODEL_ID 环境变量 -> config.json。默认 tiiuae/Falcon-OCR。"""
    mid = os.environ.get("AUTORUN_OCR_MODEL_ID")
    if mid:
        return mid
    return get_setting("ocr_model_id", "tiiuae/Falcon-OCR")


def set_ocr_model_id(model_id: str) -> None:
    set_setting("ocr_model_id", model_id)


def get_ocr_device() -> Optional[str]:
    """获取 OCR 推理设备。默认自动检测（CUDA > CPU）。"""
    dev = os.environ.get("AUTORUN_OCR_DEVICE")
    if dev:
        return dev
    return get_setting("ocr_device")


def set_ocr_device(device: str) -> None:
    set_setting("ocr_device", device)


def get_ocr_local_dir() -> Optional[str]:
    """获取 OCR 模型本地缓存目录。"""
    d = os.environ.get("AUTORUN_OCR_LOCAL_DIR")
    if d:
        return d
    return get_setting("ocr_local_dir")


def set_ocr_local_dir(local_dir: str) -> None:
    set_setting("ocr_local_dir", local_dir)


def get_language() -> Optional[str]:
    return get_setting("language")


def compute_device_fingerprint() -> str:
    """保留兼容 — 不再使用。"""
    return ""
