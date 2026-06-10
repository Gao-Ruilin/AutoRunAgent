"""
Slash command system.

Provides a command registry for / slash commands used in the interactive REPL.
"""

import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CommandHandler = Callable[[str, Any], Optional[str]]


class CommandRegistry:
    """Registry for slash commands."""

    def __init__(self):
        self._commands: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, handler: CommandHandler,
                 description: str = "", aliases: List[str] = None,
                 hidden: bool = False) -> None:
        self._commands[name] = {
            "name": name,
            "handler": handler,
            "description": description,
            "aliases": aliases or [],
            "hidden": hidden,
        }

    def execute(self, cmd_line: str, state: Any) -> Optional[str]:
        parts = cmd_line.split(maxsplit=1)
        cmd_name = parts[0].lower()
        if cmd_name.startswith("/"):
            cmd_name = cmd_name[1:]
        args_str = parts[1] if len(parts) > 1 else ""

        for name, cmd in self._commands.items():
            if cmd_name == name or cmd_name in cmd.get("aliases", []):
                try:
                    return cmd["handler"](args_str, state)
                except Exception as e:
                    return f"Command error: {e}"

        return f"Unknown command: /{cmd_name}. Type /help for available commands."

    def get_all_commands(self) -> List[Dict[str, Any]]:
        return [
            {"name": cmd["name"], "description": cmd["description"],
             "aliases": cmd["aliases"], "hidden": cmd["hidden"]}
            for cmd in self._commands.values()
        ]

    def get_visible_commands(self) -> List[Dict[str, Any]]:
        return [c for c in self.get_all_commands() if not c["hidden"]]


# ── Command Handlers ────────────────────────────────────────────────────────

def _cmd_help(args: str, state: Any) -> Optional[str]:
    """Show help."""
    try:
        if hasattr(state, 'size') and state.size:
            width = state.size.width
        elif hasattr(state, 'application'):
            width = state.application.output.get_size().columns
        else:
            width = 80
    except Exception:
        logger.debug("Failed to detect terminal width, using default 80", exc_info=True)
        width = 80
    hr = "\u2500" * width

    lines = [hr, "  Commands:", ""]
    for cmd in _global_registry.get_visible_commands():
        name = cmd['name']
        aliases_str = ""
        if cmd.get("aliases"):
            clean = [a for a in cmd["aliases"] if a != name.lstrip("/")]
            if clean:
                aliases_str = f"  (/{', /'.join(clean)})"
        lines.append(f"  \u276f {name}{aliases_str}")
        if cmd["description"]:
            lines.append(f"    {cmd['description']}")
    lines.extend([
        "", hr,
        "  Keyboard shortcuts:", "",
        "  Ctrl+C       Interrupt / Exit",
        "  Ctrl+D       Exit (EOF)",
        "  Ctrl+L       Clear screen",
        "  Alt+Enter    Insert newline",
        "  Shift+Tab    Cycle permission mode",
        "  PageUp/Down  Scroll messages",
        "  \u2191/\u2193        History navigation",
        "", hr,
    ])
    return "\n".join(lines)


def _cmd_exit(args: str, state: Any) -> Optional[str]:
    if hasattr(state, 'running'):
        state.running = False
    return None


def _cmd_clear(args: str, state: Any) -> Optional[str]:
    # state is the UI app instance; extract the actual AppState
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state
    if app_state is not None and hasattr(app_state, 'clear_messages'):
        app_state.clear_messages()
    elif hasattr(state, 'clear_messages'):
        state.clear_messages()
    return "Conversation cleared."


def _cmd_model(args: str, state: Any) -> Optional[str]:
    """Show or set the current model."""
    if args.strip():
        new_model = args.strip()
        from AutoRUN_v1.utils.config import set_model
        set_model(new_model)
        os.environ["AUTORUN_MODEL"] = new_model
        from AutoRUN_v1.api.client import reset_client
        reset_client()
        return f"Model set to: {new_model}"
    from AutoRUN_v1.utils.config import get_model
    current = get_model()
    if current:
        return f"Current model: {current}\nUse /model <name> to change."
    return "Model not set. Use /model <name> to set."


def _cmd_context(args: str, state: Any) -> Optional[str]:
    """Show or set the model's context window size (in tokens)."""
    from AutoRUN_v1.utils.config import get_context_window, set_context_window

    args = args.strip()
    if not args:
        current = get_context_window()
        return f"Context window: {current:,} tokens ({current // 1000}k)\nUse /context <tokens> to change."

    try:
        tokens = int(args)
        if tokens < 1000:
            return "Context window must be at least 1000 tokens."
        set_context_window(tokens)
        return f"Context window set to: {tokens:,} tokens ({tokens // 1000}k)"
    except ValueError:
        return f"Invalid number: {args}\nExample: /context 128000"


def _cmd_api(args: str, state: Any) -> Optional[str]:
    """Configure API settings: type, url, key."""
    from AutoRUN_v1.utils.config import (
        get_api_type, get_api_url, get_api_key, get_model,
        set_api_type, set_api_url, save_api_key,
    )
    from AutoRUN_v1.api.client import reset_client

    parts = args.strip().split(maxsplit=1)
    if not args.strip():
        api_type = get_api_type()
        api_url = get_api_url() or "(not set)"
        api_key = get_api_key()
        model = get_model() or "(not set)"
        key_disp = api_key[:8] + "..." + api_key[-4:] if api_key and len(api_key) > 12 else "***" if api_key else "(not set)"
        return (
            f"Current API configuration:\n"
            f"  Type: {api_type}\n"
            f"  URL:  {api_url}\n"
            f"  Key:  {key_disp}\n"
            f"  Model: {model}\n"
            f"\n"
            f"Set:\n"
            f"  /api type <openai|anthropic>  -- set API type\n"
            f"  /api url <full URL>           -- set API base URL\n"
            f"  /api key <key>                -- set API key\n"
            f"  /model <name>                 -- set model name"
        )

    subcmd = parts[0].lower()
    subargs = parts[1] if len(parts) > 1 else ""

    if subcmd == "type":
        if subargs not in ("openai", "anthropic"):
            return "API type must be 'openai' or 'anthropic'."
        set_api_type(subargs)
        reset_client()
        return f"API type set to: {subargs}"

    elif subcmd == "url":
        if not subargs:
            return "Please provide an API URL.\nExample: /api url https://api.openai.com"
        if not subargs.startswith(("http://", "https://")):
            subargs = "https://" + subargs
        set_api_url(subargs)
        reset_client()
        return f"API URL set to: {subargs}"

    elif subcmd == "key":
        if not subargs:
            return "Please provide an API key.\nExample: /api key sk-xxxx"
        save_api_key(subargs)
        os.environ["AUTORUN_API_KEY"] = subargs
        reset_client()
        masked = subargs[:8] + "..." + subargs[-4:] if len(subargs) > 12 else "***"
        return f"API key saved: {masked}"

    else:
        return f"Unknown subcommand: {subcmd}. Supported: type, url, key"


def _cmd_status(args: str, state: Any) -> Optional[str]:
    """Show session status."""
    from AutoRUN_v1.state.app_state import get_app_state
    from AutoRUN_v1.utils.config import get_model, get_api_type, get_api_url, get_context_window

    # state is the UI app instance; extract the actual AppState
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state
    if app_state is None:
        app_state = get_app_state()
    msg_count = len(app_state.get_messages())
    model = get_model() or "(not set)"
    api_type = get_api_type()
    api_url = get_api_url() or "(not set)"
    ctx = get_context_window()
    lines = [
        f"Session ID: {app_state.session_id or '(not set)'}",
        f"CWD: {app_state.cwd}",
        f"Messages: {msg_count}",
        f"Tools enabled: {len(app_state.enabled_tools)}",
        f"Permission mode: {app_state.permission_mode}",
        f"Plan mode: {'yes' if app_state.plan_mode_active else 'no'}",
        f"API type: {api_type}",
        f"API URL:  {api_url}",
        f"Model: {model}",
        f"Context: {ctx:,} tokens ({ctx // 1000}k)",
    ]
    return "\n".join(lines)


def _cmd_compact(args: str, state: Any) -> Optional[str]:
    from AutoRUN_v1.services.compact import manual_compact
    # state is the UI app instance; extract the actual AppState
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state
    else:
        app_state = None
    return manual_compact(app_state)


def _cmd_memory(args: str, state: Any) -> Optional[str]:
    from AutoRUN_v1.skills.loader import discover_memory_files
    memories = discover_memory_files(refresh=True)
    if memories:
        return f"Memory files ({len(memories)}):\n" + "\n".join(f"  - {name}.md" for name in memories)
    return "Memory system not initialized (no .md files in ~/.autorun/memory/)."


def _cmd_todos(args: str, state: Any) -> Optional[str]:
    from AutoRUN_v1.tools.task_tool import get_all_tasks
    tasks = get_all_tasks(state)
    if not tasks:
        return "Task list empty."
    icons = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "deleted": "[-]"}
    lines = ["Current Task list:"]
    for t in tasks:
        icon = icons.get(t.get("status", "pending"), "[?]")
        lines.append(f"  #{t['id']} {icon} {t.get('subject', t.get('label', ''))}")
    return "\n".join(lines)


def _cmd_fast(args: str, state: Any) -> Optional[str]:
    current = os.environ.get("AUTORUN_SIMPLE", "0")
    if current in ("1", "true", "yes"):
        os.environ["AUTORUN_SIMPLE"] = "0"
        return "Fast mode: off"
    else:
        os.environ["AUTORUN_SIMPLE"] = "1"
        return "Fast mode: on"


def _cmd_skill(args: str, state: Any) -> Optional[str]:
    """List available skills."""
    from AutoRUN_v1.skills.loader import discover_skills

    skills = discover_skills(refresh=True)
    if not skills:
        return "没有已加载的 skill。\n\nSkill 存放位置:\n  ~/.autorun/skills/   (用户 skill)\n  ./.autorun/skills/   (项目 skill)\n  AutoRUN_v1/skills/bundled/  (内置 skill)\n\n将 .json 或 .md 文件放入上述目录即可使用。"

    lines = ["已加载的 skill:"]
    for name, skill_def in sorted(skills.items()):
        source = skill_def.get("_source", "unknown")
        desc = skill_def.get("description", "")
        skill_type = skill_def.get("type", "?")
        source_label = {"bundled": "[内置]", "user": "[用户]", "project": "[项目]"}.get(source, f"[{source}]")
        lines.append(f"  - {source_label} {name} ({skill_type})")
        if desc:
            lines.append(f"      {desc}")

    lines.append(f"\n共 {len(skills)} 个 skill。")
    lines.append(f"\nSkill 存放位置:")
    lines.append(f"  ~/.autorun/skills/    用户 skill")
    lines.append(f"  ./.autorun/skills/    项目 skill")
    lines.append(f"  skills/bundled/       内置 skill")
    return "\n".join(lines)


# ── Resume command ────────────────────────────────────────────────────────

RESUME_MARKER = "__RESUME__"


def _cmd_resume(args: str, state: Any) -> Optional[str]:
    """触发对话恢复流程。返回特殊标记由 UI 层拦截。"""
    return RESUME_MARKER


# ── Skill toggle commands ──────────────────────────────────────────────────

def _cmd_skills_status(args: str, state: Any) -> Optional[str]:
    """显示所有 skill 及启用/禁用状态。"""
    from AutoRUN_v1.skills.loader import discover_skills

    all_skills = discover_skills(refresh=True)
    disabled = getattr(state, 'disabled_skills', set()) if state else set()

    if not all_skills:
        return "没有已加载的 skill。"

    lines = ["Skill 状态:"]
    for name in sorted(all_skills.keys()):
        status = "✗ 已禁用" if name in disabled else "✓ 已启用"
        skill_def = all_skills[name]
        desc = skill_def.get("description", "")
        source = skill_def.get("_source", "unknown")
        source_label = {"bundled": "[内置]", "user": "[用户]", "project": "[项目]"}.get(source, f"[{source}]")
        lines.append(f"  {status} {source_label} {name}")
        if desc:
            lines.append(f"      {desc}")

    enabled_count = len(all_skills) - len(disabled)
    lines.append(f"\n已启用: {enabled_count}, 已禁用: {len(disabled)}, 共 {len(all_skills)}")
    return "\n".join(lines)


def _cmd_skills_toggle(args: str, state: Any) -> Optional[str]:
    """切换 skills 启用状态。无参数时进入选择模式。"""
    from AutoRUN_v1.skills.loader import discover_skills, clear_skills_cache, register_skills_to_tool

    name = args.strip()
    if not name:
        # 无参数：返回标记让 UI 显示选择列表
        return RESUME_MARKER  # 复用 marker，UI 层判断上下文

    all_skills = discover_skills(refresh=True)
    if name not in all_skills:
        return f"Skill '{name}' 不存在。"

    # Extract AppState from UI app instance
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state

    if app_state is None:
        return "无法获取应用状态。"

    disabled = app_state._get_disabled_skills() if app_state else set()
    if name in disabled:
        app_state.enable_skill(name)
        action = "已启用"
    else:
        app_state.disable_skill(name)
        action = "已禁用"

    # 刷新
    clear_skills_cache()
    updated_disabled = app_state._get_disabled_skills()
    _ = discover_skills(refresh=True, disabled_skills=updated_disabled)
    register_skills_to_tool(disabled_skills=updated_disabled)

    return f"Skill '{name}' {action}。"


def _cmd_skills_enable(args: str, state: Any) -> Optional[str]:
    """启用指定 skill。"""
    from AutoRUN_v1.skills.loader import discover_skills, clear_skills_cache, register_skills_to_tool

    name = args.strip()
    if not name:
        return "用法: /skills-enable <name>"

    all_skills = discover_skills(refresh=True)
    if name not in all_skills:
        return f"Skill '{name}' 不存在。"

    # Extract AppState from UI app instance
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state

    if app_state:
        app_state.enable_skill(name)

    clear_skills_cache()
    disabled = app_state._get_disabled_skills() if app_state else set()
    _ = discover_skills(refresh=True, disabled_skills=disabled)
    register_skills_to_tool(disabled_skills=disabled)

    return f"Skill '{name}' 已启用。"


def _cmd_skills_disable(args: str, state: Any) -> Optional[str]:
    """禁用指定 skill。"""
    from AutoRUN_v1.skills.loader import discover_skills, clear_skills_cache, register_skills_to_tool

    name = args.strip()
    if not name:
        return "用法: /skills-disable <name>"

    all_skills = discover_skills(refresh=True)
    if name not in all_skills:
        return f"Skill '{name}' 不存在。"

    # Extract AppState from UI app instance
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state

    if app_state:
        app_state.disable_skill(name)

    clear_skills_cache()
    disabled = app_state._get_disabled_skills() if app_state else set()
    _ = discover_skills(refresh=True, disabled_skills=disabled)
    register_skills_to_tool(disabled_skills=disabled)

    return f"Skill '{name}' 已禁用。"


def _cmd_skills_enable_all(args: str, state: Any) -> Optional[str]:
    """启用所有 skill。"""
    from AutoRUN_v1.skills.loader import clear_skills_cache, discover_skills, register_skills_to_tool

    # Extract AppState from UI app instance
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state

    if app_state:
        app_state._get_disabled_skills().clear()

    clear_skills_cache()
    _ = discover_skills(refresh=True)
    register_skills_to_tool()

    return "所有 skill 已启用。"


def _cmd_skills_disable_all(args: str, state: Any) -> Optional[str]:
    """禁用所有 skill。"""
    from AutoRUN_v1.skills.loader import discover_skills, clear_skills_cache, register_skills_to_tool

    # Extract AppState from UI app instance
    app_state = None
    if hasattr(state, '_state'):
        app_state = state._state
    elif hasattr(state, 'state'):
        app_state = state.state

    all_skills = discover_skills(refresh=True)
    if app_state:
        for name in all_skills:
            app_state.disable_skill(name)

    clear_skills_cache()
    disabled = app_state._get_disabled_skills() if app_state else set()
    _ = discover_skills(refresh=True, disabled_skills=disabled)
    register_skills_to_tool(disabled_skills=disabled)

    return f"已禁用 {len(all_skills)} 个 skill。"


def _cmd_index(args: str, state: Any) -> Optional[str]:
    """管理文件索引: /index status | build | skip"""
    idx = getattr(state, "indexer", None) if state else None
    if idx is None:
        return "索引器未初始化。请等待引擎完全启动后重试。"

    action = args.strip().lower()

    if action in ("", "status"):
        if idx.is_building:
            return "索引正在构建中..."
        if idx.is_ready:
            return f"索引已就绪，共 {idx.file_count} 个文件。"
        if idx.needs_prompt():
            return (
                "项目文件索引尚未构建。\n"
                "  输入 /index build  开始构建（后台进行，不阻塞对话）\n"
                "  输入 /index skip   跳过"
            )
        return "索引未构建（用户已跳过）。使用 /index build 手动构建。"

    if action == "build":
        if idx.is_building:
            return "索引已在构建中..."
        if idx.is_ready:
            return f"索引已存在（{idx.file_count} 个文件）。如需重建，请先删除 .autorun/index/ 目录。"
        idx.mark_user_response(accepted=True)
        return "索引构建已开始，将在后台进行。"

    if action == "skip":
        idx.mark_user_response(accepted=False)
        return "已跳过索引构建。之后可使用 /index build 手动构建。"

    return f"未知操作: '{action}'。可用: status | build | skip"


def _cmd_agent(args: str, state: Any) -> Optional[str]:
    """列出已注册的下游 Agent。"""
    try:
        from AutoRUN_v1.services.agent_registry import discover_agents
        agents = discover_agents(refresh=True)
        if not agents:
            return (
                "没有已注册的下游 Agent。\n\n"
                "Agent 模板存储位置:\n"
                "  ~/.autorun/agents/    用户级 Agent\n"
                "  ./.autorun/agents/    项目级 Agent\n\n"
                "通过门控Agent 或直接使用 AgentManage 工具创建 Agent。"
            )
        lines = ["已注册的下游 Agent:"]
        for name, agent_def in sorted(agents.items()):
            source = agent_def.get("_source", "unknown")
            source_label = {"user": "[用户]", "project": "[项目]"}.get(source, f"[{source}]")
            desc = agent_def.get("description", "")
            model = agent_def.get("model", "")
            model_str = f" (模型: {model})" if model else ""
            lines.append(f"  {source_label} {name}{model_str}")
            if desc:
                lines.append(f"      {desc}")
        lines.append(f"\n共 {len(agents)} 个 Agent。")
        return "\n".join(lines)
    except Exception as e:
        return f"获取 Agent 列表失败: {e}"


def _cmd_workflow(args: str, state: Any) -> Optional[str]:
    """列出已保存的工作流。"""
    try:
        import os, json
        from pathlib import Path
        workflows_dir = os.path.expanduser("~/.autorun/workflows")
        if not os.path.isdir(workflows_dir):
            return (
                "没有已保存的工作流。\n\n"
                f"工作流存储目录: {workflows_dir}\n"
                "使用 Workflow 工具保存工作流。"
            )
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
            except Exception:
                pass
        if not workflows:
            return "没有已保存的工作流。"
        lines = ["已保存的工作流:"]
        for w in workflows:
            desc = w.get("description", "")
            desc_str = f" — {desc}" if desc else ""
            lines.append(f"  {w['name']}{desc_str} ({w['steps_count']} 步)")
        lines.append(f"\n共 {len(workflows)} 个工作流。")
        lines.append("使用 Workflow 工具 action=\"load\" name=\"<名称>\" 加载。")
        return "\n".join(lines)
    except Exception as e:
        return f"获取工作流列表失败: {e}"


# ── Global Registry ─────────────────────────────────────────────────────────

_global_registry = CommandRegistry()


def _register_defaults() -> None:
    _global_registry.register("/help", _cmd_help, "Show this help", aliases=["help", "h"])
    _global_registry.register("/exit", _cmd_exit, "Exit REPL", aliases=["exit", "quit", "q"])
    _global_registry.register("/clear", _cmd_clear, "Clear conversation", aliases=["clear"])
    _global_registry.register("/model", _cmd_model, "Show or set model", aliases=["model"])
    _global_registry.register("/context", _cmd_context, "Show or set context window size", aliases=["context", "ctx"])
    _global_registry.register("/api", _cmd_api, "Configure API (type/url/key)", aliases=["api", "config"])
    _global_registry.register("/status", _cmd_status, "Show session status", aliases=["status"])
    _global_registry.register("/compact", _cmd_compact, "Compact context", aliases=["compact"])
    _global_registry.register("/memory", _cmd_memory, "Show memory status", aliases=["memory"])
    _global_registry.register("/todos", _cmd_todos, "Show todo list", aliases=["todos", "tasks"])
    _global_registry.register("/fast", _cmd_fast, "Toggle fast mode", aliases=["fast"])
    _global_registry.register("/skill", _cmd_skill, "List available skills", aliases=["skill", "skills"])
    _global_registry.register("/resume", _cmd_resume, "Resume previous conversation", aliases=["resume", "r"])
    _global_registry.register("/skills-status", _cmd_skills_status, "Show skill enable/disable status", aliases=["skills-status"])
    _global_registry.register("/skills-toggle", _cmd_skills_toggle, "Toggle a skill on/off", aliases=["skills-toggle"])
    _global_registry.register("/skills-enable", _cmd_skills_enable, "Enable a skill", aliases=["skills-enable"])
    _global_registry.register("/skills-disable", _cmd_skills_disable, "Disable a skill", aliases=["skills-disable"])
    _global_registry.register("/skills-enable-all", _cmd_skills_enable_all, "Enable all skills", aliases=["skills-enable-all"])
    _global_registry.register("/skills-disable-all", _cmd_skills_disable_all, "Disable all skills", aliases=["skills-disable-all"])
    _global_registry.register("/index", _cmd_index, "Manage file index (status/build/skip)", aliases=["index"])
    _global_registry.register("/agent", _cmd_agent, "List registered downstream agents", aliases=["agent", "agents"])
    _global_registry.register("/workflow", _cmd_workflow, "List saved workflows", aliases=["workflow", "workflows"])


_register_defaults()


def get_registry() -> CommandRegistry:
    return _global_registry


def execute_command(cmd_line: str, state: Any = None) -> Optional[str]:
    return _global_registry.execute(cmd_line, state)


def is_command(text: str) -> bool:
    return text.strip().startswith("/")


def parse_command(text: str) -> Tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    name = parts[0].lstrip("/").lower()
    args = parts[1] if len(parts) > 1 else ""
    return name, args
