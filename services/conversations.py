"""
对话持久化服务 — 保存、加载、列表和搜索对话历史。

对话存储在 ~/.autorun/conversations/:
  - index.json  — 所有会话元数据的索引
  - {session_id}.json — 完整会话数据
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from AutoRUN_v1.utils.env_utils import get_autorun_config_dir
from AutoRUN_v1.utils.file_lock import FileLock

logger = logging.getLogger(__name__)


def ensure_conversations_dir() -> str:
    """确保对话存储目录存在并返回其路径。"""
    d = os.path.join(get_autorun_config_dir(), "conversations")
    os.makedirs(d, exist_ok=True)
    return d


def _index_path() -> str:
    return os.path.join(ensure_conversations_dir(), "index.json")


def _session_path(session_id: str) -> str:
    return os.path.join(ensure_conversations_dir(), f"{session_id}.json")


def _load_index() -> List[Dict[str, Any]]:
    """加载索引文件。

    Raises:
        RuntimeError: 索引文件存在但已损坏，无法解析。
    """
    path = _index_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                raise RuntimeError(
                    f"索引文件格式异常（期望列表，实际为 {type(data).__name__}），"
                    f"请检查 {path}"
                )
            return data
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"索引文件已损坏，无法解析 JSON：{path}"
        ) from e
    except IOError as e:
        raise RuntimeError(
            f"索引文件无法读取：{path}"
        ) from e


def _save_index(entries: List[Dict[str, Any]]) -> None:
    """保存索引文件。"""
    path = _index_path()
    with FileLock(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)


def get_project_name(cwd: str) -> str:
    """从工作目录路径提取项目名称。"""
    return os.path.basename(cwd.rstrip("/").rstrip("\\")) or cwd


def _get_preview(messages: List[Dict[str, Any]], max_len: int = 80) -> str:
    """从消息列表中提取预览文本（第一条用户消息的前几个字）。"""
    for msg in messages:
        if msg.get("type") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text and not text.startswith("["):
                        return text[:max_len]
        elif isinstance(content, str) and not content.startswith("["):
            return content[:max_len]
    return ""


def save_conversation(state) -> str:
    """将当前 AppState 保存为对话文件。

    返回 session_id。

    Raises:
        RuntimeError: 索引文件损坏导致无法更新。
    """
    from AutoRUN_v1.messages.types import Message

    # 生成或复用 session_id
    if state.session_id:
        session_id = state.session_id
    else:
        import uuid
        session_id = uuid.uuid4().hex[:12]
        state.session_id = session_id

    now = datetime.now(timezone.utc).isoformat()

    if state.session_created_at is None:
        state.session_created_at = now

    # 序列化消息
    messages = state.get_messages()
    serialized_messages = []
    for msg in messages:
        if hasattr(msg, 'to_dict'):
            serialized_messages.append(msg.to_dict())
        elif isinstance(msg, dict):
            serialized_messages.append(msg)
        else:
            serialized_messages.append({"type": "unknown", "content": str(msg)})

    # 构建会话数据
    session_data: Dict[str, Any] = {
        "session_id": session_id,
        "cwd": state.cwd,
        "model": state.model,
        "created_at": state.session_created_at,
        "updated_at": now,
        "messages": serialized_messages,
        "disabled_skills": sorted(state._get_disabled_skills()),
        "permission_mode": state.permission_mode,
    }

    # 保存会话文件
    with FileLock(_session_path(session_id)):
        with open(_session_path(session_id), "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)

    # 更新索引
    index = _load_index()
    # 移除同 session_id 的旧条目
    index = [e for e in index if e.get("session_id") != session_id]
    index.append({
        "session_id": session_id,
        "cwd": state.cwd,
        "project_name": get_project_name(state.cwd),
        "model": state.model,
        "message_count": len(messages),
        "created_at": state.session_created_at,
        "updated_at": now,
        "preview": _get_preview(serialized_messages),
        "disabled_skills": sorted(state._get_disabled_skills()),
        "permission_mode": state.permission_mode,
    })
    _save_index(index)

    return session_id


def load_conversation(session_id: str) -> Optional[Dict[str, Any]]:
    """加载指定会话的完整数据。

    Raises:
        RuntimeError: 会话文件存在但数据已损坏，无法解析。
    """
    path = _session_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"会话文件 {session_id}.json 已损坏，无法解析 JSON"
        ) from e
    except IOError as e:
        raise RuntimeError(
            f"会话文件 {session_id}.json 无法读取"
        ) from e


def restore_to_state(session_id: str, state) -> bool:
    """将会话数据恢复到 AppState。

    返回 True 表示成功，False 表示未找到会话或数据已损坏。
    损坏的消息会被替换为 SystemMessage 占位符，确保用户和 AI 能感知。
    """
    from AutoRUN_v1.messages.types import message_from_dict, SystemMessage

    try:
        data = load_conversation(session_id)
    except RuntimeError as e:
        state.add_message(SystemMessage(
            content=f"❌ {e}",
            level="error",
        ))
        return False

    if data is None:
        return False

    # 恢复消息 — 反序列化失败的消息替换为明确占位符
    state.clear_messages()
    skipped = 0
    for msg_dict in data.get("messages", []):
        try:
            msg = message_from_dict(msg_dict)
            state.add_message(msg)
        except Exception:
            skipped += 1
            state.add_message(SystemMessage(
                content="⚠️ 此处有一条无法解析的历史消息（格式不兼容，已被跳过）",
                level="warn",
            ))

    # 恢复状态
    state.session_id = data.get("session_id", session_id)
    state.session_created_at = data.get("created_at")
    state.model = data.get("model", state.model)
    state.permission_mode = data.get("permission_mode", state.permission_mode)

    # 恢复 disabled_skills
    state._get_disabled_skills().clear()
    for name in data.get("disabled_skills", []):
        state._get_disabled_skills().add(name)

    # 如果 cwd 不同，更新
    saved_cwd = data.get("cwd", "")
    if saved_cwd and os.path.isdir(saved_cwd) and saved_cwd != state.cwd:
        state.cwd = saved_cwd

    if skipped > 0:
        state.add_message(SystemMessage(
            content=(
                f"❌ 从历史对话恢复时，共 {skipped} 条消息因格式不兼容无法恢复。"
                f"对话可能不完整。"
            ),
            level="error",
        ))

    return True


def list_conversations(cwd_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出已保存的对话。

    Args:
        cwd_filter: 如果提供，只返回该目录下的会话。

    Raises:
        RuntimeError: 索引文件损坏。
    """
    index = _load_index()
    # 按更新时间降序排列
    index.sort(key=lambda e: e.get("updated_at", ""), reverse=True)

    if cwd_filter:
        cwd_filter = cwd_filter.rstrip("/").rstrip("\\")
        index = [e for e in index if e.get("cwd", "").rstrip("/").rstrip("\\") == cwd_filter]

    return index


def delete_conversation(session_id: str) -> bool:
    """删除会话及其索引条目。

    Raises:
        RuntimeError: 索引文件损坏。
    """
    path = _session_path(session_id)
    deleted = False
    if os.path.exists(path):
        os.remove(path)
        deleted = True

    index = _load_index()
    index = [e for e in index if e.get("session_id") != session_id]
    _save_index(index)

    return deleted


def search_conversations(query: str) -> List[Dict[str, Any]]:
    """搜索对话（在项目名称和预览文本中匹配）。

    Args:
        query: 搜索关键词。

    Raises:
        RuntimeError: 索引文件损坏。
    """
    index = _load_index()
    query_lower = query.lower()
    results = []
    for entry in index:
        preview = entry.get("preview", "").lower()
        project = entry.get("project_name", "").lower()
        model = entry.get("model", "").lower()
        if query_lower in preview or query_lower in project or query_lower in model:
            results.append(entry)
    results.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    return results
