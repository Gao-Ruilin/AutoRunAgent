"""
SkillTool — Load and execute user-defined skills.

Mirrors src/skills/ — provides the / command system for invoking
skills (specialized prompts and workflows).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


# Available skills registry
_available_skills: Dict[str, Dict[str, Any]] = {}


class SkillTool(Tool):
    """Execute a skill (specialized prompt/workflow)."""

    @property
    def name(self) -> str:
        return "Skill"

    @property
    def description(self) -> str:
        return """在主对话中执行一个技能。

当用户要求你执行任务时，检查是否有任何可用技能匹配。技能提供专门的能力和领域知识。

当用户提到"斜杠命令"或"/<something>"（例如 "/commit", "/review-pr"）时，他们指的是一个技能。使用此工具来调用它。

调用方法:
- 使用此工具并指定技能名称和可选参数
- 示例:
  - skill: "pdf" — 调用 pdf 技能
  - skill: "commit", args: "-m 'Fix bug'" — 带参数调用
  - skill: "review-pr", args: "123" — 带参数调用

重要:
- 可用的技能在 system-reminder 消息中列出
- 当技能匹配用户请求时，在任何其他响应之前调用 Skill 工具
- 绝不在没有实际调用此工具的情况下提及某个技能
- 不要调用已经在运行的技能
- 不要对内置 CLI 命令（如 /help, /clear 等）使用此工具"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name. E.g., \"commit\", \"review-pr\", or \"pdf\"",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments for the skill",
                },
            },
            "required": ["skill"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        skill_name = args.get("skill", "").strip()
        skill_args = args.get("args", "")

        if not skill_name:
            return ToolResult(data="Error: skill name is required", is_error=True)

        # Look up the skill
        skill = _available_skills.get(skill_name)
        if skill:
            return await self._execute_skill(skill, skill_args, context)

        # No matching skill found
        return ToolResult(
            data=f"Skill '{skill_name}' not found. Available skills: {', '.join(_available_skills.keys()) or 'none'}",
            is_error=False,
        )

    async def _execute_skill(self, skill: Dict[str, Any], args_str: str,
                             context: ToolContext) -> ToolResult:
        """Execute a loaded skill."""
        skill_type = skill.get("type", "prompt")

        if skill_type == "prompt":
            prompt = skill.get("prompt", "")
            return ToolResult(
                data=f"[Skill '{skill.get('name')}' loaded]\n\n{prompt}",
                is_error=False,
            )
        elif skill_type == "command":
            cmd = skill.get("command", "")
            return ToolResult(
                data=f"[Skill '{skill.get('name')}' — command execution not yet available]\n{cmd}",
                is_error=False,
            )
        else:
            return ToolResult(
                data=f"Skill '{skill.get('name')}' has unsupported type: {skill_type}",
                is_error=True,
            )


def register_skill(name: str, skill_def: Dict[str, Any]) -> None:
    """Register a skill definition."""
    _available_skills[name] = skill_def


class SkillToggleTool(Tool):
    """Enable or disable a skill during a conversation."""

    @property
    def name(self) -> str:
        return "SkillToggle"

    @property
    def description(self) -> str:
        return """启用或禁用一个技能。被禁用的技能在当前对话中将立即可用/不可用。

当你需要:
- 暂时禁用一个不适用或有冲突的技能
- 重新启用之前禁用的技能
- 根据对话上下文调整可用技能列表

使用此工具来管理对话中的技能状态。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Skill 名称",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "true = 启用技能, false = 禁用技能",
                },
            },
            "required": ["skill", "enabled"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return False

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        skill_name = args.get("skill", "").strip()
        enabled = args.get("enabled", True)

        if not skill_name:
            return ToolResult(data="Error: skill name is required", is_error=True)

        try:
            from AutoRUN_v1.state.app_state import get_app_state
            from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

            state = context.state if context.state else get_app_state()

            # 检查 skill 是否存在（在所有 skill 中查找，包括禁用的）
            all_skills = discover_skills(refresh=True)
            if skill_name not in all_skills:
                return ToolResult(
                    data=f"Skill '{skill_name}' 不存在。可用技能: {', '.join(sorted(all_skills.keys())) or '无'}",
                    is_error=True,
                )

            if enabled:
                state.enable_skill(skill_name)
                # 重新注册 skill
                skill_def = all_skills.get(skill_name, {})
                if skill_def:
                    register_skill(skill_name, skill_def)
            else:
                state.disable_skill(skill_name)

            # 刷新缓存使更改生效
            clear_skills_cache()
            _ = discover_skills(refresh=True, disabled_skills=state._get_disabled_skills())

            action = "已启用" if enabled else "已禁用"
            return ToolResult(
                data=f"Skill '{skill_name}' {action}。",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(
                data=f"切换 Skill '{skill_name}' 失败: {e}",
                is_error=True,
            )


def register_skills_from_dir(skills_dir: str) -> None:
    """Load skills from a directory of skill definition files."""
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        return

    for skill_file in skills_path.glob("*.json"):
        try:
            import json

            with open(skill_file, "r") as f:
                skill = json.load(f)

            name = skill.get("name") or skill_file.stem
            _available_skills[name] = skill
        except Exception:
            logger.warning("Failed to load skill file: %s", skill_file, exc_info=True)

