"""
Agent 注册中心 — 发现并加载用户定义的 Agent 模板。

参照 skills/loader.py 的设计模式。
Agent 模板存储在:
  ~/.autorun/agents/    （用户级，跨项目复用）
  ./.autorun/agents/    （项目级）

Agent 模板是 JSON 文件，定义下游 Agent 的名称、描述和系统提示词。
代码中不预定义任何 Agent 类型 — 所有 Agent 都由用户动态创建。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# 缓存
_agents_cache: Optional[Dict[str, Dict[str, Any]]] = None
_agents_cache_cwd: Optional[str] = None


def discover_agents(refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """发现所有可用的 Agent 模板。

    搜索用户级和项目级的 Agent 目录。
    结果在会话生命周期内缓存。

    Args:
        refresh: 强制重新扫描。
    """
    global _agents_cache, _agents_cache_cwd

    current_cwd = os.getcwd()
    if _agents_cache_cwd is not None and _agents_cache_cwd != current_cwd:
        refresh = True

    if _agents_cache is not None and not refresh:
        return dict(_agents_cache)

    agents: Dict[str, Dict[str, Any]] = {}

    # 1. 用户级 Agent (~/.autorun/agents/)
    user_agents_dir = os.path.expanduser("~/.autorun/agents")
    if os.path.isdir(user_agents_dir):
        _load_agents_from_dir(agents, user_agents_dir, "user")

    # 2. 项目级 Agent (./.autorun/agents/)
    project_agents_dir = os.path.join(os.getcwd(), ".autorun", "agents")
    if os.path.isdir(project_agents_dir):
        _load_agents_from_dir(agents, project_agents_dir, "project")

    _agents_cache = agents
    _agents_cache_cwd = current_cwd
    return agents


def _load_agents_from_dir(agents: Dict[str, Dict[str, Any]],
                          directory: str,
                          source: str) -> None:
    """从目录加载 Agent 定义。

    Agent JSON 格式:
    {
      "name": "agent-name",
      "description": "该 Agent 的职责描述，门控Agent 用它来做任务匹配",
      "system_prompt": "专用的系统提示词",
      "model": "opus"  // 可选
    }
    """
    dir_path = Path(directory)

    for agent_file in sorted(dir_path.glob("*.json")):
        if agent_file.name.startswith("."):
            continue

        try:
            with open(agent_file, "r", encoding="utf-8") as f:
                agent_def = json.load(f)

            name = agent_def.get("name", agent_file.stem)
            if not name:
                continue

            # 确保必要字段存在
            if "description" not in agent_def:
                agent_def["description"] = ""
            if "system_prompt" not in agent_def:
                agent_def["system_prompt"] = ""

            agent_def["_source"] = source
            agent_def["_file"] = str(agent_file)
            agents[name] = agent_def

        except (json.JSONDecodeError, IOError, OSError):
            pass


def get_agent(name: str) -> Optional[Dict[str, Any]]:
    """按名称获取特定 Agent 模板。"""
    agents = discover_agents()
    return agents.get(name)


def list_agent_names() -> List[str]:
    """列出所有可用 Agent 名称。"""
    return sorted(discover_agents().keys())


def get_agents_for_prompt() -> str:
    """生成可供注入系统提示词的 Agent 列表文本。"""
    agents = discover_agents()
    if not agents:
        return "（暂无已注册的 Agent，用户可通过你来创建）"

    lines = ["当前已注册的 Agent 列表:"]
    for name, agent_def in sorted(agents.items()):
        desc = agent_def.get("description", "")
        model = agent_def.get("model", "")
        model_str = f" [模型: {model}]" if model else ""
        source = agent_def.get("_source", "unknown")
        source_label = {"user": "用户级", "project": "项目级"}.get(source, source)
        lines.append(f"  - {name} ({source_label}){model_str}: {desc}")

    return "\n".join(lines)


def clear_agents_cache() -> None:
    """清除 Agent 缓存。"""
    global _agents_cache, _agents_cache_cwd
    _agents_cache = None
    _agents_cache_cwd = None
