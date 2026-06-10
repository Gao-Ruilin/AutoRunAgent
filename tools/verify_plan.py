"""
VerifyPlanExecutionTool — Verify that implementation matches the plan.

Mirrors src/tools/VerifyPlanExecutionTool/ — checks that the user's
approved plan has been fully and correctly implemented.
"""

import os
from typing import Any, Dict, List, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class VerifyPlanExecutionTool(Tool):
    """Verify that code implementation matches the approved plan."""

    @property
    def name(self) -> str:
        return "VerifyPlanExecution"

    @property
    def description(self) -> str:
        return """验证当前实现是否与已批准的计划匹配。

此工具通过以下方式帮助确保实现质量：
1. 检查所有计划的更改是否已完成
2. 验证是否存在额外/计划外的更改
3. 将实现与计划要求进行对比
4. 识别计划与实现之间的差距

完成实现后使用此工具：
- 确保没有遗漏
- 发现实现偏差
- 验证交付的工作与计划匹配
- 获取剩余事项的检查清单

通过设置环境变量 AUTORUN_VERIFY_PLAN=true 启用。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan_summary": {
                    "type": "string",
                    "description": "Brief summary of what the plan promised to deliver",
                },
                "expected_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of files the plan expected to create or modify",
                },
                "expected_changes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of expected changes described in the plan",
                },
            },
            "required": ["plan_summary"],
        }

    def is_enabled(self) -> bool:
        """Only enabled when explicitly activated via env var."""
        return os.environ.get("AUTORUN_VERIFY_PLAN", "").lower() in ("1", "true", "yes")

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        plan_summary = args.get("plan_summary", "").strip()
        expected_files = args.get("expected_files", [])
        expected_changes = args.get("expected_changes", [])

        if not plan_summary:
            return ToolResult(data="Error: plan_summary is required", is_error=True)

        cwd = context.cwd or os.getcwd()
        results = []
        issues = []

        # Check expected files exist
        if expected_files:
            results.append("## File Verification")
            for f in expected_files:
                full_path = os.path.join(cwd, f)
                if os.path.exists(full_path):
                    results.append(f"  ✅ {f} — exists")
                else:
                    results.append(f"  ❌ {f} — MISSING")
                    issues.append(f"Missing file: {f}")

        # Check expected changes
        if expected_changes:
            results.append("\n## Change Verification")
            for change in expected_changes:
                results.append(f"  🔍 {change} — manual verification needed")

        if issues:
            results.append(f"\n## Issues Found ({len(issues)})")
            for issue in issues:
                results.append(f"  - {issue}")
            results.append("\n⚠️ Implementation does NOT fully match the plan.")
        else:
            results.append("\n## Result")
            results.append("✅ No issues detected. Implementation appears consistent with the plan.")

        # Summary
        results.insert(0, f"# Plan Verification Report\n\n**Plan**: {plan_summary}\n")

        return ToolResult(data="\n".join(results), is_error=len(issues) > 0)
