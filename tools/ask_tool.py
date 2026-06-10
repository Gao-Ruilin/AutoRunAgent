"""
AskUserQuestionTool — Ask the user questions during execution.

Mirrors src/tools/AskUserQuestionTool/ — allows the assistant to
gather user preferences or clarify ambiguous instructions.
"""

from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class AskUserQuestionTool(Tool):
    """Ask the user clarifying questions during task execution."""

    @property
    def name(self) -> str:
        return "AskUserQuestion"

    @property
    def description(self) -> str:
        return """在执行过程中需要向用户提问时使用此工具。这允许你:
1. 收集用户偏好或需求
2. 澄清模糊的指令
3. 在工作过程中获取用户对实现选择的决定
4. 向用户提供方向选择。

用法说明:
- 用户始终可以选择"其他"来提供自定义文本输入
- 使用 multiSelect: true 允许为一个问题选择多个答案
- 如果你推荐某个特定选项，将其放在列表的第一个并在标签末尾添加"（推荐）"

计划模式说明: 在计划模式中，使用此工具在最终确定计划之前澄清需求或选择方法。不要使用此工具询问"我的计划准备好了吗？"或"我应该继续吗？"

预览功能:
在呈现用户需要视觉比较的具体工件时，使用选项上的可选 preview 字段:
- UI 布局或组件的 ASCII 模拟
- 显示不同实现的代码片段
- 图表变体
- 配置示例

预览内容以等宽框中的 markdown 渲染。支持多行文本（带换行符）。当任何选项有预览时，UI 切换到左右布局，左侧是垂直选项列表，右侧是预览。不要为标签和描述已足够的简单偏好问题使用预览。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The complete question to ask the user. Should be clear, specific, and end with a question mark.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Very short label displayed as a chip/tag (max 12 chars).",
                                "maxLength": 12,
                            },
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "The display text for this option (1-5 words).",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of what this option means or will happen if chosen.",
                                        },
                                        "preview": {
                                            "type": "string",
                                            "description": "Optional preview content rendered when this option is focused.",
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Set to true to allow the user to select multiple options.",
                            },
                        },
                        "required": ["question", "header", "options", "multiSelect"],
                    },
                },
            },
            "required": ["questions"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        questions = args.get("questions", [])

        if not questions:
            return ToolResult(data="Error: questions array is required", is_error=True)

        if not context.is_interactive:
            # In non-interactive mode, return a message
            q_summary = ", ".join(q.get("header", q.get("question", "")[:30])
                                 for q in questions)
            return ToolResult(
                data=f"[Asking user: {q_summary}] "
                     f"(Cannot prompt in non-interactive mode. Please select the first/default option.)",
                is_error=False,
            )

        # Build a formatted display of the questions
        formatted = ""
        for i, q in enumerate(questions):
            header = q.get("header", f"Q{i+1}")
            question = q.get("question", "")
            options = q.get("options", [])

            formatted += f"\n## {header}\n"
            formatted += f"{question}\n\n"
            for j, opt in enumerate(options):
                label = opt.get("label", f"Option {j+1}")
                desc = opt.get("description", "")
                formatted += f"  {j+1}. {label} — {desc}\n"

        return ToolResult(
            data=f"Questions for user:{formatted}\n\n"
                 f"[Use /answer <question_number> <option_number> to respond]",
            is_error=False,
        )
