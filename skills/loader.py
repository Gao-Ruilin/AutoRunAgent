"""
技能加载器 — 发现并加载用户自定义技能。

对应 src/skills/loader.ts — 从以下来源发现技能:
1. 内置技能目录
2. 用户技能目录 (~/.autorun/skills/)
3. 项目本地技能 (.autorun/skills/)

技能是通过 SkillTool 调用的专用提示词和工作流。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# Cache of discovered skills
_skills_cache: Optional[Dict[str, Dict[str, Any]]] = None
_skills_cache_cwd: Optional[str] = None
_memory_cache: Optional[Dict[str, str]] = None


def discover_skills(refresh: bool = False,
                   disabled_skills: Optional[set] = None) -> Dict[str, Dict[str, Any]]:
    """发现所有可用技能。

    搜索内置、用户和项目本地的技能目录。
    结果在会话生命周期内缓存。当工作目录变化时自动失效。

    Args:
        refresh: 如果为 True，强制重新扫描。
        disabled_skills: 要排除的技能名称集合。
    """
    global _skills_cache, _skills_cache_cwd

    current_cwd = os.getcwd()
    if _skills_cache_cwd is not None and _skills_cache_cwd != current_cwd:
        refresh = True  # cwd changed, invalidate cache

    if _skills_cache is not None and not refresh:
        skills = dict(_skills_cache)
    else:
        skills: Dict[str, Dict[str, Any]] = {}

        # 1. Bundled skills (shipped with AutoRUN)
        bundled_dir = _get_bundled_skills_dir()
        if bundled_dir and os.path.isdir(bundled_dir):
            _load_skills_from_dir(skills, bundled_dir, "bundled")

        # 2. User skills (~/.autorun/skills/)
        user_skills_dir = os.path.expanduser("~/.autorun/skills")
        if os.path.isdir(user_skills_dir):
            _load_skills_from_dir(skills, user_skills_dir, "user")

        # 3. Project-local skills (./.autorun/skills/)
        project_skills_dir = os.path.join(os.getcwd(), ".autorun", "skills")
        if os.path.isdir(project_skills_dir):
            _load_skills_from_dir(skills, project_skills_dir, "project")

        _skills_cache = skills
        _skills_cache_cwd = current_cwd

    # 过滤掉禁用的技能
    if disabled_skills:
        skills = {k: v for k, v in skills.items() if k not in disabled_skills}

    return skills


def _get_bundled_skills_dir() -> Optional[str]:
    """获取内置技能目录的路径。"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # skills directory is AutoRUN_v1/skills/
    bundled = os.path.join(current_dir, "bundled")
    if os.path.isdir(bundled):
        return bundled
    return None


def _load_skills_from_dir(skills: Dict[str, Dict[str, Any]],
                          directory: str,
                          source: str) -> None:
    """从目录加载技能定义。

    技能文件可以是:
    - .json 文件，包含 {name, type, prompt/command}
    - .md 文件（名称来自文件名，type=prompt，内容作为提示词）
    """
    dir_path = Path(directory)

    for skill_file in sorted(dir_path.glob("*")):
        if skill_file.name.startswith("."):
            continue

        try:
            if skill_file.suffix == ".json":
                with open(skill_file, "r", encoding="utf-8") as f:
                    skill_def = json.load(f)
                name = skill_def.get("name", skill_file.stem)
                skill_def["_source"] = source
                skills[name] = skill_def

            elif skill_file.suffix == ".md":
                with open(skill_file, "r", encoding="utf-8") as f:
                    prompt_text = f.read()
                name = skill_file.stem
                skills[name] = {
                    "name": name,
                    "type": "prompt",
                    "description": f"User skill: {name}",
                    "prompt": prompt_text,
                    "_source": source,
                }
        except (json.JSONDecodeError, IOError, OSError):
            # Skip invalid skill files silently
            pass


def discover_memory_files(refresh: bool = False) -> Dict[str, str]:
    """从 ~/.autorun/memory/ 发现 memory 文件。"""
    global _memory_cache

    if _memory_cache is not None and not refresh:
        return _memory_cache

    memory: Dict[str, str] = {}
    memory_dir = os.path.join(os.path.expanduser("~"), ".autorun", "memory")

    if os.path.isdir(memory_dir):
        for mem_file in sorted(Path(memory_dir).glob("*.md")):
            try:
                with open(mem_file, "r", encoding="utf-8") as f:
                    content = f.read()
                memory[mem_file.stem] = content
            except (IOError, OSError):
                pass

    _memory_cache = memory
    return memory


def get_skill(name: str, disabled_skills: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """按名称获取特定技能。"""
    skills = discover_skills(disabled_skills=disabled_skills)
    return skills.get(name)


def list_skill_names(disabled_skills: Optional[set] = None) -> List[str]:
    """列出所有可用技能名称。"""
    return sorted(discover_skills(disabled_skills=disabled_skills).keys())


def register_skills_to_tool(disabled_skills: Optional[set] = None) -> None:
    """将发现的技能注册到 SkillTool。"""
    from AutoRUN_v1.tools.skill_tool import register_skill

    skills = discover_skills(disabled_skills=disabled_skills)
    for name, skill_def in skills.items():
        register_skill(name, skill_def)


def clear_skills_cache() -> None:
    """清除技能缓存（用于测试）。"""
    global _skills_cache, _memory_cache
    _skills_cache = None
    _memory_cache = None
