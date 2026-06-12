"""
ConnectionTool — 管理连接和切换目录。

允许 Agent 自动添加 SSH 连接、本地文件夹、切换工作目录。
"""

import os
from typing import Any, Dict

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult


class ConnectionTool(Tool):
    """管理远程 SSH 连接、本地文件夹和切换工作目录。"""

    @property
    def name(self) -> str:
        return "Connection"

    @property
    def description(self) -> str:
        return """管理 SSH 远程连接、本地文件夹和切换工作目录。

支持三种操作:

1. **add_ssh** — 添加 SSH 远程连接
   - 参数: name (连接名称), host (主机地址), port (端口, 默认22), user (用户名),
     auth_type ("password" 或 "key"), password (密码), key_path (密钥路径)
   - 示例: {"action": "add_ssh", "name": "prod-server", "host": "10.0.0.1", "user": "root", "password": "xxx"}

2. **add_local** — 添加本地文件夹
   - 参数: name (连接名称), path (本地路径)
   - 示例: {"action": "add_local", "name": "my-project", "path": "D:\\projects\\app"}

3. **switch** — 切换当前工作目录到某个连接或路径
   - 参数: target (连接名称 或 路径)
   - 自动判断 target 是连接名还是路径
   - 示例: {"action": "switch", "target": "prod-server"}
   - 示例: {"action": "switch", "target": "D:\\projects\\app"}

4. **list** — 列出所有已保存的连接
   - 无需参数
   - 返回: 所有本地文件夹和 SSH 连接列表

5. **remove** — 删除一个连接
   - 参数: name (连接名称)
   - 示例: {"action": "remove", "name": "old-server"}

常用场景:
- 用户想让 AI 连接到远程服务器时，先调用 add_ssh 添加连接，再调用 switch 切换过去
- 用户想让 AI 换个项目目录工作时，调用 switch 切换目录
"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add_ssh", "add_local", "switch", "list", "remove"],
                    "description": "操作类型",
                },
                "name": {
                    "type": "string",
                    "description": "连接名称 (add_ssh/add_local/remove 需要)",
                },
                "host": {
                    "type": "string",
                    "description": "SSH 主机地址 (add_ssh 需要)",
                },
                "port": {
                    "type": "integer",
                    "description": "SSH 端口 (默认 22)",
                    "default": 22,
                },
                "user": {
                    "type": "string",
                    "description": "SSH 用户名 (add_ssh 需要)",
                },
                "auth_type": {
                    "type": "string",
                    "enum": ["password", "key"],
                    "description": "认证方式 (默认 password)",
                    "default": "password",
                },
                "password": {
                    "type": "string",
                    "description": "SSH 密码 (auth_type=password 时)",
                },
                "key_path": {
                    "type": "string",
                    "description": "SSH 密钥路径 (auth_type=key 时，如 ~/.ssh/id_rsa)",
                },
                "path": {
                    "type": "string",
                    "description": "本地路径 (add_local 需要)",
                },
                "target": {
                    "type": "string",
                    "description": "切换目标：连接名称或目录路径 (switch 需要)",
                },
            },
            "required": ["action"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return args.get("action") == "list"

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = args.get("action", "").strip()
        cwd = context.cwd or os.getcwd()

        if action == "list":
            return await self._list_connections()

        elif action == "add_ssh":
            return await self._add_ssh(args)

        elif action == "add_local":
            return await self._add_local(args)

        elif action == "switch":
            return await self._switch(args, cwd, context)

        elif action == "remove":
            return await self._remove(args)

        else:
            return ToolResult(
                data=f"未知操作: {action}。支持: add_ssh, add_local, switch, list, remove",
                is_error=True,
            )

    async def _list_connections(self) -> ToolResult:
        try:
            from AutoRUN_v1.utils.config import get_connections
            conns = get_connections()
            if not conns:
                return ToolResult(
                    data="当前没有任何已保存的连接。",
                    is_error=False,
                )
            lines = ["已保存的连接:"]
            for i, c in enumerate(conns):
                icon = "🖥️" if c.get("type") == "ssh" else "📂"
                if c.get("type") == "ssh":
                    detail = f"{c.get('user','')}@{c.get('host','')}:{c.get('port',22)}"
                    lines.append(f"  {i+1}. {icon} {c['name']} → {detail}")
                else:
                    lines.append(f"  {i+1}. {icon} {c['name']} → {c.get('path','')}")
            return ToolResult(data="\n".join(lines), is_error=False)
        except Exception as e:
            return ToolResult(data=f"获取连接列表失败: {e}", is_error=True)

    async def _add_ssh(self, args: Dict[str, Any]) -> ToolResult:
        name = args.get("name", "").strip()
        host = args.get("host", "").strip()
        if not name or not host:
            return ToolResult(data="需要提供 name 和 host", is_error=True)

        conn = {
            "name": name,
            "type": "ssh",
            "host": host,
            "port": args.get("port", 22),
            "user": args.get("user", "").strip(),
            "auth_type": args.get("auth_type", "password"),
        }
        if conn["auth_type"] == "password":
            conn["password"] = args.get("password", "")
        else:
            conn["key_path"] = args.get("key_path", "")

        try:
            from AutoRUN_v1.utils.config import save_connection, save_ssh_config
            save_connection(conn)
            # 同步保存为 SSH 配置（供 SSH 工具使用）
            save_ssh_config(
                name=name, host=host, port=conn["port"],
                user=conn["user"], auth_type=conn["auth_type"],
                password=args.get("password", ""),
                key_path=args.get("key_path", ""),
            )
            return ToolResult(
                data=f"✅ SSH 连接 '{name}' 已添加: {conn['user']}@{host}:{conn['port']}\n"
                     f"可在对话中使用 SSHBashTool、SSHReadTool 等操作远程文件。",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(data=f"添加 SSH 连接失败: {e}", is_error=True)

    async def _add_local(self, args: Dict[str, Any]) -> ToolResult:
        name = args.get("name", "").strip()
        path = args.get("path", "").strip()
        if not name or not path:
            return ToolResult(data="需要提供 name 和 path", is_error=True)

        # 规范化路径
        path = os.path.abspath(os.path.expanduser(path))

        conn = {"name": name, "type": "local", "path": path}
        try:
            from AutoRUN_v1.utils.config import save_connection
            save_connection(conn)
            return ToolResult(
                data=f"✅ 本地文件夹 '{name}' 已添加: {path}",
                is_error=False,
            )
        except Exception as e:
            return ToolResult(data=f"添加本地文件夹失败: {e}", is_error=True)

    async def _switch(self, args: Dict[str, Any], cwd: str, context: ToolContext) -> ToolResult:
        target = args.get("target", "").strip()
        if not target:
            return ToolResult(data="需要提供 target（连接名称或路径）", is_error=True)

        # 1. 先尝试作为连接名查找
        try:
            from AutoRUN_v1.utils.config import get_connections
            conns = get_connections()
            for c in conns:
                if c.get("name") == target:
                    if c.get("type") == "local":
                        new_path = c["path"]
                    else:
                        # SSH 连接 — 无法切换本地 cwd，告知用户通过 SSH 工具操作
                        return ToolResult(
                            data=f"'{target}' 是 SSH 远程连接 ({c.get('user','')}@{c.get('host','')})。\n"
                                 f"无法切换本地工作目录到远程。请使用 SSHBashTool、SSHReadTool 等工具操作远程。",
                            is_error=False,
                        )
                    break
            else:
                # 不是连接名，当作路径
                new_path = os.path.abspath(os.path.expanduser(target))
        except Exception:
            new_path = os.path.abspath(os.path.expanduser(target))

        # 2. 验证路径存在
        if not os.path.isdir(new_path):
            return ToolResult(
                data=f"目标路径不存在: {new_path}\n请确认路径正确，或先用 add_local 添加。",
                is_error=True,
            )

        # 3. 切换目录
        try:
            os.chdir(new_path)
            return ToolResult(
                data=f"✅ 已切换工作目录到: {new_path}",
                is_error=False,
            )
        except PermissionError:
            return ToolResult(data=f"没有权限访问目录: {new_path}", is_error=True)
        except Exception as e:
            return ToolResult(data=f"切换目录失败: {e}", is_error=True)

    async def _remove(self, args: Dict[str, Any]) -> ToolResult:
        name = args.get("name", "").strip()
        if not name:
            return ToolResult(data="需要提供 name", is_error=True)

        try:
            from AutoRUN_v1.utils.config import delete_connection
            delete_connection(name)
            return ToolResult(data=f"✅ 连接 '{name}' 已删除", is_error=False)
        except Exception as e:
            return ToolResult(data=f"删除连接失败: {e}", is_error=True)
