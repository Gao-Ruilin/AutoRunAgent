"""
WorkflowTool — 工作流定义与管理。

工作流由门控Agent 在对话中自然创建，创建时即保存到文件。
执行时，门控Agent 加载工作流定义并逐步调度（Agent分发、Bash命令、用户确认等）。

不使用 action 参数派发 — 根据提供的参数自动判断操作:
- 提供 steps → 创建/保存工作流
- 只提供 name → 加载工作流定义（供门控Agent 执行）
- 无参数 → 列出已保存的工作流
- 提供 name + delete=true → 删除工作流

新增特性:
- emergency: 步骤级紧急超时（秒），confirm 步骤超时后门控Agent 代行决策
- next_step: Agent 步骤完成后路由到下一步（支持条件分支）
- output_to: 将 Agent 输出传递给后续步骤的变量
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class WorkflowTool(Tool):
    """定义、保存和执行工作流。

    工作流是一系列步骤的序列。门控Agent 使用此工具来:
    - 创建并保存工作流（提供 steps 时自动保存）
    - 列出已保存的工作流
    - 加载工作流并逐步执行
    """

    @property
    def name(self) -> str:
        return "Workflow"

    @property
    def description(self) -> str:
        return """创建和管理可复用工作流。

**创建/更新工作流（提供 name + steps）**
当你和用户讨论并确定了一个工作流程后，使用此工具创建并保存。
工作流在创建时就保存到文件，无需额外操作。

**加载工作流（只提供 name）**
加载已保存的工作流定义。拿到步骤列表后，按顺序逐步执行：
每一步的 type 决定执行方式:
- agent → 使用 Agent 工具分发给对应 Agent
- bash → 使用 Bash 工具执行命令
- confirm → 使用 AskUserQuestion 工具向用户确认

**列出工作流（无参数）**
查看所有已保存的工作流。

**删除工作流（提供 name + delete=true）**

工作流存储位置: ~/.autorun/workflows/<name>.json

步骤格式:
  {"type": "agent", "agent": "agent名称", "description": "简短描述", "prompt": "指令"}
  {"type": "bash", "command": "shell命令"}
  {"type": "confirm", "message": "确认消息"}

**紧急模式（emergency）**
可在步骤上设置 "emergency": N（秒数）。对于 confirm 步骤，如果用户在 N 秒内
没有回复，门控Agent 将自主决策推进工作流。对于 agent 步骤，若后台 Agent
超过 N 秒未完成，门控Agent 将取消它并继续。

**步骤路由（next_step）**
Agent 步骤可通过 "next_step" 字段指定完成后的路由:
- 数值: 跳转到指定步骤（从0开始）
- 对象: {"default": 3, "conditions": [{"if": "包含关键词X", "goto": 2}]}
  根据 Agent 输出包含的关键词决定跳转

**变量传递（output_to）**
设置 "output_to": "varname" 将 Agent/Bash 输出存储为变量，
后续步骤的 prompt/command 中可用 {varname} 引用。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "工作流名称（创建、加载或删除时提供）",
                },
                "description": {
                    "type": "string",
                    "description": "工作流用途描述（创建时提供，帮助理解何时使用）",
                },
                "steps": {
                    "type": "array",
                    "description": "工作流步骤列表。提供此参数时自动创建并保存工作流。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["agent", "bash", "confirm", "parallel", "gatekeeper"]},
                            "agent": {"type": "string", "description": "目标Agent名称（type=agent时）"},
                            "description": {"type": "string", "description": "步骤简述"},
                            "prompt": {"type": "string", "description": "给Agent的指令（type=agent时）。可用 {变量名} 引用前置步骤输出。"},
                            "command": {"type": "string", "description": "Shell命令（type=bash时）"},
                            "message": {"type": "string", "description": "确认消息（type=confirm时）"},
                            "emergency": {"type": "integer", "description": "紧急超时秒数。0=不启用。confirm超时后门控Agent代行决策，agent超时后取消。"},
                            "next_step": {"description": "Agent完成后的路由。可以是步骤索引(int)或条件路由对象。默认顺序执行。"},
                            "output_to": {"type": "string", "description": "将本步骤输出存储为变量名，后续步骤可通过 {变量名} 引用"},
                            "steps": {"type": "array", "description": "并行子步骤（type=parallel时）。每个子步骤格式同上。"},
                        },
                    },
                },
                "delete": {
                    "type": "boolean",
                    "description": "设为true删除指定名称的工作流",
                },
            },
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        # 只有列出和加载是只读的
        return not args.get("steps") and not args.get("delete")

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        workflows_dir = os.path.expanduser("~/.autorun/workflows")
        name = (args.get("name", "") or "").strip()

        # ── 删除 ──
        if args.get("delete") and name:
            return self._delete(name, workflows_dir)

        # ── 创建/更新（提供了 steps）──
        if args.get("steps"):
            return self._create(args, workflows_dir)

        # ── 加载 ──
        if name:
            return self._load(name, workflows_dir)

        # ── 列出 ──
        return self._list(workflows_dir)

    def _create(self, args: Dict[str, Any], workflows_dir: str) -> ToolResult:
        """创建并立即保存工作流。"""
        name = (args.get("name", "") or "").strip()
        if not name:
            return ToolResult(data="错误: 创建需要提供 name", is_error=True)
        if "/" in name or "\\" in name:
            return ToolResult(data="错误: 名称不能包含路径分隔符", is_error=True)
        if len(name) > 64:
            return ToolResult(data="错误: 名称不能超过64个字符", is_error=True)

        steps = args.get("steps", [])
        if not steps:
            return ToolResult(data="错误: 必须提供 steps", is_error=True)

        description = (args.get("description", "") or "").strip()

        workflow_def = {
            "name": name,
            "description": description,
            "steps": steps,
        }

        os.makedirs(workflows_dir, exist_ok=True)
        out_path = os.path.join(workflows_dir, f"{name}.json")
        existed = os.path.isfile(out_path)

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(workflow_def, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return ToolResult(data=f"保存失败: {e}", is_error=True)

        action = "已更新" if existed else "已创建"
        return ToolResult(
            data=f"工作流 '{name}' {action}并已保存。\n"
                 f"文件: {out_path}\n"
                 f"步骤数: {len(steps)}\n"
                 f"描述: {description or '(无)'}\n"
                 f"\n之后可通过 Workflow(name=\"{name}\") 加载执行。",
            is_error=False,
        )

    def _list(self, workflows_dir: str) -> ToolResult:
        """列出所有已保存的工作流。"""
        if not os.path.isdir(workflows_dir):
            return ToolResult(
                data="没有已保存的工作流。",
                is_error=False,
            )

        workflows = []
        for f in sorted(Path(workflows_dir).glob("*.json")):
            if f.name.startswith("."):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # 统计紧急步骤和路由步骤
                steps = data.get("steps", [])
                emergency_count = sum(1 for s in steps if s.get("emergency", 0) > 0)
                route_count = sum(1 for s in steps if s.get("next_step") is not None)
                workflows.append({
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "steps_count": len(steps),
                    "emergency_steps": emergency_count,
                    "route_steps": route_count,
                })
            except (json.JSONDecodeError, OSError):
                pass

        if not workflows:
            return ToolResult(data="没有已保存的工作流。", is_error=False)

        lines = [f"已保存的工作流 ({len(workflows)}):"]
        for w in workflows:
            desc = w.get("description", "")
            desc_str = f" — {desc}" if desc else ""
            flags = []
            if w.get("emergency_steps", 0) > 0:
                flags.append(f"紧急x{w['emergency_steps']}")
            if w.get("route_steps", 0) > 0:
                flags.append(f"路由x{w['route_steps']}")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {w['name']}{desc_str}  [{w['steps_count']}步]{flag_str}")

        lines.append(f"\n使用 Workflow(name=\"<名称>\") 加载并执行。")
        return ToolResult(data="\n".join(lines), is_error=False)

    def _load(self, name: str, workflows_dir: str) -> ToolResult:
        """加载工作流定义，返回步骤供门控Agent 执行。"""
        path = os.path.join(workflows_dir, f"{name}.json")

        if not os.path.isfile(path):
            return ToolResult(
                data=f"未找到工作流 '{name}'。使用 Workflow() 查看已保存的列表。",
                is_error=True,
            )

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return ToolResult(data=f"加载失败: {e}", is_error=True)

        wf_steps = data.get("steps", [])
        wf_desc = data.get("description", "")

        # 生成执行指令给门控Agent
        parts = [
            f"已加载工作流: {name}",
            f"描述: {wf_desc or '(无)'}",
            f"共 {len(wf_steps)} 步",
            "",
            "【执行规则】",
            "1. 默认按步骤顺序执行。Agent 步骤若有 next_step 字段，按其路由跳转。",
            "2. 遇到 gatekeeper 类型步骤时，你（门控Agent）自行判断并处理。",
            "3. 紧急步骤（emergency>0）: confirm 超时后你代行决策；agent 超时后取消并继续。",
            "4. Agent 步骤的 output_to 变量可供后续步骤通过 {变量名} 引用。",
            "5. 子Agent 遇到预设外情况时，会在输出中以 [需要门控] 开头说明，由你处理。",
            "",
            "请按顺序逐步执行以下步骤:",
            "",
        ]

        for i, step in enumerate(wf_steps):
            t = step.get("type", "?")
            emergency = step.get("emergency", 0)
            next_step = step.get("next_step")
            output_to = step.get("output_to", "")
            em_flag = f" [紧急 {emergency}s]" if emergency > 0 else ""
            out_flag = f" → ${output_to}" if output_to else ""

            parts.append(f"--- 步骤 {i+1}/{len(wf_steps)} ---")

            if t == "agent":
                agent = step.get("agent", "general-purpose")
                desc = step.get("description", "")
                prompt = step.get("prompt", "")
                parts.append(f"[Agent: {agent}]{em_flag} {desc}{out_flag}")
                parts.append(f"指令: {prompt}")
                parts.append(f"→ 使用 Agent(subagent_type=\"{agent}\") 执行")
                if emergency > 0:
                    parts.append(f"⚠ 紧急: 若 {emergency}s 内未完成，取消并继续下一步。")
                if next_step is not None:
                    parts.append(f"🔀 路由: 完成后根据输出跳转到 → {self._format_next_step(next_step)}")

            elif t == "bash":
                cmd = step.get("command", "")
                parts.append(f"[Bash]{em_flag} {cmd}{out_flag}")
                parts.append(f"→ 使用 Bash 工具执行")
                if next_step is not None:
                    parts.append(f"🔀 路由: {self._format_next_step(next_step)}")

            elif t == "confirm":
                msg = step.get("message", "")
                parts.append(f"[确认]{em_flag} {msg}")
                parts.append(f"→ 使用 AskUserQuestion 确认")
                if emergency > 0:
                    parts.append(f"⚠ 紧急: 若 {emergency}s 内用户无回复，你（门控Agent）自行判断并继续。")

            elif t == "gatekeeper":
                prompt = step.get("prompt", "")
                parts.append(f"[门控Agent]{em_flag} {step.get('description', '')}")
                parts.append(f"→ 你（门控Agent）自行处理: {prompt}")
                if emergency > 0:
                    parts.append(f"⚠ 紧急: 限时 {emergency}s 内完成此判断。")

            elif t == "parallel":
                subs = step.get("steps", [])
                parts.append(f"[并行]{em_flag} {len(subs)} 个子步骤同时执行")
                for s in subs:
                    st = s.get("type", "?")
                    if st == "agent":
                        parts.append(f"  Agent({s.get('agent','?')}): {s.get('description','')}")
                    elif st == "bash":
                        parts.append(f"  Bash: {s.get('command','')}")

            parts.append("")

        parts.append(f"=== 工作流 '{name}' 结束 ===")
        return ToolResult(data="\n".join(parts), is_error=False)

    def _format_next_step(self, next_step: Any) -> str:
        """格式化 next_step 路由信息。"""
        if isinstance(next_step, int):
            return f"步骤 {next_step + 1}"
        if isinstance(next_step, dict):
            default = next_step.get("default", "?")
            conditions = next_step.get("conditions", [])
            cond_strs = []
            for c in conditions:
                cond_strs.append(f"'{c.get('if','?')}' → 步骤 {c.get('goto',0)+1}")
            return f"默认→步骤{default+1}, 条件: {', '.join(cond_strs)}"
        return str(next_step)

    def _delete(self, name: str, workflows_dir: str) -> ToolResult:
        """删除工作流。"""
        path = os.path.join(workflows_dir, f"{name}.json")

        if not os.path.isfile(path):
            return ToolResult(
                data=f"未找到工作流 '{name}'。",
                is_error=True,
            )

        try:
            os.remove(path)
        except OSError as e:
            return ToolResult(data=f"删除失败: {e}", is_error=True)

        return ToolResult(
            data=f"工作流 '{name}' 已删除。",
            is_error=False,
        )
