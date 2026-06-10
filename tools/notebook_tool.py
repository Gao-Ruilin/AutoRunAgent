"""
NotebookEditTool — Edit Jupyter notebook cells.

Mirrors src/tools/NotebookEditTool/ — replaces, inserts, or deletes
cells in .ipynb files.
"""

import json
import os
from typing import Any, Dict, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class NotebookEditTool(Tool):
    """Edit Jupyter notebook (.ipynb) cells."""

    @property
    def name(self) -> str:
        return "NotebookEdit"

    @property
    def description(self) -> str:
        return """用新的源代码完全替换 Jupyter notebook（.ipynb 文件）中特定单元格的内容。

Jupyter notebooks 是结合了代码、文本和可视化的交互式文档，常用于数据分析和科学计算。notebook_path 参数必须是绝对路径，不能是相对路径。单元格编号从 0 开始。使用 edit_mode=insert 在指定索引处添加新单元格。使用 edit_mode=delete 删除指定索引处的单元格。"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "notebook_path": {
                    "type": "string",
                    "description": "The absolute path to the Jupyter notebook file to edit (must be absolute, not relative)",
                },
                "cell_id": {
                    "type": "string",
                    "description": "The ID of the cell to edit. When inserting, the new cell will be inserted after this cell.",
                },
                "new_source": {
                    "type": "string",
                    "description": "The new source for the cell",
                },
                "cell_type": {
                    "type": "string",
                    "enum": ["code", "markdown"],
                    "description": "The type of the cell (code or markdown). Required for insert mode.",
                },
                "edit_mode": {
                    "type": "string",
                    "enum": ["replace", "insert", "delete"],
                    "description": "The type of edit to make (replace, insert, delete). Defaults to replace.",
                },
            },
            "required": ["notebook_path", "new_source"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return args.get("edit_mode") != "delete"

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        notebook_path = args.get("notebook_path", "")
        new_source = args.get("new_source", "")
        cell_id = args.get("cell_id")
        cell_type = args.get("cell_type", "code")
        edit_mode = args.get("edit_mode", "replace")

        if not notebook_path:
            return ToolResult(data="Error: notebook_path is required", is_error=True)

        if not os.path.isabs(notebook_path):
            notebook_path = os.path.join(context.cwd or os.getcwd(), notebook_path)
        notebook_path = os.path.normpath(notebook_path)

        if not os.path.exists(notebook_path):
            return ToolResult(
                data=f"Error: Notebook not found: {notebook_path}",
                is_error=True,
            )

        try:
            with open(notebook_path, "r", encoding="utf-8") as f:
                notebook = json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            return ToolResult(
                data=f"Error reading notebook: {e}",
                is_error=True,
            )

        cells = notebook.get("cells", [])
        target_index = None

        if cell_id:
            for i, cell in enumerate(cells):
                if cell.get("id") == cell_id:
                    target_index = i
                    break
            if target_index is None and edit_mode == "replace":
                return ToolResult(
                    data=f"Error: Cell with id '{cell_id}' not found",
                    is_error=True,
                )

        if edit_mode == "replace" and target_index is not None:
            cells[target_index]["source"] = new_source
            action = f"Cell {target_index} replaced"

        elif edit_mode == "insert":
            new_cell = {
                "cell_type": cell_type,
                "metadata": {},
                "source": new_source,
            }
            if target_index is not None:
                cells.insert(target_index + 1, new_cell)
                action = f"Cell inserted after index {target_index}"
            else:
                cells.append(new_cell)
                action = "Cell appended at end"

        elif edit_mode == "delete":
            if target_index is not None:
                del cells[target_index]
                action = f"Cell {target_index} deleted"
            else:
                return ToolResult(
                    data="Error: cell_id required for delete mode",
                    is_error=True,
                )
        else:
            return ToolResult(
                data=f"Error: Unknown edit_mode: {edit_mode}",
                is_error=True,
            )

        notebook["cells"] = cells

        try:
            with open(notebook_path, "w", encoding="utf-8") as f:
                json.dump(notebook, f, indent=1, ensure_ascii=False)
        except Exception as e:
            return ToolResult(
                data=f"Error writing notebook: {e}",
                is_error=True,
            )

        return ToolResult(data=f"Notebook edited: {action}", is_error=False)
