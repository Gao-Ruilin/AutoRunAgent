"""
AgentManageTool — 门控Agent 用于管理下游 Agent 模板的工具。

参照 tools/skill_manager.py 的设计。
允许门控Agent 创建、修改、删除、列出下游 Agent。
Agent 模板存储在 ~/.autorun/agents/ 目录中。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

from AutoRUN_v1.services.agent_registry import clear_agents_cache, discover_agents
from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class AgentManageTool(Tool):
    """创建、修改、删除和列出下游 Agent 模板。"""

    @property
    def name(self) -> str:
        return "AgentManage"

    @property
    def description(self) -> str:
        return """管理下游 Agent 模板。使用此工具来创建、修改、删除或列出下游 Agent。

下游 Agent 是你可以分派任务的目标。每个 Agent 有一个名称、描述（用来判断何时分派给它）和专用的系统提示词。

操作:
- list: 列出所有已注册的 Agent
- create: 创建新的 Agent 模板（存在则覆盖）
- delete: 删除一个 Agent 模板
- get: 获取某个 Agent 的完整定义

Agent JSON 格式:
{
  "name": "agent-name",
  "description": "Agent 的职责和能力描述（门控Agent 根据这个描述做任务匹配和分发）",
  "system_prompt": "Agent 的专用系统提示词",
  "model": "opus"  // 可选：指定模型，不填则继承默认
}

重要: 创建/删除 Agent 后会自动刷新缓存，即刻生效。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "delete", "get"],
                    "description": "操作类型",
                },
                "name": {
                    "type": "string",
                    "description": "Agent 名称（create/delete/get 需要）",
                },
                "description": {
                    "type": "string",
                    "description": "Agent 的职责描述（仅 create），门控Agent 用它做任务匹配",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Agent 专用系统提示词（仅 create）",
                },
                "model": {
                    "type": "string",
                    "description": "可选模型（仅 create），如 opus/sonnet/haiku",
                },
            },
            "required": ["action"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return args.get("action") in ("list", "get")

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = args.get("action", "list")
        agents_dir = os.path.expanduser("~/.autorun/agents")

        if action == "list":
            return self._list_agents()
        elif action == "get":
            return self._get_agent(args)
        elif action == "create":
            return self._create_agent(args, agents_dir)
        elif action == "delete":
            return self._delete_agent(args, agents_dir)
        else:
            return ToolResult(
                data=f"未知操作: {action}。支持: list, create, delete, get",
                is_error=True,
            )

    def _list_agents(self) -> ToolResult:
        """列出所有 Agent 模板。"""
        agents = discover_agents(refresh=True)

        if not agents:
            return ToolResult(
                data="当前没有已注册的下游 Agent。\n\n"
                     "创建新 Agent 示例:\n"
                     "AgentManage(action=\"create\", name=\"bug-fixer\", "
                     "description=\"专注于定位和修复代码bug\", "
                     "system_prompt=\"你是一个专业的bug修复专家...\")",
                is_error=False,
            )

        lines = ["已注册的下游 Agent:"]
        for name, agent_def in sorted(agents.items()):
            desc = agent_def.get("description", "")
            model = agent_def.get("model", "")
            source = agent_def.get("_source", "unknown")
            source_label = {"user": "[用户]", "project": "[项目]"}.get(source, f"[{source}]")
            model_str = f" 模型:{model}" if model else ""
            lines.append(f"  {source_label} {name}{model_str}")
            if desc:
                lines.append(f"    {desc}")

        lines.append(f"\n共 {len(agents)} 个 Agent。")
        return ToolResult(data="\n".join(lines), is_error=False)

    def _get_agent(self, args: Dict[str, Any]) -> ToolResult:
        """获取指定 Agent 详情。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 需要提供 Agent 名称", is_error=True)

        agent = discover_agents(refresh=True).get(name)
        if not agent:
            return ToolResult(
                data=f"未找到 Agent: {name}\n"
                     f"可用 Agent: {', '.join(discover_agents().keys()) or '无'}",
                is_error=True,
            )

        model = agent.get("model", "(默认)")
        source = agent.get("_source", "unknown")
        return ToolResult(
            data=f"Agent: {name}\n"
                 f"来源: {source}\n"
                 f"模型: {model}\n"
                 f"描述: {agent.get('description', '')}\n\n"
                 f"--- system_prompt ---\n"
                 f"{agent.get('system_prompt', '')}",
            is_error=False,
        )

    def _create_agent(self, args: Dict[str, Any], agents_dir: str) -> ToolResult:
        """创建或更新 Agent 模板。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 需要提供 Agent 名称", is_error=True)

        # 基本名称验证
        if "/" in name or "\\" in name:
            return ToolResult(data="错误: Agent 名称不能包含路径分隔符", is_error=True)
        if len(name) > 64:
            return ToolResult(data="错误: Agent 名称不能超过64个字符", is_error=True)

        description = (args.get("description", "") or "").strip()
        system_prompt = (args.get("system_prompt", "") or "").strip()

        if not description:
            return ToolResult(data="错误: 需要提供 Agent 描述（用于任务匹配）", is_error=True)
        if not system_prompt:
            return ToolResult(data="错误: 需要提供 Agent 系统提示词", is_error=True)

        agent_def: Dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
        }

        if args.get("model"):
            agent_def["model"] = args["model"]

        # 确保目录存在
        os.makedirs(agents_dir, exist_ok=True)

        out_path = os.path.join(agents_dir, f"{name}.json")
        existed = os.path.isfile(out_path)

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(agent_def, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return ToolResult(data=f"写入 Agent 文件失败: {e}", is_error=True)

        # 刷新缓存以立即生效
        clear_agents_cache()

        action = "已更新" if existed else "已创建"
        return ToolResult(
            data=f"Agent '{name}' {action}。\n"
                 f"文件: {out_path}\n"
                 f"描述: {description}\n"
                 f"模型: {agent_def.get('model', '默认')}",
            is_error=False,
        )

    def _delete_agent(self, args: Dict[str, Any], agents_dir: str) -> ToolResult:
        """删除一个 Agent 模板。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 需要提供 Agent 名称", is_error=True)

        path = os.path.join(agents_dir, f"{name}.json")

        if not os.path.isfile(path):
            return ToolResult(
                data=f"未找到 Agent '{name}'。\n路径: {path}",
                is_error=True,
            )

        try:
            os.remove(path)
        except OSError as e:
            return ToolResult(data=f"删除 Agent 文件失败: {e}", is_error=True)

        # 刷新缓存
        clear_agents_cache()

        return ToolResult(
            data=f"Agent '{name}' 已删除。",
            is_error=False,
        )
