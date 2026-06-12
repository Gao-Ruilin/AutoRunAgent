"""
配置管理 — 用户自定义 API 配置。

用户必须自行提供:
- API 类型 (openai / anthropic)
- API URL (完整的基础 URL，如 https://api.openai.com)
- API Key
- 模型名称

没有任何预设值。
"""

import base64
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


# ── SSH 配置管理 ────────────────────────────────────────────────────────────

def get_ssh_configs() -> list:
    """获取所有 SSH 配置列表。返回列表，每项包含 name/host/port/user/auth_type 等。

    Returns:
        List of SSH config dicts (passwords are base64-encoded).
    """
    return get_setting("ssh_configs", [])


def get_ssh_config(name: str) -> Optional[dict]:
    """获取单个 SSH 配置。

    Args:
        name: 配置名称

    Returns:
        Config dict or None if not found.
    """
    configs = get_ssh_configs()
    for cfg in configs:
        if cfg.get("name") == name:
            return dict(cfg)
    return None


def save_ssh_config(
    name: str,
    host: str,
    port: int = 22,
    user: str = "root",
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    passphrase: Optional[str] = None,
) -> list:
    """保存或更新 SSH 配置。

    Args:
        name: 配置名称（唯一标识）
        host: 服务器地址
        port: SSH 端口，默认 22
        user: 用户名，默认 root
        auth_type: 认证类型，"password" 或 "key"
        password: 密码（会 base64 编码后存储）
        key_path: 私钥路径
        passphrase: 私钥口令（会 base64 编码后存储）

    Returns:
        更新后的配置列表。
    """
    configs = get_ssh_configs()

    cfg = {
        "name": name,
        "host": host,
        "port": int(port),
        "user": user,
        "auth_type": auth_type,
    }

    # 密码/密钥脱敏存储
    if auth_type == "password" and password:
        cfg["password"] = _encode_sensitive(password)
    elif auth_type == "key":
        if key_path:
            cfg["key_path"] = key_path
        if passphrase:
            cfg["passphrase"] = _encode_sensitive(passphrase)

    # 更新或追加
    found = False
    for i, c in enumerate(configs):
        if c.get("name") == name:
            configs[i] = cfg
            found = True
            break
    if not found:
        configs.append(cfg)

    set_setting("ssh_configs", configs)
    return configs


def delete_ssh_config(name: str) -> list:
    """删除 SSH 配置。

    Args:
        name: 配置名称

    Returns:
        更新后的配置列表。
    """
    configs = get_ssh_configs()
    configs = [c for c in configs if c.get("name") != name]
    set_setting("ssh_configs", configs)
    return configs


def get_ssh_config_decrypted(name: str) -> Optional[dict]:
    """获取 SSH 配置并解密密码字段。

    Args:
        name: 配置名称

    Returns:
        Config dict with decrypted password/passphrase, or None.
    """
    cfg = get_ssh_config(name)
    if cfg is None:
        return None

    cfg = dict(cfg)
    if "password" in cfg:
        cfg["password"] = _decode_sensitive(cfg["password"])
    if "passphrase" in cfg:
        cfg["passphrase"] = _decode_sensitive(cfg["passphrase"])
    return cfg


def _encode_sensitive(value: str) -> str:
    """Base64 编码敏感信息。"""
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


def _decode_sensitive(encoded: str) -> str:
    """Base64 解码敏感信息。"""
    try:
        return base64.b64decode(encoded.encode("utf-8")).decode("utf-8")
    except Exception:
        return encoded  # 兼容未编码的旧数据


# ── Directory-scoped SSH Configs ─────────────────────────────────────────

def _ssh_configs_file(cwd: str) -> str:
    """Get the SSH configs file path for a working directory."""
    import os as _os
    dir_path = _os.path.join(cwd, ".autorun")
    _os.makedirs(dir_path, exist_ok=True)
    return _os.path.join(dir_path, "ssh_configs.json")


def get_dir_ssh_configs(cwd: str) -> list:
    """获取指定目录的 SSH 配置列表（密码脱敏）。"""
    import json, os as _os
    fpath = _ssh_configs_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            configs = json.load(f)
    except Exception:
        return []
    safe = []
    for c in configs:
        sc = dict(c)
        if sc.get("password"):
            sc["password"] = "****"
        if sc.get("key_path"):
            sc["key_path"] = sc["key_path"][:30] + "..." if len(sc.get("key_path", "")) > 30 else sc["key_path"]
        safe.append(sc)
    return safe


def get_dir_ssh_config_decrypted(cwd: str, name: str) -> Optional[dict]:
    """获取指定目录的 SSH 配置并解密密码字段。"""
    configs = get_dir_ssh_configs(cwd)
    for cfg in configs:
        if cfg.get("name") == name:
            cfg = dict(cfg)
            if "password" in cfg:
                cfg["password"] = _decode_sensitive(cfg["password"])
            if "passphrase" in cfg:
                cfg["passphrase"] = _decode_sensitive(cfg["passphrase"])
            return cfg
    return None


def save_dir_ssh_config(cwd: str, name: str, host: str, port: int = 22,
                        user: str = "root", auth_type: str = "password",
                        password: Optional[str] = None, key_path: Optional[str] = None,
                        passphrase: Optional[str] = None) -> list:
    """保存或更新指定目录的 SSH 配置。"""
    import json, os as _os
    fpath = _ssh_configs_file(cwd)

    cfg = {
        "name": name, "host": host, "port": int(port),
        "user": user, "auth_type": auth_type,
    }
    if auth_type == "password" and password:
        cfg["password"] = _encode_sensitive(password)
    elif auth_type == "key":
        if key_path:
            cfg["key_path"] = key_path
        if passphrase:
            cfg["passphrase"] = _encode_sensitive(passphrase)

    configs = []
    if _os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                configs = json.load(f)
        except Exception:
            configs = []

    found = False
    for i, c in enumerate(configs):
        if c.get("name") == name:
            configs[i] = cfg
            found = True
            break
    if not found:
        configs.append(cfg)

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)

    return configs


def delete_dir_ssh_config(cwd: str, name: str) -> list:
    """删除指定目录的 SSH 配置。"""
    import json, os as _os
    fpath = _ssh_configs_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            configs = json.load(f)
    except Exception:
        return []

    configs = [c for c in configs if c.get("name") != name]

    if configs:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
    else:
        _os.remove(fpath)

    return configs


# ── Connections (local folders + SSH remotes) ──

def get_connections() -> list:
    """获取所有已保存的连接（本地目录 + SSH 远程）。

    Returns:
        List of connection dicts with passwords redacted.
    """
    conns = get_setting("connections") or []
    safe = []
    for c in conns:
        sc = dict(c)
        if sc.get("password"):
            sc["password"] = "****"
        if sc.get("key_path") and len(sc.get("key_path", "")) > 30:
            sc["key_path"] = sc["key_path"][:30] + "..."
        safe.append(sc)
    return safe


def save_connection(conn: dict) -> list:
    """保存或更新一个连接。

    Args:
        conn: {name, type: "local"|"ssh", ...}
              local: {path}
              ssh: {host, port, user, auth_type, password?, key_path?}

    Returns:
        Updated list of all connections.
    """
    name = conn.get("name", "").strip()
    if not name:
        return get_connections()

    # 加密 SSH 密码
    sc = dict(conn)
    if sc.get("password") and sc["password"] != "****":
        sc["password"] = _encode_sensitive(sc["password"])

    conns = get_setting("connections") or []
    existing = next((c for c in conns if c.get("name") == name), None)
    if existing:
        existing.update(sc)
    else:
        conns.append(sc)

    set_setting("connections", conns)
    return get_connections()


def delete_connection(name: str) -> list:
    """删除一个连接。

    Returns:
        Updated list of all connections.
    """
    conns = get_setting("connections") or []
    conns = [c for c in conns if c.get("name") != name]
    set_setting("connections", conns)
    return get_connections()


# ── Directory-scoped Connections (per working directory) ──

def _connections_file(cwd: str) -> str:
    """Get the connections file path for a working directory."""
    import os as _os
    dir_path = _os.path.join(cwd, ".autorun")
    _os.makedirs(dir_path, exist_ok=True)
    return _os.path.join(dir_path, "connections.json")


def get_dir_connections(cwd: str) -> list:
    """获取指定目录的已保存连接（密码脱敏）。"""
    import json, os as _os
    fpath = _connections_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            conns = json.load(f)
    except Exception:
        return []
    safe = []
    for c in conns:
        sc = dict(c)
        if sc.get("password"):
            sc["password"] = "****"
        if sc.get("key_path") and len(sc.get("key_path", "")) > 30:
            sc["key_path"] = sc["key_path"][:30] + "..."
        safe.append(sc)
    return safe


def save_dir_connection(cwd: str, conn: dict) -> list:
    """保存或更新一个目录隔离的连接。

    Args:
        cwd: 当前工作目录
        conn: {name, type: "local"|"ssh", ...}

    Returns:
        Updated list of all connections for this directory.
    """
    import json, os as _os
    name = conn.get("name", "").strip()
    if not name:
        return get_dir_connections(cwd)

    sc = dict(conn)
    if sc.get("password") and sc["password"] != "****":
        sc["password"] = _encode_sensitive(sc["password"])

    fpath = _connections_file(cwd)
    conns = []
    if _os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                conns = json.load(f)
        except Exception:
            conns = []

    existing = next((c for c in conns if c.get("name") == name), None)
    if existing:
        existing.update(sc)
    else:
        conns.append(sc)

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(conns, f, ensure_ascii=False, indent=2)

    return get_dir_connections(cwd)


def delete_dir_connection(cwd: str, name: str) -> list:
    """删除指定目录的一个连接。

    Returns:
        Updated list of all connections for this directory.
    """
    import json, os as _os
    fpath = _connections_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            conns = json.load(f)
    except Exception:
        return []

    conns = [c for c in conns if c.get("name") != name]

    if conns:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(conns, f, ensure_ascii=False, indent=2)
    else:
        _os.remove(fpath)

    return get_dir_connections(cwd)
