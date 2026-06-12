"""
FastAPI Web UI server.

Mirrors src/web/server.ts — provides HTTP REST API + WebSocket endpoints
for the AutoRUN Web UI, plus static file serving for the frontend SPA.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from AutoRUN_v1.utils.config import get_model, get_api_type, get_api_url, get_api_key as cfg_get_api_key, get_context_window, check_config
from AutoRUN_v1.utils.file_lock import FileLock

logger = logging.getLogger(__name__)

# ── FastAPI Application ──────────────────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Clean up background tasks on server shutdown."""
    yield
    # Shut down all indexers from all sessions
    for state in _ws_states.values():
        idx = getattr(state, "indexer", None)
        if idx:
            try:
                idx.shutdown()
            except Exception:
                pass
    # Cancel all agent poll tasks
    for task in _agent_poll_tasks.values():
        if not task.done():
            task.cancel()
    # Cancel all remaining ws tasks
    for task in _ws_tasks.values():
        if not task.done():
            task.cancel()
    # Clean up auto-trigger callbacks
    try:
        from AutoRUN_v1.tools.agent_tool import _on_agent_done_callbacks
        _on_agent_done_callbacks.clear()
    except Exception:
        pass


app = FastAPI(title="AutoRUN_v1 Web UI", version="1.0.0", lifespan=lifespan)

# In-memory session storage
_ws_sessions: Dict[str, WebSocket] = {}
_ws_states: Dict[str, Any] = {}
_ws_engines: Dict[str, Any] = {}
_ws_cancel_events: Dict[str, asyncio.Event] = {}
_ws_tasks: Dict[str, asyncio.Task] = {}
_ws_queued: Dict[str, List[Dict[str, Any]]] = {}
_chat_sessions: Dict[str, Dict[str, Any]] = {}

# Agent visibility tracking
_agent_last_status: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> last known agent status list
_agent_outputs: Dict[str, Dict[str, List[str]]] = {}  # session_id -> {agent_id: [output_chunks]}
_agent_poll_tasks: Dict[str, asyncio.Task] = {}  # session_id -> poll task

# Auto-trigger guard: prevent duplicate auto-trigger scheduling
_auto_trigger_guard: Dict[str, asyncio.Lock] = {}

# Token usage tracking
# In-memory: current WebSocket session token counts (session_id -> total)
_session_tokens: Dict[str, int] = {}
# Persistent file path: ~/.autorun/token_usage.json
_TOKEN_USAGE_FILE = os.path.join(os.path.expanduser("~"), ".autorun", "token_usage.json")


def _load_token_usage() -> Dict[str, Any]:
    """加载持久化的 token 使用数据。"""
    try:
        if os.path.isfile(_TOKEN_USAGE_FILE):
            with open(_TOKEN_USAGE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.debug("Failed to load token_usage.json", exc_info=True)
    return {"global": 0, "projects": {}, "conversations": {}, "sessions": {}}


def _save_token_usage(data: Dict[str, Any]) -> None:
    """保存 token 使用数据到文件。"""
    try:
        os.makedirs(os.path.dirname(_TOKEN_USAGE_FILE), exist_ok=True)
        with FileLock(_TOKEN_USAGE_FILE):
            with open(_TOKEN_USAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("Failed to save token_usage.json", exc_info=True)

# ── REST API Routes ──────────────────────────────────────────────────────────


@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    """健康检查端点。"""
    return {
        "status": "ok",
        "version": "1.0.0",
        "model": get_model(),
        "sessions": len(_ws_sessions),
    }


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """获取当前配置（含模型列表及每个模型的 URL）。"""
    from AutoRUN_v1.utils.config import get_models as cfg_get_models
    current_model = get_model()
    return {
        "model": current_model or "",
        "api_type": get_api_type(current_model),
        "api_url": get_api_url(current_model) or "",
        "api_key": get_api_key(current_model) or "",
        "context_window": get_context_window(),
        "ws_sessions": len(_ws_sessions),
        "api_configured": bool(get_api_key(current_model)),
        "models": cfg_get_models(),
    }


@app.post("/api/config")
async def update_config(request: Request) -> Dict[str, Any]:
    """更新配置（API Key、模型、语言等）。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    from AutoRUN_v1.utils.config import set_setting, set_api_url, set_api_type, save_api_key, set_context_window
    from AutoRUN_v1.api.client import reset_client

    changed = False
    if "api_type" in data and data["api_type"]:
        set_api_type(data["api_type"])
        changed = True
    if "api_url" in data and data["api_url"]:
        set_api_url(data["api_url"])
        changed = True
    if "api_key" in data and data["api_key"]:
        save_api_key(data["api_key"])
        changed = True
    if "model" in data and data["model"]:
        set_setting("model", data["model"])
        changed = True
    if "context_window" in data and data["context_window"]:
        try:
            set_context_window(int(data["context_window"]))
            changed = True
        except (ValueError, TypeError):
            pass
    if "language" in data:
        set_setting("language", data["language"])

    if changed:
        reset_client()

    return {"status": "ok"}


@app.post("/api/config/set-model")
async def set_model(request: Request) -> Dict[str, Any]:
    """切换当前模型，同时可更新该模型的专属 URL/类型。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    model = data.get("model", "").strip()
    if not model:
        return JSONResponse({"error": "Empty model"}, status_code=400)

    from AutoRUN_v1.utils.config import set_model as cfg_set_model, upsert_model
    from AutoRUN_v1.api.client import reset_client

    cfg_set_model(model)
    # 如果前端传了模型专属配置，一同保存
    api_url = data.get("api_url", "").strip() or None
    api_type = data.get("api_type", "").strip() or None
    api_key = data.get("api_key", "").strip() or None
    if api_url or api_type or api_key:
        upsert_model(model, api_url=api_url, api_type=api_type, api_key=api_key)
    reset_client()
    return {"status": "ok", "model": model}


# ── SSH Config Endpoints ──

@app.get("/api/ssh-configs")
async def get_ssh_configs() -> Dict[str, Any]:
    """获取当前目录的 SSH 配置（密码脱敏）。"""
    from AutoRUN_v1.utils.config import get_dir_ssh_configs
    cwd = _base_dir
    return {"configs": get_dir_ssh_configs(cwd)}


@app.post("/api/ssh-configs")
async def save_ssh_config_endpoint(request: Request) -> Dict[str, Any]:
    """保存当前目录的 SSH 配置。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)

    from AutoRUN_v1.utils.config import save_dir_ssh_config
    cwd = _base_dir
    save_dir_ssh_config(
        cwd=cwd,
        name=name,
        host=data.get("host", ""),
        port=int(data.get("port", 22)),
        user=data.get("user", ""),
        auth_type=data.get("auth_type", "password"),
        password=data.get("password", ""),
        key_path=data.get("key_path", ""),
    )
    return {"status": "ok"}


@app.delete("/api/ssh-configs")
async def delete_ssh_config_endpoint(request: Request) -> Dict[str, Any]:
    """删除当前目录的 SSH 配置。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)

    from AutoRUN_v1.utils.config import delete_dir_ssh_config
    cwd = _base_dir
    delete_dir_ssh_config(cwd, name)
    return {"status": "ok"}


# ── Connections (local folders + SSH remotes) ──

@app.get("/api/connections")
async def get_connections_endpoint() -> Dict[str, Any]:
    """获取当前目录的已保存连接（密码脱敏）。"""
    from AutoRUN_v1.utils.config import get_dir_connections
    cwd = _base_dir
    return {"connections": get_dir_connections(cwd)}


@app.post("/api/connections")
async def save_connection_endpoint(request: Request) -> Dict[str, Any]:
    """保存当前目录的连接。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    from AutoRUN_v1.utils.config import save_dir_connection
    cwd = _base_dir
    save_dir_connection(cwd, data)
    return {"status": "ok"}


@app.delete("/api/connections")
async def delete_connection_endpoint(request: Request) -> Dict[str, Any]:
    """删除当前目录的一个连接。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    from AutoRUN_v1.utils.config import delete_dir_connection
    cwd = _base_dir
    delete_dir_connection(cwd, name)
    return {"status": "ok"}


@app.post("/api/connections/{name}/connect")
async def connect_ssh_endpoint(name: str) -> Dict[str, Any]:
    """建立 SSH 连接并返回远程根目录文件列表。"""
    from AutoRUN_v1.utils.config import get_dir_connections, _decode_sensitive

    cwd = _base_dir
    conns = get_dir_connections(cwd)
    cfg = None
    for c in conns:
        if c.get("name") == name and c.get("type") == "ssh":
            cfg = dict(c)
            break
    if not cfg:
        return JSONResponse({"error": f"SSH config '{name}' not found"}, status_code=404)

    # Decrypt password
    if cfg.get("password") and cfg["password"] != "****":
        cfg["password"] = _decode_sensitive(cfg["password"])

    try:
        from AutoRUN_v1.utils.ssh_client import get_ssh_client
        client = get_ssh_client()
        key_path = cfg.get("key_path", "")
        if key_path:
            import os as _os
            key_path = _os.path.expanduser(key_path)
            if not _os.path.isabs(key_path):
                key_path = _os.path.abspath(key_path)
        client.connect(
            name,
            host=cfg["host"], port=cfg.get("port", 22),
            user=cfg["user"], password=cfg.get("password", ""),
            key_path=key_path,
        )
        result = client.exec_command(cfg["host"], cfg.get("port", 22), "ls -la /")
        stdout = result.get("stdout", "")
        files = _parse_ls_output(stdout, "/")
        return {"status": "ok", "files": files, "root": "/"}
    except Exception as e:
        return JSONResponse({"error": f"SSH connection failed: {str(e)}"}, status_code=500)


@app.get("/api/connections/{name}/files")
async def get_remote_files(name: str, path: str = "/") -> Dict[str, Any]:
    """获取远程目录文件列表。"""
    from AutoRUN_v1.utils.config import get_dir_connections, _decode_sensitive

    cwd = _base_dir
    conns = get_dir_connections(cwd)
    cfg = None
    for c in conns:
        if c.get("name") == name and c.get("type") == "ssh":
            cfg = dict(c)
            break
    if not cfg:
        return JSONResponse({"error": f"SSH config '{name}' not found"}, status_code=404)

    if cfg.get("password") and cfg["password"] != "****":
        cfg["password"] = _decode_sensitive(cfg["password"])

    try:
        from AutoRUN_v1.utils.ssh_client import get_ssh_client
        client = get_ssh_client()
        safe_path = path.replace("..", "").replace(";", "").replace("&", "").replace("|", "")
        if not safe_path.startswith("/"):
            safe_path = "/" + safe_path
        result = client.exec_command(cfg["host"], cfg.get("port", 22), f"ls -la {safe_path}")
        stdout = result.get("stdout", "")
        files = _parse_ls_output(stdout, safe_path)
        return {"status": "ok", "files": files, "path": safe_path}
    except Exception as e:
        return JSONResponse({"error": f"Failed to list remote files: {str(e)}"}, status_code=500)


def _parse_ls_output(stdout: str, parent_path: str) -> list:
    """解析 ls -la 输出为文件列表。"""
    files = []
    for line in stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            perms = parts[0]
            is_dir = perms.startswith("d")
            name = " ".join(parts[8:])
            if name in (".", ".."):
                continue
            files.append({
                "name": name,
                "path": parent_path.rstrip("/") + "/" + name,
                "is_dir": is_dir,
                "size": parts[4] if not is_dir else "",
                "modified": " ".join(parts[5:8]),
            })
        except (IndexError, ValueError):
            continue
    return sorted(files, key=lambda f: (not f["is_dir"], f["name"].lower()))


@app.get("/api/sessions")
async def list_sessions() -> Dict[str, Any]:
    """列出活跃的 WebSocket 会话。"""
    return {
        "sessions": list(_ws_sessions.keys()),
        "count": len(_ws_sessions),
    }


@app.get("/api/indexer/status")
async def get_indexer_status(session_id: str = "") -> Dict[str, Any]:
    """获取文件索引器状态。"""
    state = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]

    indexer = getattr(state, "indexer", None) if state else None
    if indexer is None:
        # 尝试从任意活跃会话获取索引器
        for s in _ws_states.values():
            indexer = getattr(s, "indexer", None)
            if indexer is not None:
                break

    if indexer is None:
        return {"exists": False, "ready": False, "needs_prompt": False,
                "is_building": False, "file_count": 0,
                "context_length": 0, "enabled": False,
                "estimated_total": 0, "estimated_large": False}
    from AutoRUN_v1.services.indexer import quick_estimate
    estimated = quick_estimate(os.getcwd())
    ctx = indexer.get_injectable_context() if indexer.is_ready else ""
    return {
        "exists": indexer.is_ready,
        "ready": indexer.is_ready,
        "needs_prompt": indexer.needs_prompt(),
        "is_building": indexer.is_building,
        "file_count": indexer.file_count,
        "context_length": len(ctx),
        "enabled": indexer.enabled,
        "estimated_total": estimated,
        "estimated_large": estimated > 200,
    }


@app.post("/api/indexer/toggle")
async def toggle_index(request: Request, session_id: str = "") -> Dict[str, Any]:
    """启用或禁用文件索引注入。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    enabled = body.get("enabled", True)

    # 优先操作指定会话的索引器
    idx = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]
        idx = getattr(state, "indexer", None)
    if idx is None:
        # 遍历所有会话
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return JSONResponse({"error": "No indexer available"}, status_code=400)
    idx.set_enabled(bool(enabled))
    # Invalidate cached engines so next message picks up the change
    _ws_engines.clear()
    return {"status": "ok", "enabled": idx.enabled}


@app.post("/api/indexer/build")
async def build_index(request: Request, session_id: str = "") -> Dict[str, Any]:
    """开始构建文件索引。"""
    # Check force flag from request body
    force = False
    try:
        body = await request.json()
        force = body.get("force", False)
    except Exception:
        pass

    # Check for large directories
    from AutoRUN_v1.services.indexer import quick_estimate
    estimated = quick_estimate(os.getcwd())
    if estimated > 200 and not force:
        return JSONResponse({
            "error": "large_directory",
            "estimated": estimated,
            "message": f"当前目录估计有 {estimated} 个文件/文件夹，构建索引会消耗大量资源。如需继续请再次确认。",
        }, status_code=409)

    state = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]

    idx = getattr(state, "indexer", None) if state else None
    if idx is None:
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return JSONResponse({"error": "No indexer available"}, status_code=400)
    idx.mark_user_response(accepted=True)
    return {"status": "building"}


@app.post("/api/indexer/skip")
async def skip_index(session_id: str = "") -> Dict[str, Any]:
    """跳过文件索引构建。"""
    state = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]

    idx = getattr(state, "indexer", None) if state else None
    if idx is None:
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return JSONResponse({"error": "No indexer available"}, status_code=400)
    idx.mark_user_response(accepted=False)
    return {"status": "skipped"}


@app.delete("/api/indexer")
async def delete_index(session_id: str = "") -> Dict[str, Any]:
    """删除文件索引。"""
    state = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]

    idx = getattr(state, "indexer", None) if state else None
    if idx is None:
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return JSONResponse({"error": "No indexer available"}, status_code=400)
    success = idx.delete_index()
    if success:
        return {"status": "deleted"}
    return JSONResponse({"error": "Failed to delete index"}, status_code=500)


@app.get("/api/indexer/progress")
async def get_indexer_progress(session_id: str = "") -> Dict[str, Any]:
    """获取索引器构建进度详情（供前端轮询）。"""
    state = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]

    idx = getattr(state, "indexer", None) if state else None
    if idx is None:
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return {"is_building": False, "ready": False, "file_count": 0,
                "stage": "idle", "scanned": 0, "summary_done": 0, "summary_total": 0}
    return idx.progress


@app.get("/api/agents-workflows")
async def get_agents_workflows() -> Dict[str, Any]:
    """获取已注册的 Agent 和已保存的工作流。"""
    agents = []
    try:
        from AutoRUN_v1.services.agent_registry import discover_agents
        agent_dict = discover_agents(refresh=True)
        for name, ad in agent_dict.items():
            agents.append({
                "name": name,
                "description": ad.get("description", ""),
                "model": ad.get("model", ""),
                "source": ad.get("_source", "unknown"),
            })
    except Exception:
        pass

    workflows = []
    try:
        import os, json
        workflows_dir = os.path.expanduser("~/.autorun/workflows")
        if os.path.isdir(workflows_dir):
            from pathlib import Path
            for f in sorted(Path(workflows_dir).glob("*.json")):
                if f.name.startswith("."):
                    continue
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    workflows.append({
                        "name": data.get("name", f.stem),
                        "description": data.get("description", ""),
                        "steps": len(data.get("steps", [])),
                        "steps_detail": data.get("steps", []),
                    })
                except Exception:
                    pass
    except Exception:
        pass

    return {"agents": agents, "workflows": workflows}


@app.get("/api/agents/status")
async def get_agents_status() -> Dict[str, Any]:
    """获取后台 Agent 运行状态。"""
    from AutoRUN_v1.tools.agent_tool import _background_tasks, _background_results
    active = []
    for sid, tasks in _background_tasks.items():
        for e in tasks:
            t = e["task"]
            active.append({
                "session_id": sid,
                "description": e.get("description", "?"),
                "done": t.done(),
                "cancelled": t.cancelled(),
            })
    completed = sum(len(v) for v in _background_results.values())
    return {"active": active, "active_count": len(active), "completed_count": completed}


@app.get("/api/token/usage")
async def get_token_usage(session_id: str = "") -> Dict[str, Any]:
    """获取四级 Token 消耗统计。

    Query parameter:
        session_id: 当前会话 ID（用于获取当前 WebSocket session 计数）

    Returns:
        {
            "session": 3456,        # 本次 WebSocket session 消耗
            "conversation": 12345,  # 当前对话历史消耗
            "project": 456789,      # 当前项目目录的所有消耗
            "global": 1234567       # 本机所有消耗
        }
    """
    project_dir = os.path.abspath(os.getcwd())
    data = _load_token_usage()

    # session: from in-memory (current WebSocket session)
    session_count = _session_tokens.get(session_id, 0) if session_id else 0

    # conversation: from persistent file
    conversation_count = 0
    if session_id:
        convs = data.get("conversations", {})
        conversation_count = convs.get(session_id, 0)

    # project: from persistent file
    projects = data.get("projects", {})
    project_count = projects.get(project_dir, 0)

    # global: from persistent file
    global_count = data.get("global", 0)

    return {
        "session": session_count,
        "conversation": conversation_count,
        "project": project_count,
        "global": global_count,
    }


@app.post("/api/chat")
async def chat_http(request: Request) -> Dict[str, Any]:
    """HTTP 聊天端点（无 WebSocket 备选方案）。

    适用于不支持 WebSocket 的环境。
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"type": "error", "error": "Invalid JSON"},
            status_code=400,
        )

    prompt = data.get("message", "").strip()
    if not prompt:
        return JSONResponse(
            {"type": "error", "error": "Empty message"},
            status_code=400,
        )

    session_id = data.get("session_id", str(uuid.uuid4()))

    try:
        from AutoRUN_v1.api.client import get_client
        from AutoRUN_v1.context import build_context_text, build_env_info, get_system_context, get_user_context
        from AutoRUN_v1.messages.types import create_user_message
        from AutoRUN_v1.messages.utils import normalize_messages_for_api
        from AutoRUN_v1.prompts.system_prompt import get_system_prompt

        model = get_model()
        user_context = await get_user_context()
        system_context = await get_system_context()
        system_prompt = await get_system_prompt(set(), model)
        env_info = build_env_info(model)

        full_prompt = "\n".join(system_prompt) if isinstance(system_prompt, list) else (system_prompt or "")
        full_prompt += "\n" + env_info

        # CoD context injection (silent)
        cod_state = _ws_states.get(session_id) if session_id else None
        if cod_state and hasattr(cod_state, 'cod_context') and cod_state.cod_context:
            cod_text = cod_state.cod_context
            cod_state.cod_context = ''
            full_prompt += (
                "\n\n[system]"
                "\nError in your previous output (hidden from user by CoD overlay):"
                f"\n{cod_text}"
                "\nContinue outputting the correct content. Do NOT mention CoD, do NOT apologize."
                "\n[/system]"
            )

        context_text = build_context_text(user_context, system_context)

        user_msg = create_user_message(context_text + "\n" + prompt)
        api_messages = normalize_messages_for_api([user_msg])

        client = get_client()
        full_response = ""

        async for event in client.stream_message(
            messages=api_messages,
            system_prompt=full_prompt,
            model=model,
        ):
            if event.get("type") == "text_delta":
                full_response += event.get("text", "")
            elif event.get("type") == "error":
                return {"type": "error", "error": event.get("error", "")}

        return {
            "type": "message_complete",
            "session_id": session_id,
            "full_text": full_response,
        }

    except Exception as e:
        return JSONResponse(
            {"type": "error", "error": str(e)},
            status_code=500,
        )


# ── File Tree API ────────────────────────────────────────────────────────────

# Directories / extensions to exclude from file tree
_EXCLUDED_DIRS = {
    "__pycache__", ".git", ".claude", "node_modules", ".venv", "venv",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "*.egg-info", "dist", "build", ".wrangler",
}
_EXCLUDED_EXTS = {
    # Only exclude build artifacts and compiled binaries
    ".pyc", ".pyo", ".so", ".dll", ".pyd", ".exe", ".bin",
}

# Changeable base directory for file tree
import os as _os
_base_dir: str = _os.getcwd()


def _resolve_path(subpath: str) -> str:
    """Resolve a subpath relative to the current base_dir, with security checks."""
    base = _os.path.abspath(_base_dir)
    target = _os.path.normpath(_os.path.join(base, subpath)) if subpath else base
    if not target.startswith(base):
        target = base  # prevent path escape
    return target


@app.get("/api/files/cwd")
async def get_cwd() -> Dict[str, Any]:
    """获取当前工作目录。"""
    return {"cwd": _os.path.abspath(_base_dir).replace("\\", "/")}


@app.post("/api/files/cwd")
async def set_cwd(request: Request) -> Dict[str, Any]:
    """更改文件树的工作目录。"""
    global _base_dir
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    new_path = data.get("path", "").strip()
    if not new_path:
        return JSONResponse({"error": "Empty path"}, status_code=400)

    expanded = _os.path.expanduser(new_path)
    abs_path = _os.path.abspath(expanded)

    # 如果路径指向的是文件，则切换到该文件所在的目录
    if _os.path.isfile(abs_path):
        abs_path = _os.path.dirname(abs_path)

    if not _os.path.isdir(abs_path):
        return JSONResponse({"error": f"目录不存在: {abs_path}"}, status_code=400)

    _base_dir = abs_path
    _os.chdir(abs_path)  # also change process cwd

    # Refresh skills for new directory (project-local skills may differ)
    try:
        from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool
        from AutoRUN_v1.state.app_state import get_app_state

        clear_skills_cache()
        state = get_app_state()
        # _get_disabled_skills() 负责首次访问时自动禁用非内置 skill
        ds = state._get_disabled_skills()
        discover_skills(refresh=True, disabled_skills=ds)
        register_skills_to_tool(disabled_skills=ds)

        # 清除已缓存的引擎，确保下次消息使用新目录的 skill 配置
        _ws_engines.clear()

        # 为新目录重新初始化索引器
        try:
            from AutoRUN_v1.services.indexer import FileIndexer
            new_idx = FileIndexer.reinit_for_cwd(abs_path, state=state)
            logger.debug("Indexer reinitialized for new CWD: %s (ready=%s, files=%d)",
                         abs_path, new_idx.is_ready, new_idx.file_count)
        except Exception:
            logger.debug("Indexer reinit after CWD change failed", exc_info=True)
    except Exception:
        logger.warning("Failed to refresh skills after CWD change", exc_info=True)

    return {"status": "ok", "cwd": abs_path.replace("\\", "/")}


@app.get("/api/files")
async def list_files(path: str = "") -> Dict[str, Any]:
    """返回项目文件树结构。"""
    cwd = _os.path.abspath(_base_dir)
    target = _resolve_path(path)

    if not _os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=400)

    items = []
    try:
        for name in sorted(_os.listdir(target), key=lambda n: (not _os.path.isdir(_os.path.join(target, n)), n.lower())):
            full = _os.path.join(target, name)
            is_dir = _os.path.isdir(full)

            if is_dir and (name.startswith(".") or name in _EXCLUDED_DIRS):
                continue
            if not is_dir:
                ext = _os.path.splitext(name)[1].lower()
                if ext in _EXCLUDED_EXTS:
                    continue

            try:
                stat = _os.stat(full)
                size = stat.st_size if not is_dir else 0
                items.append({
                    "name": name,
                    "path": _os.path.relpath(full, cwd).replace("\\", "/"),
                    "is_dir": is_dir,
                    "size": size,
                    "modified": int(stat.st_mtime),
                })
            except OSError:
                pass
    except OSError:
        return JSONResponse({"error": "Cannot read directory"}, status_code=500)

    return {"items": items, "path": path or "", "cwd": cwd.replace("\\", "/")}


@app.get("/api/file/content")
async def get_file_content(path: str = "") -> Dict[str, Any]:
    """读取项目文件内容。"""
    target = _resolve_path(path)

    if not _os.path.isfile(target):
        return JSONResponse({"error": "Not a file"}, status_code=404)

    # Limit file size to 500KB (text files) / 5MB (binary files detected below)
    fsize = _os.path.getsize(target)
    if fsize > 512000:
        return JSONResponse({"error": "File too large"}, status_code=400)

    try:
        # Detect binary files by checking for null bytes
        with open(target, "rb") as f:
            raw = f.read(8192)
        if b"\x00" in raw:
            ext = _os.path.splitext(target)[1].lower()
            return {
                "content": "",
                "path": path,
                "size": fsize,
                "binary": True,
                "type": ext or "binary",
            }
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"content": content, "path": path, "size": len(content)}
    except Exception:
        return JSONResponse({"error": "Cannot read file"}, status_code=500)


# ── Raw File / Save API ─────────────────────────────────────────────────────

# Known image/audio/video MIME types
_RAW_MIME_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".aac": "audio/aac", ".m4a": "audio/mp4",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".pdf": "application/pdf",
}


@app.get("/api/file/raw")
async def get_file_raw(path: str = ""):
    """返回原始文件字节（用于图片/音频预览和 Canvas 编辑）。"""
    target = _resolve_path(path)
    if not _os.path.isfile(target):
        return JSONResponse({"error": "Not a file"}, status_code=404)

    ext = _os.path.splitext(target)[1].lower()
    mime = _RAW_MIME_TYPES.get(ext, "application/octet-stream")

    # Limit binary file size to 50MB for raw serving
    fsize = _os.path.getsize(target)
    if fsize > 50 * 1024 * 1024:
        return JSONResponse({"error": "File too large for raw serving (>50MB)"}, status_code=400)

    try:
        from fastapi.responses import FileResponse
        return FileResponse(target, media_type=mime)
    except Exception:
        return JSONResponse({"error": "Cannot read file"}, status_code=500)


@app.post("/api/file/save")
async def save_file(request: Request) -> Dict[str, Any]:
    """保存编辑后的文件内容（图片 Canvas 导出、文本覆盖等）。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    path = (data.get("path") or "").strip()
    content = data.get("content", "")
    is_base64 = data.get("base64", False)

    if not path:
        return JSONResponse({"error": "Empty path"}, status_code=400)

    target = _resolve_path(path)
    parent = _os.path.dirname(target)
    if not _os.path.isdir(parent):
        return JSONResponse({"error": "Parent directory does not exist"}, status_code=400)

    try:
        if is_base64:
            import base64
            # Strip data URL prefix if present
            if isinstance(content, str) and "," in content:
                content = content.split(",", 1)[1]
            raw_bytes = base64.b64decode(content)
            with open(target, "wb") as f:
                f.write(raw_bytes)
        else:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        return {"status": "ok", "path": path, "size": _os.path.getsize(target)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── File Operations API ──────────────────────────────────────────────────────

@app.delete("/api/file")
async def delete_file(path: str = "") -> Dict[str, Any]:
    """删除文件或文件夹（需要用户确认由前端处理）。"""
    if not path:
        return JSONResponse({"error": "Empty path"}, status_code=400)
    target = _resolve_path(path)

    if not _os.path.exists(target):
        return JSONResponse({"error": "Not found"}, status_code=404)

    try:
        if _os.path.isdir(target):
            import shutil
            shutil.rmtree(target)
        else:
            _os.remove(target)
        return {"status": "deleted", "path": path}
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/file/reveal")
async def reveal_in_file_manager(request: Request) -> Dict[str, Any]:
    """在系统文件管理器中定位到文件/文件夹。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    path = data.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "Empty path"}, status_code=400)

    target = _resolve_path(path)
    if not _os.path.exists(target):
        return JSONResponse({"error": "Not found"}, status_code=404)

    try:
        import subprocess
        import platform
        abs_path = _os.path.abspath(target)
        system = platform.system()

        if system == "Windows":
            # If file, use explorer /select to highlight it
            if _os.path.isfile(abs_path):
                subprocess.Popen(["explorer", "/select,", abs_path])
            else:
                subprocess.Popen(["explorer", abs_path])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R" if _os.path.isfile(abs_path) else "", abs_path])
        else:  # Linux
            dir_path = _os.path.dirname(abs_path) if _os.path.isfile(abs_path) else abs_path
            subprocess.Popen(["xdg-open", dir_path])

        return {"status": "ok", "path": path}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/indexer/file")
async def get_file_index(path: str = "", session_id: str = "") -> Dict[str, Any]:
    """获取单个文件的索引摘要信息。"""
    if not path:
        return JSONResponse({"error": "Empty path"}, status_code=400)

    target = _resolve_path(path)

    # 优先从指定会话获取索引器
    idx = None
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]
        idx = getattr(state, "indexer", None)
    if idx is None:
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return {"exists": False, "ready": False, "summary": None}

    if not idx.is_ready:
        return {"exists": True, "ready": False, "file_count": idx.file_count, "found": False}

    # Look up the file: manifest.files[rel_path] -> md5 -> summaries[md5]
    summary_info = None
    found = False
    try:
        manifest = idx._manifest
        summaries = idx._summaries
        rel_path = _os.path.relpath(target, _os.path.abspath(_base_dir)).replace("\\", "/")
        if manifest and rel_path in manifest.files:
            entry = manifest.files[rel_path]
            if entry.md5 in summaries:
                s = summaries[entry.md5]
                summary_info = {
                    "dependencies": s.dependencies,
                    "functionality": s.functionality,
                    "logic": s.logic,
                    "notes": s.notes,
                    "relationships": s.relationships,
                }
            found = True
    except Exception:
        pass

    return {
        "exists": True,
        "ready": idx.is_ready,
        "file_count": idx.file_count,
        "summary": summary_info,
        "found": found,
    }


@app.post("/api/indexer/file")
async def rebuild_file_index(request: Request) -> Dict[str, Any]:
    """为单个文件/文件夹重建索引摘要。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    path = data.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "Empty path"}, status_code=400)

    target = _resolve_path(path)
    if not _os.path.exists(target):
        return JSONResponse({"error": "Not found"}, status_code=404)

    # 优先从指定会话获取索引器
    idx = None
    session_id = data.get("session_id", "")
    if session_id and session_id in _ws_states:
        state = _ws_states[session_id]
        idx = getattr(state, "indexer", None)
    if idx is None:
        for s in _ws_states.values():
            idx = getattr(s, "indexer", None)
            if idx is not None:
                break
    if idx is None:
        return JSONResponse({"error": "No indexer available"}, status_code=400)

    # Force reindex: mark indexer as needing rebuild, then trigger build
    try:
        # Invalidate cached entry for this file to force re-summarization
        manifest = idx._manifest
        if manifest:
            rel_path = _os.path.relpath(target, _os.path.abspath(_base_dir)).replace("\\", "/")
            if rel_path in manifest.files:
                entry = manifest.files[rel_path]
                # Remove cached summary to force regeneration
                if entry.md5 in idx._summaries:
                    del idx._summaries[entry.md5]
        idx.mark_user_response(accepted=True)
        return {"status": "building", "path": path}
    except AttributeError:
        idx.mark_user_response(accepted=True)
        return {"status": "building", "path": path, "note": "full_rebuild_fallback"}


# ── Task Management API ────────────────────────────────────────────────────

@app.get("/api/tasks")
async def get_tasks() -> Dict[str, Any]:
    """返回当前所有活跃任务（TaskCreate/TaskUpdate 管理的任务）。"""
    from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display
    tasks = get_all_tasks_for_display(None)
    return {"tasks": tasks, "count": len(tasks)}


# ── Skill Management API ────────────────────────────────────────────────────

_SKILLS_DIR = os.path.expanduser("~/.autorun/skills")
_BUNDLED_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "bundled")


@app.get("/api/skills")
async def list_skills() -> Dict[str, Any]:
    """列出所有可用的 skill（用户 + 内置）。"""
    skills = []

    # 1. Bundled skills
    if os.path.isdir(_BUNDLED_SKILLS_DIR):
        _collect_skills(skills, _BUNDLED_SKILLS_DIR, "bundled")

    # 2. User skills
    if os.path.isdir(_SKILLS_DIR):
        _collect_skills(skills, _SKILLS_DIR, "user")

    return {"skills": skills, "count": len(skills)}


def _collect_skills(skills: list, directory: str, source: str) -> None:
    """从目录收集 skill 信息。"""
    import json as _json

    for name in sorted(os.listdir(directory)):
        if name.startswith("."):
            continue
        full = os.path.join(directory, name)
        try:
            if name.endswith(".json") and os.path.isfile(full):
                with open(full, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                skills.append({
                    "name": data.get("name", name[:-5]),
                    "type": data.get("type", "?"),
                    "file": name,
                    "source": source,
                    "description": data.get("description", ""),
                    "has_prompt": bool(data.get("prompt")),
                })
            elif name.endswith(".md") and os.path.isfile(full):
                skills.append({
                    "name": name[:-3],
                    "type": "prompt",
                    "file": name,
                    "source": source,
                    "description": f"Markdown skill",
                    "has_prompt": True,
                })
        except Exception:
            skills.append({
                "name": name.rsplit(".", 1)[0],
                "type": "?",
                "file": name,
                "source": source,
                "description": "(无法解析)",
                "has_prompt": False,
            })


@app.post("/api/skills")
async def create_skill(request: Request) -> Dict[str, Any]:
    """创建或更新一个用户 skill。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    name = (data.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"error": "Skill name is required"}, status_code=400)

    os.makedirs(_SKILLS_DIR, exist_ok=True)

    skill_def = {
        "name": name,
        "type": data.get("type", "prompt"),
        "description": (data.get("description", "") or "").strip(),
        "prompt": (data.get("prompt", "") or "").strip(),
    }

    out_path = os.path.join(_SKILLS_DIR, f"{name}.json")
    exists = os.path.isfile(out_path)

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(skill_def, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {
        "status": "ok",
        "action": "updated" if exists else "created",
        "file": out_path,
        "skill": skill_def,
    }


# ── Skill Drafts API ─────────────────────────────────────────────────────────

_DRAFTS_DIR = os.path.join(_SKILLS_DIR, "drafts")


@app.get("/api/skills/drafts")
async def list_drafts() -> Dict[str, Any]:
    """列出所有草稿。"""
    import time as _time

    drafts = []
    if os.path.isdir(_DRAFTS_DIR):
        for name in sorted(os.listdir(_DRAFTS_DIR), reverse=True):
            if not name.endswith(".json"):
                continue
            path = os.path.join(_DRAFTS_DIR, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                draft_id = name[:-5]
                prompt = data.get("prompt", "")
                drafts.append({
                    "id": draft_id,
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "prompt_preview": prompt[:80] + ("..." if len(prompt) > 80 else ""),
                    "updated_at": data.get("updated_at", ""),
                })
            except Exception:
                logger.debug("Failed to load draft file: %s", name, exc_info=True)
    return {"drafts": drafts, "count": len(drafts)}


@app.post("/api/skills/drafts")
async def save_draft(request: Request) -> Dict[str, Any]:
    """保存或更新草稿。"""
    import time as _time

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    draft_id = (data.get("draft_id") or "").strip()
    os.makedirs(_DRAFTS_DIR, exist_ok=True)

    if draft_id:
        path = os.path.join(_DRAFTS_DIR, f"{draft_id}.json")
        exists = os.path.isfile(path)
    else:
        draft_id = uuid.uuid4().hex[:8]
        path = os.path.join(_DRAFTS_DIR, f"{draft_id}.json")
        exists = False

    draft = {
        "name": (data.get("name") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "prompt": (data.get("prompt") or "").strip(),
        "updated_at": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime()),
    }

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {
        "status": "ok",
        "action": "updated" if exists else "created",
        "draft_id": draft_id,
        "draft": draft,
    }


@app.get("/api/skills/drafts/{draft_id}")
async def get_draft(draft_id: str) -> Dict[str, Any]:
    """获取单个草稿的完整内容。"""
    path = os.path.join(_DRAFTS_DIR, f"{draft_id}.json")
    if not os.path.isfile(path):
        return JSONResponse({"error": f"Draft not found: {draft_id}"}, status_code=404)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"draft": data, "draft_id": draft_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/skills/drafts")
async def clear_all_drafts() -> Dict[str, Any]:
    """清空所有草稿。"""
    if not os.path.isdir(_DRAFTS_DIR):
        return {"status": "ok", "deleted_count": 0}

    count = 0
    for name in os.listdir(_DRAFTS_DIR):
        if name.endswith(".json"):
            try:
                os.remove(os.path.join(_DRAFTS_DIR, name))
                count += 1
            except OSError:
                logger.debug("Failed to delete draft file: %s", name, exc_info=True)
    return {"status": "ok", "deleted_count": count}


@app.delete("/api/skills/drafts/{draft_id}")
async def delete_draft(draft_id: str) -> Dict[str, Any]:
    """删除单个草稿。"""
    path = os.path.join(_DRAFTS_DIR, f"{draft_id}.json")
    if not os.path.isfile(path):
        return JSONResponse({"error": f"Draft not found: {draft_id}"}, status_code=404)
    try:
        os.remove(path)
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"status": "ok", "action": "deleted", "draft_id": draft_id}




# ── Skill Toggle API ───────────────────────────────────────────────────────


@app.post("/api/skills/toggle")
async def toggle_skill_api(request: Request) -> Dict[str, Any]:
    """Toggle a skill's enabled state."""
    from AutoRUN_v1.state.app_state import get_app_state
    from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "error": "Invalid JSON body"}

    name = body.get("name", "").strip()
    enabled = body.get("enabled", True)

    if not name:
        return {"status": "error", "error": "Skill name is required"}

    state = get_app_state()

    # Check if skill exists
    all_skills = discover_skills(refresh=True)
    if name not in all_skills:
        return {"status": "error", "error": f"Skill '{name}' not found"}

    if enabled:
        state.enable_skill(name)
    else:
        state.disable_skill(name)

    clear_skills_cache()
    discover_skills(refresh=True, disabled_skills=state._get_disabled_skills())
    register_skills_to_tool(disabled_skills=state._get_disabled_skills())

    # 清除所有已缓存的引擎，让下次消息重新初始化以获取最新 skill 列表
    _ws_engines.clear()

    return {
        "status": "ok",
        "skill": name,
        "enabled": enabled,
        "disabled_skills": sorted(state._get_disabled_skills()),
    }


@app.get("/api/skills/toggle-state")
async def get_skills_toggle_state() -> Dict[str, Any]:
    """Get current skill toggle states."""
    from AutoRUN_v1.state.app_state import get_app_state
    from AutoRUN_v1.skills.loader import discover_skills

    state = get_app_state()
    all_skills = discover_skills(refresh=True)
    disabled = state._get_disabled_skills()

    skills_state = {}
    for name in sorted(all_skills.keys()):
        skills_state[name] = not (name in disabled)

    return {
        "status": "ok",
        "skills_state": skills_state,
        "disabled_skills": sorted(disabled),
    }


@app.get("/api/skills/{skill_name}")
async def get_skill(skill_name: str) -> Dict[str, Any]:
    """获取特定 skill 的完整内容。"""
    json_path = os.path.join(_SKILLS_DIR, f"{skill_name}.json")
    md_path = os.path.join(_SKILLS_DIR, f"{skill_name}.md")

    for path, is_json in [(json_path, True), (md_path, False)]:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if is_json:
                        data = json.load(f)
                        return {"skill": data, "file": os.path.basename(path), "source": "user"}
                    else:
                        content = f.read()
                        return {
                            "skill": {
                                "name": skill_name,
                                "type": "prompt",
                                "description": "Markdown skill",
                                "prompt": content,
                            },
                            "file": os.path.basename(path),
                            "source": "user",
                        }
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

    # Also check bundled skills
    bundled_path = os.path.join(_BUNDLED_SKILLS_DIR, f"{skill_name}.json")
    if os.path.isfile(bundled_path):
        try:
            with open(bundled_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {"skill": data, "file": f"{skill_name}.json", "source": "bundled"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"error": f"Skill not found: {skill_name}"}, status_code=404)


@app.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str) -> Dict[str, Any]:
    """删除一个用户 skill。"""
    deleted = []
    for ext in (".json", ".md"):
        path = os.path.join(_SKILLS_DIR, f"{skill_name}{ext}")
        if os.path.isfile(path):
            try:
                os.remove(path)
                deleted.append(path)
            except OSError as e:
                return JSONResponse({"error": str(e)}, status_code=500)

    if not deleted:
        return JSONResponse({"error": f"Skill not found: {skill_name}"}, status_code=404)

    return {"status": "ok", "action": "deleted", "files": deleted}


# ── Conversations API ──────────────────────────────────────────────────────


@app.get("/api/conversations")
async def list_conversations_api(filter: str = "all") -> Dict[str, Any]:
    """列出已保存的对话。filter=project 只显示当前项目。"""
    from AutoRUN_v1.services.conversations import list_conversations

    cwd = os.getcwd() if filter == "project" else None
    try:
        convs = list_conversations(cwd_filter=cwd)
    except RuntimeError as e:
        return {"status": "error", "error": str(e)}
    return {"status": "ok", "conversations": convs}


@app.get("/api/conversations/search")
async def search_conversations_api(q: str = "") -> Dict[str, Any]:
    """搜索对话。"""
    from AutoRUN_v1.services.conversations import search_conversations

    if not q.strip():
        return {"status": "ok", "conversations": []}
    try:
        results = search_conversations(q.strip())
    except RuntimeError as e:
        return {"status": "error", "error": str(e)}
    return {"status": "ok", "conversations": results}


@app.post("/api/conversations/load")
async def load_conversation_api(request: Request) -> Dict[str, Any]:
    """加载指定对话到当前会话。"""
    from AutoRUN_v1.services.conversations import restore_to_state, load_conversation
    from AutoRUN_v1.state.app_state import get_app_state

    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "error": "Invalid JSON body"}

    session_id = body.get("session_id", "")
    if not session_id:
        return {"status": "error", "error": "session_id is required"}

    state = get_app_state()
    ok = restore_to_state(session_id, state)
    if not ok:
        return {"status": "error", "error": "Conversation not found"}

    # Store restored state for subsequent WebSocket chat messages
    _ws_states[session_id] = state

    # Refresh skills
    try:
        from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool
        clear_skills_cache()
        discover_skills(refresh=True, disabled_skills=state._get_disabled_skills())
        register_skills_to_tool(disabled_skills=state._get_disabled_skills())
    except Exception:
        logger.warning("Failed to refresh skills after conversation load", exc_info=True)

    try:
        data = load_conversation(session_id)
    except RuntimeError:
        data = None

    # Serialize restored messages for frontend display
    serialized_messages = []
    for msg in state.get_messages():
        if hasattr(msg, 'to_dict'):
            serialized_messages.append(msg.to_dict())
        elif isinstance(msg, dict):
            serialized_messages.append(msg)

    return {
        "status": "ok",
        "session_id": session_id,
        "message_count": len(state.get_messages()),
        "model": state.model,
        "project": data.get("project_name", "") if data else "",
        "messages": serialized_messages,
    }


@app.delete("/api/conversations/{session_id}")
async def delete_conversation_api(session_id: str) -> Dict[str, Any]:
    """删除指定对话。"""
    from AutoRUN_v1.services.conversations import delete_conversation

    ok = delete_conversation(session_id)
    return {"status": "ok" if ok else "error", "deleted": ok}


# ── 工作流触发器 API ────────────────────────────────────────────────────────
# 外部程序可通过这些 REST 端点触发已定义的工作流。
# 支持任意语言调用（curl, Python requests, node.js, Go 等）。


@app.get("/api/triggers")
async def list_triggers_api() -> Dict[str, Any]:
    """列出所有已定义的触发器。"""
    from AutoRUN_v1.services.workflow_triggers import list_triggers, get_trigger_status
    triggers = list_triggers()
    status = get_trigger_status()
    return {
        "triggers": triggers,
        "active_count": status["active_count"],
        "active_names": status["active_names"],
    }


@app.post("/api/triggers/{trigger_name}/fire")
async def fire_trigger_api(trigger_name: str, request: Request) -> Dict[str, Any]:
    """调用触发器，启动对应的工作流。

    外部程序示例 (curl):
      curl -X POST http://localhost:8080/api/triggers/git-push-trigger/fire \\
        -H "Content-Type: application/json" \\
        -d '{"target": "public"}'

    返回 trigger_id 用于追踪工作流执行状态。
    """
    from AutoRUN_v1.services.workflow_triggers import fire_trigger_by_name

    # 解析可选的请求体
    meta = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            meta = body
    except Exception:
        pass  # 允许无请求体

    trigger_id = await fire_trigger_by_name(trigger_name, meta=meta)
    if trigger_id is None:
        return JSONResponse(
            {"error": f"未找到触发器 '{trigger_name}'，或它不是 call 类型"},
            status_code=404,
        )

    return {
        "status": "ok",
        "trigger_name": trigger_name,
        "trigger_id": trigger_id,
        "message": f"工作流已触发，trigger_id={trigger_id}",
    }


@app.get("/api/triggers/{trigger_name}")
async def get_trigger_api(trigger_name: str) -> Dict[str, Any]:
    """获取指定触发器的定义。"""
    from AutoRUN_v1.services.workflow_triggers import get_trigger
    trigger = get_trigger(trigger_name)
    if trigger is None:
        return JSONResponse(
            {"error": f"未找到触发器 '{trigger_name}'"},
            status_code=404,
        )
    return {"trigger": trigger}


@app.get("/api/workflows")
async def list_workflows_api() -> Dict[str, Any]:
    """列出所有已保存的工作流（供外部程序查询可用工作流）。"""
    import os, json
    from pathlib import Path
    workflows_dir = os.path.expanduser("~/.autorun/workflows")
    if not os.path.isdir(workflows_dir):
        return {"workflows": []}

    workflows = []
    for f in sorted(Path(workflows_dir).glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            workflows.append({
                "name": data.get("name", f.stem),
                "description": data.get("description", ""),
                "steps_count": len(data.get("steps", [])),
            })
        except (json.JSONDecodeError, OSError):
            pass

    return {"workflows": workflows}


@app.get("/api/workflows/{workflow_name}")
async def get_workflow_api(workflow_name: str) -> Dict[str, Any]:
    """获取指定工作流的完整定义（供外部程序查询）。"""
    import os, json
    path = os.path.expanduser(f"~/.autorun/workflows/{workflow_name}.json")
    if not os.path.isfile(path):
        return JSONResponse(
            {"error": f"未找到工作流 '{workflow_name}'"},
            status_code=404,
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {"workflow": data}


# ── 用户模型持久化 API ────────────────────────────────────────────────────
# 模型存储在 config.json 的 models 数组中，由 config.py 统一管理


@app.get("/api/user-models")
async def get_user_models() -> Dict[str, Any]:
    """获取用户自定义模型列表。"""
    from AutoRUN_v1.utils.config import get_models
    return {"models": get_models()}


@app.post("/api/user-models")
async def save_user_models(request: Request) -> Dict[str, Any]:
    """保存用户自定义模型列表（完整替换）。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    models = data.get("models", [])
    if not isinstance(models, list):
        return JSONResponse({"error": "models must be an array"}, status_code=400)
    from AutoRUN_v1.utils.config import save_models
    save_models(models)
    return {"status": "ok", "count": len(models)}


# ── WebSocket Sessions ──────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket 端点：实时聊天。"""
    await websocket.accept()

    session_id = str(uuid.uuid4())
    _ws_sessions[session_id] = websocket

    try:
        # 欢迎消息（携带完整配置状态）
        cfg = check_config()
        await websocket.send_json({
            "type": "connected",
            "session_id": session_id,
            "model": get_model(),
            "api_type": get_api_type(),
            "api_configured": cfg["ok"],
            "config_error": cfg.get("error", ""),
        })

        # 启动 Agent 状态轮询
        agent_poll_cancel = asyncio.Event()
        agent_poll_task = asyncio.create_task(
            _poll_agent_status(websocket, session_id, agent_poll_cancel)
        )
        _agent_poll_tasks[session_id] = agent_poll_task

        # 注册 Agent 完成回调 — 子Agent完成时自动触发门控处理
        # （只注册一个回调，避免竞态）
        from AutoRUN_v1.tools.agent_tool import register_agent_done_callback

        def _on_agent_done_callback(done_session_id: str, agent_id: str, result_str: str):
            """Called from agent_tool._on_done when a background agent completes.
            Uses closure-captured session_id to ensure correct routing."""
            try:
                asyncio.create_task(
                    _maybe_auto_trigger_agent_results(websocket, session_id)
                )
            except Exception:
                logger.debug("Failed to schedule auto-trigger for session %s", session_id, exc_info=True)

        register_agent_done_callback(session_id, _on_agent_done_callback)

        # 注册流式回调：子Agent 流式输出转发到前端
        from AutoRUN_v1.tools.agent_tool import register_stream_callback

        def _on_agent_stream(done_session_id: str, agent_id: str, event: dict):
            """转发子 Agent 流式事件到 WebSocket。"""
            # Use closure-captured session_id; ignore done_session_id
            try:
                evt = dict(event)
                evt["agent_id"] = agent_id
                evt["type"] = "agent_stream"
                asyncio.create_task(websocket.send_json(evt))
            except Exception:
                logger.debug("Agent stream event send failed", exc_info=True)

        register_stream_callback(session_id, _on_agent_stream)

        # 预初始化引擎（使 indexer 在页面加载时即可用）
        try:
            from AutoRUN_v1.query_engine import QueryEngine
            from AutoRUN_v1.state.app_state import AppState
            state = AppState()
            state.session_id = session_id
            engine = QueryEngine(state)
            await engine.initialize()
            _ws_states[session_id] = state
            _ws_engines[session_id] = engine
        except Exception:
            logger.debug("Engine pre-init skipped", exc_info=True)

        # 发送当前任务列表（页面刷新恢复）
        try:
            from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display
            tasks = get_all_tasks_for_display(None)
            if tasks:
                await websocket.send_json({
                    "type": "task_update",
                    "tasks": tasks,
                })
        except Exception:
            logger.debug("Failed to send initial task list to WebSocket", exc_info=True)

        # 主消息循环
        while True:
            try:
                data = await websocket.receive_json()
                message_type = data.get("type", "")

                if message_type == "chat":
                    # If a task is already running, queue this message instead of cancelling
                    prev_task = _ws_tasks.get(session_id)
                    if prev_task is not None and not prev_task.done():
                        queued = _ws_queued.setdefault(session_id, [])
                        # Ensure message has an ID for frontend tracking
                        msg_id = data.get("message_id", str(uuid.uuid4()))
                        data["message_id"] = msg_id
                        queued.append(data)
                        logger.warning(f"QUEUED: sid={session_id}, pos={len(queued)}, msg_id={msg_id}, msg={data.get('message','')[:50]}")
                        await websocket.send_json({
                            "type": "queued",
                            "session_id": session_id,
                            "message_id": msg_id,
                            "position": len(queued),
                            "message_preview": data.get("message", "")[:80],
                        })
                        continue
                    # Run as background task so WebSocket can still receive messages (e.g. stop, queued)
                    cancel_event = asyncio.Event()
                    _ws_cancel_events[session_id] = cancel_event
                    task = asyncio.create_task(
                        _handle_chat_message(websocket, data, cancel_event)
                    )
                    _ws_tasks[session_id] = task
                elif message_type == "stop":
                    # Cancel the ongoing chat generation
                    if session_id in _ws_cancel_events:
                        _ws_cancel_events[session_id].set()
                    await websocket.send_json({"type": "message_complete", "session_id": session_id})
                elif message_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif message_type == "list_conversations":
                    from AutoRUN_v1.services.conversations import list_conversations as lc
                    cwd = os.getcwd() if data.get("filter") == "project" else None
                    try:
                        convs = lc(cwd_filter=cwd)
                        await websocket.send_json({"type": "conversations", "conversations": convs})
                    except RuntimeError as e:
                        await websocket.send_json({"type": "error", "error": str(e)})
                elif message_type == "search_conversations":
                    from AutoRUN_v1.services.conversations import search_conversations as sc
                    try:
                        results = sc(data.get("query", ""))
                        await websocket.send_json({"type": "conversations", "conversations": results})
                    except RuntimeError as e:
                        await websocket.send_json({"type": "error", "error": str(e)})
                elif message_type == "load_conversation":
                    await _handle_load_conversation(websocket, data)
                elif message_type == "skill_toggle":
                    await _handle_skill_toggle(websocket, data)
                elif message_type == "agent_user_message":
                    # User sends a message to a specific sub-agent, CC gating agent
                    await _handle_agent_user_message(websocket, data, session_id)
                elif message_type == "agent_pref":
                    # Multi-agent checkbox toggled — update state, invalidate engine
                    pref = bool(data.get("agentPref", True))
                    if session_id in _ws_states:
                        _ws_states[session_id].agent_pref = pref
                        # Invalidate cached engine so system prompt is rebuilt
                        if session_id in _ws_engines:
                            del _ws_engines[session_id]
                elif message_type == "slash":
                    command = data.get("command", "")
                    await websocket.send_json({
                        "type": "system",
                        "text": f"命令: {command}",
                    })
                elif message_type == "cancel_queued":
                    # Frontend wants to cancel a specific queued message
                    msg_id = data.get("message_id", "")
                    if session_id in _ws_queued:
                        before_count = len(_ws_queued[session_id])
                        _ws_queued[session_id] = [
                            item for item in _ws_queued[session_id]
                            if item.get("message_id") != msg_id
                        ]
                        after_count = len(_ws_queued[session_id])
                        if not _ws_queued[session_id]:
                            del _ws_queued[session_id]
                        logger.warning(f"CANCEL_QUEUED: sid={session_id}, msg_id={msg_id}, removed={before_count - after_count}")
                        await _send_queue_update(websocket, session_id)
                elif message_type == "cod_detected":
                    # Frontend detected error in AI output — inject CoD correction context
                    cod_error = data.get("error_text", "")[:500]
                    logger.warning(f"CoD: sid={session_id}, error_snippet={cod_error[:80]}")
                    # Store CoD context for next engine rebuild
                    if session_id in _ws_states:
                        state = _ws_states[session_id]
                        prev = getattr(state, 'cod_context', '') or ''
                        state.cod_context = (prev + '\n' + cod_error)[-2000:]
                        # Invalidate engine to rebuild with CoD context
                        if session_id in _ws_engines:
                            del _ws_engines[session_id]
                else:
                    await websocket.send_json({
                        "type": "error",
                        "error": f"未知消息类型: {message_type}",
                    })

            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "error": "无效的 JSON",
                })

    except WebSocketDisconnect:
        logger.info(f"WebSocket 会话 {session_id} 已断开")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
    finally:
        # Stop agent poll task
        if session_id in _agent_poll_tasks:
            poll_task = _agent_poll_tasks.pop(session_id)
            agent_poll_cancel.set()
            try:
                poll_task.cancel()
            except Exception:
                pass
        _ws_sessions.pop(session_id, None)
        _ws_states.pop(session_id, None)
        _ws_engines.pop(session_id, None)
        _ws_cancel_events.pop(session_id, None)
        _ws_tasks.pop(session_id, None)
        _ws_queued.pop(session_id, None)
        _agent_last_status.pop(session_id, None)
        _agent_outputs.pop(session_id, None)
        # Clean up auto-trigger lock
        # Clean up auto-trigger callback
        try:
            from AutoRUN_v1.tools.agent_tool import unregister_agent_done_callbacks
            unregister_agent_done_callbacks(session_id)
        except Exception:
            pass
        # Clean up stream callbacks
        try:
            from AutoRUN_v1.tools.agent_tool import unregister_stream_callbacks
            unregister_stream_callbacks(session_id)
        except Exception:
            pass


async def _handle_chat_message(websocket: WebSocket, data: Dict[str, Any], cancel_event: asyncio.Event = None) -> None:
    """处理 WebSocket 聊天消息（使用 QueryEngine 支持工具调用）。"""
    from AutoRUN_v1.query_engine import QueryEngine
    from AutoRUN_v1.state.app_state import AppState

    prompt = data.get("message", "").strip()
    if not prompt:
        await websocket.send_json({"type": "error", "error": "空消息"})
        return

    # 获取或创建会话状态
    session_id = data.get("session_id", "")
    if not session_id:
        session_id = str(uuid.uuid4())

    if session_id in _ws_states:
        state = _ws_states[session_id]
    else:
        state = AppState()
        state.session_id = session_id
        _ws_states[session_id] = state

    # Store agent delegation preference from frontend
    if "agentPref" in data:
        state.agent_pref = bool(data["agentPref"])

    # Track whether task-related tools were called this message
    _task_tools_seen = set()
    # Track whether skill-related tools were called (needs restart prompt)
    _skill_tool_seen = False
    # Track whether generation was cancelled
    _was_cancelled = False

    async def _maybe_send_task_update():
        """If any task tools were used, send updated task list to frontend."""
        if _task_tools_seen:
            from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display
            tasks = get_all_tasks_for_display(state)
            await websocket.send_json({
                "type": "task_update",
                "tasks": tasks,
            })

    try:
        # Reuse cached engine per session; only initialize on first message or after invalidation
        if session_id in _ws_engines:
            engine = _ws_engines[session_id]
            # Update state reference (messages may have been restored via load_conversation)
            engine.state = state
        else:
            engine = QueryEngine(state)
            await engine.initialize()
            _ws_engines[session_id] = engine

        _last_sent_text = ""  # track accumulated text for delta computation

        # ── Inject completed background Agent results ──
        from AutoRUN_v1.tools.agent_tool import drain_background_results, _background_results
        # Save completed agents snapshot before draining
        completed_agents_strs = list(_background_results.get(session_id, []))
        bg_results = drain_background_results(session_id)
        is_auto_trigger = (prompt == "__AUTO_TRIGGER__")
        if bg_results:
            if is_auto_trigger:
                prompt = (
                    "以下子 Agent 已在后台完成工作，请汇总结果给用户：\n\n"
                    + bg_results
                    + "\n\n请用自己的话简洁总结子 Agent 完成的工作，不要直接复制子 Agent 的输出。"
                )
            else:
                prompt = (
                    "以下子 Agent 已在后台完成工作，请汇总结果给用户：\n\n"
                    + bg_results +
                    "\n\n---\n用户原始消息:\n" + prompt +
                    "\n\n请优先回应用户的消息，如需提及子 Agent 的结果，用自己的话简洁总结。"
                )
            try:
                current = _get_agent_status_snapshot(session_id)
                _agent_last_status[session_id] = current
                await websocket.send_json({
                    "type": "agent_status",
                    "agents": current,
                    "session_id": session_id,
                })
                # Also send agent_output for each completed agent
                for result_str in completed_agents_strs:
                    desc = "Sub-Agent"
                    if result_str.startswith("[Agent 结果: "):
                        desc = result_str[len("[Agent 结果: "):].split("]", 1)[0]
                    elif result_str.startswith("[Agent 错误: "):
                        desc = result_str[len("[Agent 错误: "):].split("]", 1)[0]
                    elif result_str.startswith("[Agent 已取消: "):
                        desc = result_str[len("[Agent 已取消: "):].split("]", 1)[0]
                    # 只发送纯结果内容（跳过 [Agent 结果: desc] 头部），且截断到 3000 字符
                    result_parts = result_str.split("\n", 1)
                    result_text = result_parts[1] if len(result_parts) > 1 else result_str
                    result_text = result_text[:3000]
                    await _send_agent_output(
                        websocket, session_id, desc,
                        result_text, is_partial=False
                    )
            except Exception:
                logger.debug("Failed to send agent completion output", exc_info=True)

        # Auto-trigger with no results: silently complete (results may have been drained by another handler)
        if is_auto_trigger and not bg_results:
            await websocket.send_json({
                "type": "message_complete",
                "session_id": session_id,
            })
            return

        async for event in engine.send_message(prompt):
            # Check for cancellation
            if cancel_event and cancel_event.is_set():
                _was_cancelled = True
                break

            event_type = event.get("type", "")

            if event_type == "assistant":
                content = event.get("content", [])
                is_partial = event.get("is_partial", False)

                if is_partial:
                    # Partial events: streaming text deltas for incremental UI display.
                    # Content carries accumulated text; send only the new portion.
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            full_text = block.get("text", "")
                            delta = full_text[len(_last_sent_text):]
                            if delta:
                                await websocket.send_json({
                                    "type": "text_delta",
                                    "text": delta,
                                })
                                _record_tokens(session_id, "主Agent", delta)
                            _last_sent_text = full_text
                else:
                    # Complete event: text was already streamed via partial events.
                    # Only emit tool_use blocks; skip text blocks entirely.
                    _last_sent_text = ""
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            await websocket.send_json({
                                "type": "tool_use",
                                "tool_name": tool_name,
                                "tool_input": block.get("input", {}),
                            })
                            # Track task-related tools
                            if tool_name in ("TaskCreate", "TaskUpdate"):
                                _task_tools_seen.add(tool_name)
                            # Track skill-related tools
                            if tool_name in ("Skill", "SkillManage"):
                                _skill_tool_seen = True
                            # Track agent tools — send immediate status update
                            if tool_name in ("Agent", "agent_tool"):
                                try:
                                    current = _get_agent_status_snapshot(session_id)
                                    _agent_last_status[session_id] = current
                                    await websocket.send_json({
                                        "type": "agent_status",
                                        "agents": current,
                                        "session_id": session_id,
                                    })
                                except Exception:
                                    pass

            elif event_type == "user":
                for block in event.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_text = str(block.get("content", ""))
                        if len(result_text) > 1000:
                            result_text = result_text[:1000] + "..."
                        await websocket.send_json({
                            "type": "tool_result",
                            "content": result_text,
                            "is_error": block.get("is_error", False),
                        })
                # After tool results processed, update task panel
                await _maybe_send_task_update()

            elif event_type == "error":
                await websocket.send_json({
                    "type": "error",
                    "error": event.get("error", ""),
                })
                break

            elif event_type == "terminal":
                pass

        # Send final task update (only if not cancelled)
        if not _was_cancelled:
            await _maybe_send_task_update()

            # ── Drain queued messages (extracted as local function) ──────────
            async def _drain_queued():
                """Process all queued messages. Safe to call even if engine is not ready."""
                nonlocal _skill_tool_seen, _was_cancelled
                try:
                    # Guard: if engine not available, just clear the queue
                    engine  # will raise NameError if not defined
                except NameError:
                    _ws_queued.pop(session_id, None)
                    return
                try:
                    logger.warning(f"DRAIN_CHECK: sid={session_id}, in_dict={session_id in _ws_queued}, count={len(_ws_queued.get(session_id, []))}, cancelled={_was_cancelled}")
                    while session_id in _ws_queued and _ws_queued[session_id]:
                        # Check for cancellation before processing next queued message
                        if cancel_event and cancel_event.is_set():
                            _was_cancelled = True
                            break

                        # Get the next queued message
                        qlist = _ws_queued[session_id]
                        next_data = qlist.pop(0)
                        q_msg_id = next_data.get("message_id", "")
                        q_prompt = next_data.get("message", "").strip()

                        if not qlist:
                            del _ws_queued[session_id]

                        if not q_prompt:
                            # Still send queue update for remaining messages
                            await _send_queue_update(websocket, session_id)
                            continue

                        # Notify frontend that we're processing this specific queued message
                        await websocket.send_json({
                            "type": "queued_processing",
                            "session_id": session_id,
                            "message_id": q_msg_id,
                            "message_text": q_prompt,
                            "remaining": len(qlist),
                        })

                        # Send updated queue positions for remaining queued messages
                        await _send_queue_update(websocket, session_id)

                        # Build contextual prompt for queued message
                        # Simple: just send the original message, the AI handles it naturally
                        combined_prompt = q_prompt

                        # Track per-queued-message state
                        _q_last_sent_text = ""
                        _q_task_tools_seen = set()
                        _q_skill_tool_seen = False
                        _q_was_cancelled = False

                        try:
                            async for event in engine.send_message(combined_prompt):
                                if cancel_event and cancel_event.is_set():
                                    _q_was_cancelled = True
                                    break

                                event_type = event.get("type", "")

                                if event_type == "assistant":
                                    content = event.get("content", [])
                                    is_partial = event.get("is_partial", False)
                                    if is_partial:
                                        for block in content:
                                            if isinstance(block, dict) and block.get("type") == "text":
                                                full_text = block.get("text", "")
                                                delta = full_text[len(_q_last_sent_text):]
                                                if delta:
                                                    await websocket.send_json({
                                                        "type": "text_delta",
                                                        "text": delta,
                                                    })
                                                    _record_tokens(session_id, "主Agent", delta)
                                                _q_last_sent_text = full_text
                                    else:
                                        _q_last_sent_text = ""
                                        for block in content:
                                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                                tool_name = block.get("name", "")
                                                await websocket.send_json({
                                                    "type": "tool_use",
                                                    "tool_name": tool_name,
                                                    "tool_input": block.get("input", {}),
                                                })
                                                if tool_name in ("TaskCreate", "TaskUpdate"):
                                                    _q_task_tools_seen.add(tool_name)
                                                if tool_name in ("Skill", "SkillManage"):
                                                    _q_skill_tool_seen = True
                                                # Track agent tools — send immediate status update
                                                if tool_name in ("Agent", "agent_tool"):
                                                    try:
                                                        current = _get_agent_status_snapshot(session_id)
                                                        _agent_last_status[session_id] = current
                                                        await websocket.send_json({
                                                            "type": "agent_status",
                                                            "agents": current,
                                                            "session_id": session_id,
                                                        })
                                                    except Exception:
                                                        pass

                                elif event_type == "user":
                                    for block in event.get("content", []):
                                        if isinstance(block, dict) and block.get("type") == "tool_result":
                                            result_text = str(block.get("content", ""))
                                            if len(result_text) > 1000:
                                                result_text = result_text[:1000] + "..."
                                            await websocket.send_json({
                                                "type": "tool_result",
                                                "content": result_text,
                                                "is_error": block.get("is_error", False),
                                            })
                                    if _q_task_tools_seen:
                                        from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display
                                        tasks = get_all_tasks_for_display(state)
                                        await websocket.send_json({
                                            "type": "task_update",
                                            "tasks": tasks,
                                        })

                                elif event_type == "error":
                                    await websocket.send_json({
                                        "type": "error",
                                        "error": event.get("error", ""),
                                    })
                                    break

                        except asyncio.CancelledError:
                            continue
                        except Exception as e:
                            await websocket.send_json({"type": "error", "error": str(e)})
                            continue

                        # Send per-queued-message completion
                        if not _q_was_cancelled:
                            if _q_task_tools_seen:
                                from AutoRUN_v1.tools.task_tool import get_all_tasks_for_display
                                tasks = get_all_tasks_for_display(state)
                                await websocket.send_json({
                                    "type": "task_update",
                                    "tasks": tasks,
                                })
                            if _q_skill_tool_seen:
                                _skill_tool_seen = True

                        await websocket.send_json({
                            "type": "message_complete",
                            "session_id": session_id,
                            "queued_message_id": q_msg_id,
                            "skill_changed": _q_skill_tool_seen,
                            "has_more_queued": bool(session_id in _ws_queued and _ws_queued[session_id]),
                        })
                except Exception:
                    logger.debug("Drain failed", exc_info=True)

            await _drain_queued()

            # Final message_complete only if there were no queued messages processed
            # (the queued loop already sent message_complete for each one)
            if _was_cancelled or not (session_id in _ws_queued and _ws_queued[session_id]):
                # No queued messages were processed — send a final completion
                if not _was_cancelled or (session_id not in _ws_queued or not _ws_queued.get(session_id)):
                    pass  # message_complete is already sent by queued loop or below

        # Always send a final message_complete to signal main processing is done
        # Use a flag so frontend knows this is the "all done" signal
        has_pending_queued = bool(session_id in _ws_queued and _ws_queued[session_id])
        await websocket.send_json({
            "type": "message_complete",
            "session_id": session_id,
            "all_done": True,
            "skill_changed": _skill_tool_seen,
            "has_pending_queued": has_pending_queued,
        })

        if _skill_tool_seen:
            _ws_engines.pop(session_id, None)

        # ── Auto-trigger re-check: handle agents that completed during this run ──
        try:
            await _maybe_auto_trigger_agent_results(websocket, session_id)
        except Exception:
            logger.debug("Auto-trigger re-check failed", exc_info=True)

    except asyncio.CancelledError:
        # Task was cancelled via stop — silently return
        pass
        # Drain remaining queued messages even on cancellation
        try:
            await _drain_queued()
        except Exception:
            pass
    except Exception as e:
        await websocket.send_json({"type": "error", "error": str(e)})
        # Drain remaining queued messages even on error
        try:
            await _drain_queued()
        except Exception:
            pass

    # ── Auto-trigger re-check (also on error/cancel paths) ──
    try:
        await _maybe_auto_trigger_agent_results(websocket, session_id)
    except Exception:
        pass


async def _send_queue_update(websocket: WebSocket, session_id: str):
    """Send updated queue positions to frontend for all remaining queued messages."""
    qlist = _ws_queued.get(session_id, [])
    if not qlist:
        return
    positions = []
    for i, item in enumerate(qlist):
        positions.append({
            "message_id": item.get("message_id", ""),
            "position": i + 1,
            "message_preview": item.get("message", "")[:80],
        })
    try:
        await websocket.send_json({
            "type": "queue_update",
            "session_id": session_id,
            "total": len(qlist),
            "positions": positions,
        })
    except Exception:
        logger.debug("Failed to send queue update", exc_info=True)


async def _handle_load_conversation(websocket: WebSocket, data: Dict[str, Any]) -> None:
    """Handle load_conversation WebSocket message."""
    from AutoRUN_v1.services.conversations import restore_to_state, load_conversation

    session_id = data.get("session_id", "")
    if not session_id:
        await websocket.send_json({"type": "error", "error": "session_id required"})
        return

    try:
        data_conv = load_conversation(session_id)
    except RuntimeError as e:
        await websocket.send_json({"type": "error", "error": str(e)})
        return
    if not data_conv:
        await websocket.send_json({"type": "error", "error": "Conversation not found"})
        return

    from AutoRUN_v1.state.app_state import get_app_state
    state = get_app_state()
    ok = restore_to_state(session_id, state)
    if not ok:
        await websocket.send_json({"type": "error", "error": "Failed to restore"})
        return

    # Store restored state for subsequent chat messages
    _ws_states[session_id] = state

    # Refresh skills
    try:
        from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool
        clear_skills_cache()
        discover_skills(refresh=True, disabled_skills=state._get_disabled_skills())
        register_skills_to_tool(disabled_skills=state._get_disabled_skills())
    except Exception:
        logger.warning("Failed to refresh skills after conversation load", exc_info=True)

    # Invalidate cached engine for this session — messages/state changed
    _ws_engines.pop(session_id, None)

    # Serialize restored messages for frontend display
    serialized_messages = []
    for msg in state.get_messages():
        if hasattr(msg, 'to_dict'):
            serialized_messages.append(msg.to_dict())
        elif isinstance(msg, dict):
            serialized_messages.append(msg)

    await websocket.send_json({
        "type": "conversation_loaded",
        "session_id": session_id,
        "message_count": len(state.get_messages()),
        "model": state.model,
        "messages": serialized_messages,
    })


async def _handle_agent_user_message(websocket: WebSocket, data: Dict[str, Any], session_id: str) -> None:
    """Handle user sending a message to a specific sub-agent (CC gating agent).

    The message is forwarded to the specific agent via SendMessage mechanism.
    A notification is queued for the gating agent so it can coordinate.
    This avoids concurrent websocket writes by routing through the queue.
    """
    from AutoRUN_v1.tools.agent_tool import _background_tasks, get_running_agents
    from AutoRUN_v1.tools.send_message import store_pending_message

    agent_id = data.get("agent_id", "").strip()
    message = data.get("message", "").strip()

    if not agent_id or not message:
        await websocket.send_json({"type": "error", "error": "agent_id and message are required"})
        return

    # Find the target agent (match by agent_id first, then description)
    target_entry = None
    for sid in (session_id, "default"):
        if sid in _background_tasks:
            for entry in _background_tasks[sid]:
                if entry.get("agent_id", "") == agent_id:
                    target_entry = entry
                    break
        if target_entry:
            break
    if not target_entry:
        # Fallback: match by description substring
        for sid in (session_id, "default"):
            if sid in _background_tasks:
                for entry in _background_tasks[sid]:
                    if agent_id.lower() in entry.get("description", "").lower():
                        target_entry = entry
                        break
            if target_entry:
                break

    if not target_entry:
        await websocket.send_json({
            "type": "error",
            "error": f"Agent '{agent_id}' not found or already completed",
        })
        return

    try:
        # 1. Store pending message for the sub-agent (via SendMessage mechanism)
        target_agent_id = target_entry.get("agent_id", agent_id)
        store_pending_message(session_id, target_agent_id, message)

        # 2. Queue a CC notification for the gating agent via the message queue
        #    (avoids concurrent websocket writes)
        cc_msg = (
            f"[Agent CC] 用户向子 Agent「{target_entry.get('description', agent_id)}」"
            f"发送了消息: {message[:200]}"
        )
        if session_id not in _ws_queued:
            _ws_queued[session_id] = []
        from uuid import uuid4
        _ws_queued[session_id].append({
            "message_id": str(uuid4()),
            "message": cc_msg,
            "session_id": session_id,
            "agentPref": getattr(state, 'agent_pref', True) if state else True,
        })
        # Trigger queue processing if no main task is running
        existing_task = _ws_tasks.get(session_id)
        if not existing_task or existing_task.done():
            await _drain_queued()

        # 3. Acknowledge to frontend
        await websocket.send_json({
            "type": "agent_user_message_ack",
            "agent_id": agent_id,
            "message": message,
            "session_id": session_id,
        })

    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "error": f"Failed to forward message to agent: {str(e)}",
        })


async def _handle_skill_toggle(websocket: WebSocket, data: Dict[str, Any]) -> None:
    """Handle skill_toggle WebSocket message."""
    from AutoRUN_v1.state.app_state import get_app_state
    from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

    name = data.get("name", "").strip()
    enabled = data.get("enabled", True)

    state = get_app_state()
    all_skills = discover_skills(refresh=True)

    if name not in all_skills:
        await websocket.send_json({"type": "error", "error": f"Skill '{name}' not found"})
        return

    if enabled:
        state.enable_skill(name)
    else:
        state.disable_skill(name)

    clear_skills_cache()
    discover_skills(refresh=True, disabled_skills=state._get_disabled_skills())
    register_skills_to_tool(disabled_skills=state._get_disabled_skills())

    # Invalidate all cached engines — skill changes require re-init
    _ws_engines.clear()

    await websocket.send_json({
        "type": "skill_toggled",
        "name": name,
        "enabled": enabled,
        "disabled_skills": sorted(state._get_disabled_skills()),
    })


# ── Agent Visibility (polling + output capture) ────────────────────────────

def _record_tokens(session_id: str, source: str, text: str):
    """记录 token 消耗（基于字符数粗略估算：chars/4），同时更新四级计数。"""
    if not text:
        return
    count = max(1, len(text) // 4)

    # 1. 更新内存中的 session 计数
    _session_tokens[session_id] = _session_tokens.get(session_id, 0) + count

    # 2. 持久化到 token_usage.json — 四级计数
    try:
        data = _load_token_usage()
        project_dir = os.path.abspath(os.getcwd())

        # global
        data["global"] = data.get("global", 0) + count

        # projects
        if "projects" not in data:
            data["projects"] = {}
        data["projects"][project_dir] = data["projects"].get(project_dir, 0) + count

        # conversations (use session_id as conversation key)
        if "conversations" not in data:
            data["conversations"] = {}
        data["conversations"][session_id] = data["conversations"].get(session_id, 0) + count

        # sessions (within each conversation)
        if "sessions" not in data:
            data["sessions"] = {}
        if session_id not in data["sessions"]:
            data["sessions"][session_id] = {}
        data["sessions"][session_id]["latest"] = data["sessions"][session_id].get("latest", 0) + count

        _save_token_usage(data)
    except Exception:
        logger.debug("Failed to persist token usage", exc_info=True)


def _get_agent_status_snapshot(session_id: str) -> List[Dict[str, Any]]:
    """Get current agent status snapshot for a session."""
    from AutoRUN_v1.tools.agent_tool import (
        _background_tasks, _background_results, _agent_reminders,
        drain_agent_reminders,
    )

    agents = []

    # Active (running) agents
    if session_id in _background_tasks:
        for entry in _background_tasks[session_id]:
            t = entry.get("task")
            agent_info = {
                "agent_id": entry.get("agent_id", ""),
                "agent_name": entry.get("description", "agent"),
                "agent_type": entry.get("agent_type", "general-purpose"),
                "description": entry.get("description", ""),
                "status": "running",
                "done": t.done() if t else False,
                "cancelled": t.cancelled() if t else False,
            }
            agents.append(agent_info)

    # Completed agents (results are strings like "[Agent 结果: {desc}]\n{result}")
    if session_id in _background_results:
        for result_str in _background_results[session_id]:
            # Parse description from result string
            desc = "Sub-Agent"
            if result_str.startswith("[Agent 结果: "):
                desc = result_str[len("[Agent 结果: "):].split("]", 1)[0]
            elif result_str.startswith("[Agent 错误: "):
                desc = result_str[len("[Agent 错误: "):].split("]", 1)[0]
            elif result_str.startswith("[Agent 已取消: "):
                desc = result_str[len("[Agent 已取消: "):].split("]", 1)[0]
            agents.append({
                "agent_id": desc,
                "agent_name": desc,
                "agent_type": "general-purpose",
                "description": desc,
                "status": "completed",
                "done": True,
                "cancelled": False,
                "result_summary": result_str[:500],
            })

    # Reminders (agent running > 5 min, not completion)
    if session_id in _agent_reminders:
        for reminder in _agent_reminders[session_id]:
            # Parse agent description from reminder
            desc = "Unknown"
            import re
            m = re.search(r"'(.*?)'", reminder)
            if m:
                desc = m.group(1)
            agents.append({
                "agent_id": desc,
                "agent_name": desc,
                "agent_type": "general-purpose",
                "description": desc,
                "status": "reminder",
                "done": False,
                "cancelled": False,
                "reminder_text": reminder,
            })

    return agents


def _agent_status_changed(session_id: str, current: List[Dict[str, Any]]) -> bool:
    """Check if agent status has changed since last poll."""
    last = _agent_last_status.get(session_id, [])
    if len(last) != len(current):
        return True
    for cur, prev in zip(current, last):
        if cur.get("status") != prev.get("status"):
            return True
        if cur.get("done") != prev.get("done"):
            return True
    return False


async def _poll_agent_status(websocket: WebSocket, session_id: str, cancel_event: asyncio.Event):
    """Periodically poll agent status and send updates to frontend."""
    try:
        while not cancel_event.is_set():
            try:
                current = _get_agent_status_snapshot(session_id)
                if _agent_status_changed(session_id, current) or session_id not in _agent_last_status:
                    _agent_last_status[session_id] = current
                    await websocket.send_json({
                        "type": "agent_status",
                        "agents": current,
                        "session_id": session_id,
                    })
            except (WebSocketDisconnect, ConnectionError):
                break
            except Exception:
                logger.debug("Agent status poll error", exc_info=True)

            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass


async def _send_agent_output(websocket: WebSocket, session_id: str, agent_id: str,
                              text: str, is_partial: bool = True):
    """Send agent output delta to frontend."""
    try:
        await websocket.send_json({
            "type": "agent_output",
            "session_id": session_id,
            "agent_id": agent_id,
            "text": text,
            "is_partial": is_partial,
        })
        if is_partial and text:
            _record_tokens(session_id, f"子Agent-{agent_id}", text)
    except Exception:
        logger.debug("Failed to send agent output", exc_info=True)


async def _maybe_auto_trigger_agent_results(websocket: WebSocket, session_id: str):
    """Check if there are unprocessed agent results with no active agents,
    and auto-trigger gating agent processing if so."""
    from AutoRUN_v1.tools.agent_tool import _background_tasks, _background_results, drain_background_results

    # Prevent duplicate auto-trigger scheduling with asyncio.Lock
    if session_id not in _auto_trigger_guard:
        _auto_trigger_guard[session_id] = asyncio.Lock()
    lock = _auto_trigger_guard[session_id]

    async with lock:
        # Check if main chat is already running
        existing_task = _ws_tasks.get(session_id)
        if existing_task and not existing_task.done():
            return  # Main chat still running, results will drain naturally on next message

        # Check if there are still running agents
        running_count = 0
        if session_id in _background_tasks:
            running_count = len(_background_tasks[session_id])
        if running_count > 0:
            return  # Still have running agents, wait for them

        # Check if there are results to process
        if session_id not in _background_results and "default" not in _background_results:
            return
        all_results = _background_results.get(session_id, []) + _background_results.get("default", [])
        if not all_results:
            return

        # All agents done, trigger auto-processing
        cancel_event = asyncio.Event()
        _ws_cancel_events[session_id] = cancel_event

        data = {
            "type": "chat",
            "message": "__AUTO_TRIGGER__",
            "session_id": session_id,
        }

        # Set _ws_tasks BEFORE creating the task to prevent duplicate triggers
        task = asyncio.create_task(
            _handle_chat_message(websocket, data, cancel_event)
        )
        _ws_tasks[session_id] = task
        logger.debug("AUTO_TRIGGER: session=%s, results=%d", session_id, len(all_results))


# ── Static Files ────────────────────────────────────────────────────────────

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
os.makedirs(FRONTEND_DIR, exist_ok=True)

# Serve frontend static files if index.html exists
index_path = os.path.join(FRONTEND_DIR, "index.html")
if os.path.isfile(index_path):
    @app.get("/")
    async def serve_frontend():
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="frontend_assets")
else:
    @app.get("/")
    async def serve_placeholder():
        return HTMLResponse("""
        <html><head><title>AutoRUN_v1</title></head>
        <body style="font-family:sans-serif;margin:2rem;text-align:center">
          <h1>AutoRUN_v1 Web UI</h1>
          <p>前端文件未找到。请将 index.html 放入 <code>ui/web/frontend/</code> 目录。</p>
          <p>API 端点: <a href="/api/health">/api/health</a></p>
        </body></html>
        """)


# ── Server Start ────────────────────────────────────────────────────────────

def _port_is_available(host: str, port: int) -> bool:
    """Check whether a TCP port is available for binding."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def start_web_server(host: str = "127.0.0.1", port: int = 8765) -> str:
    """启动 FastAPI Web 服务器，端口被占用时自动尝试后续端口。

    Returns:
        服务器 URL（可能与请求的端口不同，如果使用了回退端口）。
    """
    import threading
    import sys

    # 尝试端口，从指定端口开始，最多 10 次
    actual_port = port
    max_attempts = 10
    for attempt in range(max_attempts):
        if _port_is_available(host, actual_port):
            break
        if attempt == max_attempts - 1:
            print(f"\n[错误] 端口 {port}-{actual_port} 均被占用，请手动释放后重试。",
                  file=sys.stderr)
            sys.exit(1)
        print(f"  端口 {actual_port} 已被占用，尝试 {actual_port + 1}...", file=sys.stderr)
        actual_port += 1

    if actual_port != port:
        print(f"  使用端口 {actual_port}", file=sys.stderr)

    def _run():
        try:
            asyncio.run(uvicorn.run(
                app,
                host=host,
                port=actual_port,
                log_level="info",
            ))
        except SystemExit:
            pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    # Brief wait to detect bind errors
    import time
    time.sleep(1.5)
    if not thread.is_alive():
        print(f"\n[错误] 无法绑定端口 {actual_port}。", file=sys.stderr)
        sys.exit(1)

    return f"http://{host}:{actual_port}"
