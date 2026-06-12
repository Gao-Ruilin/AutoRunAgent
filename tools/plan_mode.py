"""
Plan mode tools — EnterPlanMode and ExitPlanMode.

Plan mode allows the assistant to design an implementation approach
and get user approval before writing any code.
"""

import os
from typing import Any, Dict

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


# Global plan state
_plan_mode_active = False
_plan_content = ""


class EnterPlanModeTool(Tool):
    """Enter plan mode to design an approach before implementing."""

    @property
    def name(self) -> str:
        return "EnterPlanMode"

    @property
    def description(self) -> str:
        return """在即将开始非平凡的实现任务时主动使用此工具。
在编写代码之前获得用户对方法的认可可以避免浪费精力并确保一致性。
此工具将你切换到计划模式，在此模式下你可以探索代码库并设计实现方案供用户批准。

## 何时使用此工具

对于实现任务应优先使用 EnterPlanMode，除非它们很简单。当以下任一条件适用时使用它：

1. 新功能实现：添加有意义的新功能
   - 例如："添加一个退出登录按钮" — 它应该放在哪里？点击后应该发生什么？
   - 例如："添加表单验证" — 什么规则？什么错误消息？

2. 多种有效方法：任务可以用几种不同的方式解决
   - 例如："给 API 添加缓存" — 可以用 Redis、内存缓存、文件缓存等
   - 例如："提高性能" — 有许多优化策略可选

3. 代码修改：影响现有行为或结构的更改
   - 例如："更新登录流程" — 具体应该改变什么？
   - 例如："重构这个组件" — 目标架构是什么？

4. 架构决策：任务需要在模式或技术之间做选择
   - 例如："添加实时更新" — WebSocket vs SSE vs 轮询
   - 例如："实现状态管理" — Redux vs Context vs 自定义方案

5. 多文件更改：任务可能会涉及 2-3 个以上文件
   - 例如："重构认证系统"
   - 例如："添加新的 API 端点及测试"

6. 需求不明确：需要先探索才能理解完整范围
   - 例如："让应用更快" — 需要分析找出瓶颈
   - 例如："修复结账时的 bug" — 需要调查根本原因

7. 用户偏好重要：实现方式可能有多种合理选择
   - 如果你会用 AskUserQuestion 来澄清方法，改用 EnterPlanMode
   - 计划模式让你先探索，然后用上下文展示选项

## 何时不使用此工具

对于简单任务才跳过 EnterPlanMode：
- 单行或几行的修复（拼写错误、明显的 bug、小调整）
- 添加一个需求明确的单个函数
- 用户已给出非常具体、详细的指令
- 纯粹的研究/探索任务

## 计划模式中会发生什么

在计划模式中，你将：
1. 彻底探索代码库
2. 理解现有模式和架构
3. 设计实现方案
4. 向用户展示计划以供批准
5. 如果需要澄清方法则使用 AskUserQuestion
6. 准备好实现时使用 ExitPlanMode 退出计划模式

## 重要说明

- 此工具需要用户批准 — 他们必须同意进入计划模式
- 如果不确定是否使用，宁可多规划 — 提前达成一致比返工更好
- 用户希望在代码库做重大更改之前被征求意见"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        global _plan_mode_active
        _plan_mode_active = True

        # 确保计划目录存在
        cwd = context.cwd or os.getcwd()
        plan_dir = os.path.join(cwd, ".autorun_plans")
        os.makedirs(plan_dir, exist_ok=True)

        plan_file = os.path.join(plan_dir, "current_plan.md")
        return ToolResult(
            data="[Plan mode activated] You are now in plan mode. Explore the codebase, "
                 f"design your approach, and write your plan to `{plan_file}`. "
                 "Use ExitPlanMode when ready to submit the plan for user approval.",
            is_error=False,
        )


class ExitPlanModeTool(Tool):
    """Exit plan mode and submit the plan for user approval."""

    @property
    def name(self) -> str:
        return "ExitPlanMode"

    @property
    def description(self) -> str:
        return """当你在计划模式中并已完成计划编写，准备好供用户批准时使用此工具。

## 此工具如何工作
- 你应该已经将计划写入计划文件
- 此工具不接受计划内容作为参数 — 它会从你写入的文件中读取计划
- 此工具仅表示你已完成规划并准备好供用户审查和批准
- 用户在审查时会看到你的计划文件内容

## 何时使用此工具
仅当任务需要规划需要编写代码的实现的步骤时才使用此工具。
对于收集信息、搜索文件、读取文件或一般尝试理解代码库的研究任务 — 不要使用此工具。

## 使用此工具之前
确保你的计划是完整且明确的：
- 如果对需求或方法有未解决的问题，先使用 AskUserQuestion
- 一旦计划最终确定，使用此工具请求批准

重要：不要使用 AskUserQuestion 来询问"这个计划可以吗？"或"我应该继续吗？"—
这正是此工具的作用。ExitPlanMode 本身就是在请求用户批准你的计划。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "allowedPrompts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": ["Bash"],
                                "description": "The tool this prompt applies to",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Semantic description of the action (e.g., run tests, install deps)",
                            },
                        },
                    },
                },
            },
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        global _plan_mode_active, _plan_content
        _plan_mode_active = False

        allowed_prompts = args.get("allowedPrompts", [])
        prompts_summary = ", ".join(p.get("prompt", "") for p in allowed_prompts) if allowed_prompts else "none"

        # 读取计划文件内容
        cwd = context.cwd or os.getcwd()
        plan_file = os.path.join(cwd, ".autorun_plans", "current_plan.md")
        plan_text = ""
        try:
            if os.path.exists(plan_file):
                with open(plan_file, "r", encoding="utf-8") as f:
                    plan_text = f.read().strip()
                _plan_content = plan_text
        except Exception:
            pass

        if plan_text:
            # 展示计划内容给用户审查
            display = (
                "---\n\n"
                "## 计划已提交供审批\n\n"
                + plan_text +
                "\n\n---\n\n"
                "**请审查以上计划。**\n"
                '- 回复 "批准" 或 "继续" 以批准并开始实现\n'
                "- 回复你的修改意见以调整计划\n"
                f"\n允许的执行提示: {prompts_summary}"
            )
        else:
            display = (
                f"[Plan mode exited] 未找到计划文件 ({plan_file})。\n"
                "请将计划写入该文件后重新调用 ExitPlanMode。\n"
                f"允许的执行提示: {prompts_summary}"
            )

        return ToolResult(
            data=display,
            is_error=not bool(plan_text),
        )


def is_plan_mode_active() -> bool:
    """Check if plan mode is currently active."""
    return _plan_mode_active
