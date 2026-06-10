"""
SkillManageTool — AI 可调用的 skill 管理工具。

允许 AI 创建、修改、删除和列出 skill。
Skill 文件存储在 ~/.autorun/skills/ 目录中。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class SkillManageTool(Tool):
    """创建、修改、删除和列出 Agent skill。"""

    @property
    def name(self) -> str:
        return "SkillManage"

    @property
    def description(self) -> str:
        return """管理此 Agent 的 skill 文件。

使用此工具来创建、修改、删除或列出 skill。Skill 是存储在 ~/.autorun/skills/ 目录下的 JSON 或 Markdown 文件，为 Agent 提供专门的提示词和工作流。

操作:
- list: 列出所有已安装的 skill（名称和描述）
- create: 在 ~/.autorun/skills/ 中创建一个新的 skill JSON 文件
- delete: 删除 ~/.autorun/skills/ 中的一个 skill 文件
- get: 获取特定 skill 的完整内容

Skill JSON 格式:
{
  "name": "skill_name",
  "type": "prompt",
  "description": "简短描述，显示在 available_skills 列表中",
  "prompt": "完整的提示词内容"
}

重要: 创建/删除 skill 后，需要刷新会话（重启）才能加载变更。此工具不会自动刷新系统上下文中的 skill 列表——它只管理文件。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "delete", "get"],
                    "description": "要执行的操作: list(列出所有skill), create(创建新skill), delete(删除skill), get(获取skill详情)",
                },
                "name": {
                    "type": "string",
                    "description": "Skill 名称 (create/delete/get 操作必需)",
                },
                "description": {
                    "type": "string",
                    "description": "Skill 简短描述 (仅 create 操作)",
                },
                "prompt": {
                    "type": "string",
                    "description": "Skill 完整提示词内容 (仅 create 操作)",
                },
            },
            "required": ["action"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return args.get("action") in ("list", "get")

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = args.get("action", "list")
        skills_dir = os.path.expanduser("~/.autorun/skills")
        os.makedirs(skills_dir, exist_ok=True)

        if action == "list":
            return self._list_skills(skills_dir)
        elif action == "get":
            return self._get_skill(args, skills_dir)
        elif action == "create":
            return self._create_skill(args, skills_dir)
        elif action == "delete":
            return self._delete_skill(args, skills_dir)
        else:
            return ToolResult(
                data=f"未知操作: {action}。支持的操作: list, create, delete, get",
                is_error=True,
            )

    def _list_skills(self, skills_dir: str) -> ToolResult:
        """列出所有 skill 文件。"""
        skills = []
        dir_path = Path(skills_dir)

        for f in sorted(dir_path.glob("*")):
            if f.name.startswith("."):
                continue
            try:
                if f.suffix == ".json":
                    with open(f, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    skills.append({
                        "name": data.get("name", f.stem),
                        "type": data.get("type", "?"),
                        "file": f.name,
                        "description": data.get("description", ""),
                    })
                elif f.suffix == ".md":
                    skills.append({
                        "name": f.stem,
                        "type": "prompt",
                        "file": f.name,
                        "description": f"Markdown skill ({f.stat().st_size} bytes)",
                    })
            except (json.JSONDecodeError, OSError):
                skills.append({
                    "name": f.stem,
                    "type": "?",
                    "file": f.name,
                    "description": "(无法解析)",
                })

        if not skills:
            return ToolResult(
                data=f"~/.autorun/skills/ 中没有 skill 文件。\n\n创建新 skill 示例:\nSkillManage(action=\"create\", name=\"my_skill\", description=\"简短描述\", prompt=\"详细提示词...\")",
                is_error=False,
            )

        result = f"用户 skill ({skills_dir}):\n\n"
        for s in skills:
            desc = s.get("description", "")
            desc_str = f" — {desc}" if desc else ""
            result += f"  [{s['type']}] {s['name']}{desc_str}\n"
        result += f"\n共 {len(skills)} 个 skill。"
        return ToolResult(data=result, is_error=False)

    def _get_skill(self, args: Dict[str, Any], skills_dir: str) -> ToolResult:
        """获取特定 skill 的完整内容。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 需要提供 skill name", is_error=True)

        # Try .json first, then .md
        json_path = os.path.join(skills_dir, f"{name}.json")
        md_path = os.path.join(skills_dir, f"{name}.md")

        if os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ToolResult(
                    data=f"Skill: {data.get('name', name)}\n类型: {data.get('type', '?')}\n描述: {data.get('description', '')}\n\n--- prompt ---\n{data.get('prompt', '')}",
                    is_error=False,
                )
            except (json.JSONDecodeError, OSError) as e:
                return ToolResult(data=f"读取 skill 失败: {e}", is_error=True)
        elif os.path.isfile(md_path):
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    content = f.read()
                return ToolResult(
                    data=f"Skill: {name} (Markdown)\n\n--- prompt ---\n{content}",
                    is_error=False,
                )
            except OSError as e:
                return ToolResult(data=f"读取 skill 失败: {e}", is_error=True)
        else:
            return ToolResult(
                data=f"未找到 skill: {name}\n查找路径:\n  {json_path}\n  {md_path}",
                is_error=True,
            )

    def _create_skill(self, args: Dict[str, Any], skills_dir: str) -> ToolResult:
        """创建一个新的 skill JSON 文件。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 需要提供 skill name", is_error=True)

        # Validate name (no path separators, reasonable length)
        if "/" in name or "\\" in name:
            return ToolResult(data="错误: skill 名称不能包含路径分隔符", is_error=True)
        if len(name) > 64:
            return ToolResult(data="错误: skill 名称不能超过64个字符", is_error=True)

        description = (args.get("description", "") or "").strip()
        prompt = (args.get("prompt", "") or "").strip()

        if not prompt:
            return ToolResult(data="错误: 需要提供 skill prompt 内容", is_error=True)

        skill_def = {
            "name": name,
            "type": "prompt",
            "description": description or f"User skill: {name}",
            "prompt": prompt,
        }

        out_path = os.path.join(skills_dir, f"{name}.json")
        exists = os.path.isfile(out_path)

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(skill_def, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return ToolResult(data=f"写入 skill 文件失败: {e}", is_error=True)

        action = "已更新" if exists else "已创建"
        return ToolResult(
            data=f"Skill '{name}' {action}。\n文件: {out_path}\n描述: {description or '(无)'}\n\n注意: 需要重启会话或刷新才能加载此 skill。",
            is_error=False,
        )

    def _delete_skill(self, args: Dict[str, Any], skills_dir: str) -> ToolResult:
        """删除一个 skill 文件。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 需要提供 skill name", is_error=True)

        # Try both .json and .md
        deleted = []
        for ext in (".json", ".md"):
            path = os.path.join(skills_dir, f"{name}{ext}")
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    deleted.append(path)
                except OSError as e:
                    return ToolResult(data=f"删除 skill 文件失败: {e}", is_error=True)

        if not deleted:
            return ToolResult(
                data=f"未找到 skill '{name}'。\n查找路径:\n  {skills_dir}/{name}.json\n  {skills_dir}/{name}.md",
                is_error=True,
            )

        return ToolResult(
            data=f"Skill '{name}' 已删除。\n删除的文件: {', '.join(deleted)}\n\n注意: 需要重启会话以反映变更。",
            is_error=False,
        )
