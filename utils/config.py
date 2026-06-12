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
from typing import Any, Dict, List, Optional

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


def get_api_key(model: Optional[str] = None) -> Optional[str]:
    """获取 API Key。检查: 环境变量 -> 模型专属 key -> 全局 config.json。"""
    key = os.environ.get("AUTORUN_API_KEY")
    if key:
        return key
    if model:
        mc = _get_model_entry(model)
        if mc and mc.get("api_key"):
            return mc["api_key"]
    return get_setting("api_key")


def save_api_key(key: str) -> None:
    set_setting("api_key", key)


def get_api_url(model: Optional[str] = None) -> Optional[str]:
    """获取 API URL。检查: 环境变量 -> 模型专属 URL -> 全局 config.json。"""
    url = os.environ.get("AUTORUN_API_URL")
    if url:
        return url
    if model:
        mc = _get_model_entry(model)
        if mc and mc.get("api_url"):
            return mc["api_url"]
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


# ── 模型列表管理（每个模型可有独立的 api_url / api_type / api_key）─────────────

def _get_model_entry(name: str) -> Optional[Dict[str, Any]]:
    """在 models 数组中按名称查找模型。"""
    models = get_setting("models", [])
    for m in models:
        if isinstance(m, dict) and m.get("name") == name:
            return m
    return None


def get_models() -> List[Dict[str, Any]]:
    """获取模型列表（含每个模型的 url/type/key）。自动迁移旧格式。"""
    models = get_setting("models", [])
    if not models:
        models = _migrate_user_models()
    # 确保当前模型在列表中
    current = get_model()
    if current:
        found = False
        for m in models:
            if isinstance(m, dict) and m.get("name") == current:
                found = True
                break
        if not found:
            entry: Dict[str, Any] = {"name": current}
            api_url = get_setting("api_url")
            if api_url:
                entry["api_url"] = api_url
            api_type = get_setting("api_type")
            if api_type:
                entry["api_type"] = api_type
            models.append(entry)
            _save_models(models)
    return models


def _save_models(models: List[Dict[str, Any]]) -> None:
    """保存模型列表到 config.json 的 models 字段。"""
    set_setting("models", models)


def save_models(models: List[Dict[str, Any]]) -> None:
    """公开的保存模型列表接口（供 API 调用）。"""
    _save_models(models)


def upsert_model(name: str, api_url: Optional[str] = None,
                 api_type: Optional[str] = None,
                 api_key: Optional[str] = None,
                 note: Optional[str] = None) -> None:
    """添加或更新一个模型的信息。"""
    models = get_models()
    # 在 models 列表中查找（而非重新读文件）
    entry = None
    for m in models:
        if isinstance(m, dict) and m.get("name") == name:
            entry = m
            break
    if entry:
        if api_url is not None:
            entry["api_url"] = api_url
        if api_type is not None:
            entry["api_type"] = api_type
        if api_key is not None:
            entry["api_key"] = api_key
        if note is not None:
            entry["note"] = note
    else:
        new_entry: Dict[str, Any] = {"name": name}
        if api_url is not None:
            new_entry["api_url"] = api_url
        if api_type is not None:
            new_entry["api_type"] = api_type
        if api_key is not None:
            new_entry["api_key"] = api_key
        if note is not None:
            new_entry["note"] = note
        models.append(new_entry)
    _save_models(models)


def remove_model(name: str) -> bool:
    """从列表中删除模型。返回是否成功。"""
    models = get_models()
    new_models = [m for m in models if m.get("name") != name]
    if len(new_models) == len(models):
        return False
    _save_models(new_models)
    return True


def _migrate_user_models() -> List[Dict[str, Any]]:
    """将旧版 user_models.json 迁移到 config.json 的 models 数组。"""
    user_models_path = os.path.join(
        os.path.expanduser("~"), ".autorun", "user_models.json"
    )
    if not os.path.isfile(user_models_path):
        return []
    try:
        with open(user_models_path, "r", encoding="utf-8") as f:
            old_models = json.load(f)
        if not isinstance(old_models, list):
            return []
        global_url = get_setting("api_url")
        global_type = get_setting("api_type", "openai")
        migrated = []
        for m in old_models:
            if isinstance(m, str):
                entry = {"name": m}
            elif isinstance(m, dict):
                entry = {"name": m.get("name", "")}
                if m.get("api_url"):
                    entry["api_url"] = m["api_url"]
                if m.get("note"):
                    entry["note"] = m["note"]
            else:
                continue
            # 如果模型没有独立 URL，使用全局 URL 作为默认
            if not entry.get("api_url") and global_url:
                entry["api_url"] = global_url
            if not entry.get("api_type") and global_type:
                entry["api_type"] = global_type
            migrated.append(entry)
        # 保存迁移后的数据
        _save_models(migrated)
        # 备份后删除旧文件
        os.rename(user_models_path, user_models_path + ".bak")
        return migrated
    except Exception:
        return []


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

    key = api_key or get_api_key(model)
    url = api_url or get_api_url(model)
    atype = (api_type or get_api_type(model)).lower()
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


def get_api_type(model: Optional[str] = None) -> str:
    """获取 API 类型 (openai/anthropic)。检查: 环境变量 -> 模型专属类型 -> 全局 config。"""
    at = os.environ.get("AUTORUN_API_TYPE")
    if at:
        return at.lower()
    if model:
        mc = _get_model_entry(model)
        if mc and mc.get("api_type"):
            return mc["api_type"].lower()
    return get_setting("api_type", "openai")


def set_api_type(api_type: str) -> None:
    api_type = api_type.lower()
    if api_type not in ("openai", "anthropic"):
        raise ValueError("API type must be 'openai' or 'anthropic'")
    set_setting("api_type", api_type)


def check_config(model: Optional[str] = None) -> Dict[str, Any]:
    """检查配置完整性，返回结果。可指定模型名以使用该模型的专属配置。"""
    m = model or get_model()
    api_key = get_api_key(m)
    api_url = get_api_url(m)

    if not api_key:
        return {"ok": False, "error": "API Key not set. Use /api key <your_key>"}
    if not api_url:
        return {"ok": False, "error": "API URL not set. Use /api url <your_url>"}
    if not m:
        return {"ok": False, "error": "Model not set. Use /model <model_name>"}

    return {"ok": True, "api_type": get_api_type(m), "api_url": api_url, "model": m}


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
    """获取所有 SSH 配置列表（全局）。返回列表，每项包含 name/host/port/user/auth_type 等。
    Passwords are base64-encoded.
    """
    return get_setting("ssh_configs", [])


def get_ssh_config(name: str) -> Optional[dict]:
    """获取单个 SSH 配置。"""
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
    """保存或更新 SSH 配置。密码/口令会 base64 编码后存储。"""
    configs = get_ssh_configs()
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
    """删除 SSH 配置。"""
    configs = get_ssh_configs()
    configs = [c for c in configs if c.get("name") != name]
    set_setting("ssh_configs", configs)
    return configs


def get_ssh_config_decrypted(name: str) -> Optional[dict]:
    """获取 SSH 配置并解密密码字段。"""
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
    """Base64 解码敏感信息。兼容未编码的旧数据。"""
    try:
        return base64.b64decode(encoded.encode("utf-8")).decode("utf-8")
    except Exception:
        return encoded


# ── Directory-scoped SSH Configs ─────────────────────────────────────────

def _ssh_configs_file(cwd: str) -> str:
    """Get the SSH configs file path for a working directory."""
    import os as _os
    dir_path = _os.path.join(cwd, ".autorun")
    _os.makedirs(dir_path, exist_ok=True)
    return _os.path.join(dir_path, "ssh_configs.json")


def get_dir_ssh_configs(cwd: str) -> list:
    """获取指定目录的 SSH 配置列表（密码脱敏）。"""
    import json as _json
    import os as _os
    fpath = _ssh_configs_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            configs = _json.load(f)
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
    import json as _json
    import os as _os
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
                configs = _json.load(f)
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
        _json.dump(configs, f, ensure_ascii=False, indent=2)
    return configs


def delete_dir_ssh_config(cwd: str, name: str) -> list:
    """删除指定目录的 SSH 配置。"""
    import json as _json
    import os as _os
    fpath = _ssh_configs_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            configs = _json.load(f)
    except Exception:
        return []
    configs = [c for c in configs if c.get("name") != name]
    if configs:
        with open(fpath, "w", encoding="utf-8") as f:
            _json.dump(configs, f, ensure_ascii=False, indent=2)
    else:
        _os.remove(fpath)
    return configs


# ── Connections (local folders + SSH remotes) ──

def get_connections() -> list:
    """获取所有已保存的连接（本地目录 + SSH 远程）。密码脱敏。"""
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
    """保存或更新一个连接。密码会加密存储。"""
    name = conn.get("name", "").strip()
    if not name:
        return get_connections()
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
    """删除一个连接。"""
    conns = get_setting("connections") or []
    conns = [c for c in conns if c.get("name") != name]
    set_setting("connections", conns)
    return get_connections()


# ── Directory-scoped Connections ──

def _connections_file(cwd: str) -> str:
    """Get the connections file path for a working directory."""
    import os as _os
    dir_path = _os.path.join(cwd, ".autorun")
    _os.makedirs(dir_path, exist_ok=True)
    return _os.path.join(dir_path, "connections.json")


def get_dir_connections(cwd: str) -> list:
    """获取指定目录的已保存连接（密码脱敏）。"""
    import json as _json
    import os as _os
    fpath = _connections_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            conns = _json.load(f)
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
    """保存或更新一个目录隔离的连接。"""
    import json as _json
    import os as _os
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
                conns = _json.load(f)
        except Exception:
            conns = []
    existing = next((c for c in conns if c.get("name") == name), None)
    if existing:
        existing.update(sc)
    else:
        conns.append(sc)
    with open(fpath, "w", encoding="utf-8") as f:
        _json.dump(conns, f, ensure_ascii=False, indent=2)
    return get_dir_connections(cwd)


def delete_dir_connection(cwd: str, name: str) -> list:
    """删除指定目录的一个连接。"""
    import json as _json
    import os as _os
    fpath = _connections_file(cwd)
    if not _os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            conns = _json.load(f)
    except Exception:
        return []
    conns = [c for c in conns if c.get("name") != name]
    if conns:
        with open(fpath, "w", encoding="utf-8") as f:
            _json.dump(conns, f, ensure_ascii=False, indent=2)
    else:
        _os.remove(fpath)
    return get_dir_connections(cwd)
