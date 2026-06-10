"""
查询引擎 — 高层对话编排器。

对应 src/QueryEngine.ts — 在核心查询循环之上提供:
- 会话级状态管理
- 工具权限处理
- 自动/手动上下文压缩
- 附件注入（memory、skills、CLAUDE.md）
- 文件历史跟踪
- 轮次记录和归因
"""

import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set

from AutoRUN_v1.api.client import get_client
from AutoRUN_v1.context import build_context_text, build_env_info, get_system_context, get_user_context
from AutoRUN_v1.messages.types import (
    AssistantMessage,
    AttachmentMessage,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from AutoRUN_v1.messages.utils import (
    _has_tool_use_blocks,
    get_messages_after_compact_boundary,
    normalize_messages_for_api,
    prepend_user_context,
)
from AutoRUN_v1.prompts.system_prompt import get_system_prompt
from AutoRUN_v1.query import run_query
from AutoRUN_v1.state.app_state import AppState
from AutoRUN_v1.utils.config import get_model

logger = logging.getLogger(__name__)

# Compaction thresholds are now dynamic (AutoRUN_v1.utils.tokens).
# See: get_auto_compact_char_threshold(), get_compact_warning_message_count().


class QueryEngine:
    """AutoRUN 对话循环的高层编排器。

    在核心查询循环之上提供会话管理、工具权限、
    压缩和附件注入功能。
    """

    def __init__(self, state: AppState):
        if state is None:
            raise ValueError("QueryEngine requires an AppState instance")
        self.state = state
        self._model = get_model()
        self._system_prompt: Optional[str] = None
        self._user_context: Dict[str, str] = {}
        self._system_context: Dict[str, str] = {}
        self._attachments: List[AttachmentMessage] = []
        self._turn_count = 0
        self._total_char_count = 0
        self._last_index_version: int = 0  # 用于检测索引变更
        self._last_agent_pref: Optional[bool] = None  # 用于检测多Agent开关变更

    def _init_lsp(self) -> None:
        """Initialize LSP manager.

        LSP manager is always initialized (like CC) for passive diagnostics.
        The ENABLE_LSP_TOOL env var controls whether LSPTool is in the tool list
        via LSPTool.is_enabled() → is_lsp_connected().
        """
        try:
            from AutoRUN_v1.services.lsp import initialize_lsp_server_manager
            initialize_lsp_server_manager()
            logger.debug("LSP manager initialized")
        except Exception:
            logger.debug("LSP initialization skipped", exc_info=True)

    async def initialize(self, autorun_md_content: Optional[str] = None) -> None:
        """初始化引擎 — 加载上下文，构建系统提示词。"""
        self._model = self.state.model or get_model()
        self._user_context = await get_user_context(autorun_md_content)
        self._system_context = await get_system_context()

        # Load enabled tools
        enabled_tools = self.state.enabled_tools
        from AutoRUN_v1.tools import get_tools
        all_tool_defs = get_tools()
        if enabled_tools:
            self.state.tool_definitions = [
                t for t in all_tool_defs if t["name"] in enabled_tools
            ]
        else:
            # No filter set — enable all tools
            self.state.tool_definitions = list(all_tool_defs)

        # Initialize LSP manager (if enabled)
        self._init_lsp()

        # Initialize file indexer FIRST so index context is available for system prompt
        await self._init_indexer()

        # Discover attachments
        await self._discover_attachments()

        # Build system prompt (index context is now available via state.indexer)
        current_agent_pref = getattr(self.state, "agent_pref", True)
        prompt_parts = await get_system_prompt(enabled_tools, self._model, state=self.state,
                                               delegation_mode=current_agent_pref)
        env_info = build_env_info(self._model)
        if isinstance(prompt_parts, list):
            prompt_parts.append(env_info)
            self._system_prompt = "\n".join(prompt_parts)
        else:
            self._system_prompt = prompt_parts + "\n" + env_info

        # Track the index version used to build this prompt
        indexer = getattr(self.state, "indexer", None)
        if indexer is not None:
            self._last_index_version = indexer.version
        self._last_agent_pref = current_agent_pref

    async def _discover_attachments(self) -> None:
        """发现并加载附件（skills、CLAUDE.md）。"""
        self._attachments = []

        # Skills directory
        try:
            from AutoRUN_v1.skills.loader import discover_skills, register_skills_to_tool
            disabled = self.state._get_disabled_skills() if self.state else set()
            skills = discover_skills(disabled_skills=disabled)
            register_skills_to_tool(disabled_skills=disabled)
            for skill_name, skill_def in skills.items():
                self._attachments.append(AttachmentMessage(
                    attachment_type="skill",
                    attachment_data={"name": skill_name, "definition": skill_def},
                ))
        except Exception:
            logger.debug("Skills discovery failed", exc_info=True)

    async def _init_indexer(self) -> None:
        """Initialize the background file indexer (non-blocking).

        Resolves project root by walking up from CWD to find nearest
        .autorun/index/, so subdirectory navigation still uses the
        parent project's index.
        """
        try:
            from AutoRUN_v1.services.indexer import FileIndexer, resolve_project_root
            cwd = os.getcwd()
            project_root = resolve_project_root(cwd)
            indexer = FileIndexer(project_root=project_root, state=self.state)
            if indexer.load_existing():
                indexer.start_polling()
                logger.debug("File indexer started with existing index (%d files)", indexer.file_count)
            else:
                logger.debug("No existing index found, awaiting user prompt")
            self.state.indexer = indexer
        except Exception:
            logger.debug("File indexer initialization skipped", exc_info=True)

    async def _maybe_refresh_system_prompt(self) -> None:
        """如果文件索引或 agent_pref 自上次构建提示词后已变更，刷新系统提示词。"""
        indexer = getattr(self.state, "indexer", None)
        current_agent_pref = getattr(self.state, "agent_pref", True)
        
        index_changed = (indexer is not None and indexer.version != self._last_index_version)
        agent_pref_changed = (self._last_agent_pref is not None 
                              and self._last_agent_pref != current_agent_pref)

        if not index_changed and not agent_pref_changed:
            return

        # 索引或委托模式变更：重建系统提示词
        enabled_tools = self.state.enabled_tools
        prompt_parts = await get_system_prompt(enabled_tools, self._model, state=self.state,
                                               delegation_mode=current_agent_pref)
        env_info = build_env_info(self._model)
        if isinstance(prompt_parts, list):
            prompt_parts.append(env_info)
            self._system_prompt = "\n".join(prompt_parts)
        else:
            self._system_prompt = prompt_parts + "\n" + env_info
        self._last_index_version = indexer.version if indexer is not None else 0
        self._last_agent_pref = current_agent_pref

    async def send_message(self, user_text: str,
                           can_use_tool: Optional[Callable[[str, Dict[str, Any]], bool]] = None
                           ) -> AsyncGenerator[Dict[str, Any], None]:
        """发送用户消息并流式返回响应。

        这是对话循环的主入口点。

        Args:
            user_text: 用户的输入文本。
            can_use_tool: 工具执行的权限回调。

        Yields:
            来自查询循环的流事件。
        """
        # 如果索引自上次提示词构建后已变更，刷新系统提示词
        await self._maybe_refresh_system_prompt()

        # Build skill lines for context injection (sent to API, NOT stored in message)
        skill_lines = []
        for att in self._attachments:
            if att.attachment_type == "skill":
                name = att.attachment_data.get("name", "")
                definition = att.attachment_data.get("definition", {})
                desc = definition.get("description", "") if isinstance(definition, dict) else ""
                if name:
                    skill_lines.append(f"- {name}: {desc}")

        # Build full context text (will be prepended to API messages, not stored)
        full_context = build_context_text(
            self._user_context, self._system_context,
            available_skills=skill_lines or None,
        )

        # Create user message — store ONLY the user's real input, not context
        from AutoRUN_v1.messages.types import create_user_message

        user_msg = create_user_message(user_text)
        self.state.add_message(user_msg)

        # Get active messages
        messages = self.state.get_messages()

        # Check if compaction needed
        if self._should_auto_compact(messages):
            compact_signal = {
                "type": "attachment",
                "attachment": {"type": "auto_compact_suggestion"},
            }
            yield compact_signal

        # Permission callback: combine engine-level and user-provided
        def _permission_check(tool_name: str, tool_args: Dict[str, Any]) -> bool:
            if not self.state.can_use_tool(tool_name, tool_args):
                return False
            # In bypass mode, can_use_tool already returned True — skip callback
            if self.state.permission_mode == "bypass":
                return True
            if can_use_tool:
                return can_use_tool(tool_name, tool_args)
            return True

        # Run the query loop
        tool_turn_count = 0
        _normal_exit = False
        _last_added_assistant_tool_use = False
        try:
            async for event in run_query(
                messages=messages,
                system_prompt=self._system_prompt,
                user_context=self._user_context,
                tools=self.state.tool_definitions,
                model=self._model,
                can_use_tool=_permission_check,
                state=self.state,
                full_context=full_context,
            ):
                event_type = event.get("type", "")

                # Track assistant/user messages
                if event_type == "assistant" and not event.get("is_partial"):
                    content = event.get("content", [])
                    assistant_msg = AssistantMessage(content=content)
                    self.state.add_message(assistant_msg)
                    self._turn_count += 1

                    # Track whether this assistant has tool_use blocks
                    _last_added_assistant_tool_use = _has_tool_use_blocks(assistant_msg)

                    # Estimate character count
                    msg_text = str(content)
                    self._total_char_count += len(msg_text)

                elif event_type == "user":
                    content = event.get("content", [])
                    user_result_msg = UserMessage(content=content)
                    self.state.add_message(user_result_msg)
                    tool_turn_count += 1
                    # User message with tool_results paired the previous assistant
                    _last_added_assistant_tool_use = False

                elif event_type == "terminal":
                    _normal_exit = True
                    # Reset turn tracking for next user message
                    pass

                yield event
        finally:
            # 如果非正常退出（中断/取消）且最后添加的是包含 tool_use
            # 的 assistant 消息（没有对应的 tool_result），则从 state 中
            # 移除该消息，避免下次请求时发送不完整的 tool_use 配对。
            if not _normal_exit and _last_added_assistant_tool_use:
                try:
                    msgs = self.state.get_messages()
                    if msgs:
                        last = msgs[-1]
                        if last.type == "assistant" and _has_tool_use_blocks(last):
                            with self.state._lock:
                                if (self.state.messages and
                                    self.state.messages[-1] is last):
                                    self.state.messages.pop()
                                    self._turn_count = max(0, self._turn_count - 1)
                except Exception:
                    logger.debug("Failed to clean up interrupted tool_use", exc_info=True)

    def _should_auto_compact(self, messages: List[Message]) -> bool:
        """检查是否应触发自动压缩（任务边界优先）。

        优先在任务边界处触发压缩，避免在任务进行中打断上下文。
        当检测到任务完成（report/redirect 阶段）且上下文使用率较高时触发。
        """
        from AutoRUN_v1.utils.tokens import (
            get_auto_compact_char_threshold,
            get_compact_warning_message_count,
        )
        from AutoRUN_v1.services.compact import detect_task_boundaries

        # 1. 任务边界优先：检测是否有明显的任务完成点
        boundaries = detect_task_boundaries(messages)
        if boundaries:
            # 存在任务边界，检查是否接近阈值
            near_threshold = (
                self._total_char_count > get_auto_compact_char_threshold() * 0.8
                or len(messages) > get_compact_warning_message_count() * 1.5
            )
            if near_threshold:
                return True

        # 2. 严格阈值检查（仅在超过 95% 时触发）
        if self._total_char_count > get_auto_compact_char_threshold():
            return True

        # 3. 消息数超额检查
        if len(messages) > get_compact_warning_message_count() * 2:
            return True

        return False

    async def compact(self) -> str:
        """手动压缩对话上下文。

        创建到目前为止的对话摘要，并插入压缩边界标记，
        使较早的消息从未来的 API 调用中排除。
        """
        from AutoRUN_v1.services.compact import compact_conversation

        messages = self.state.get_messages()
        summary = await compact_conversation(messages)
        return summary

    @property
    def turn_count(self) -> int:
        """助手总轮次数。"""
        return self._turn_count

    @property
    def message_count(self) -> int:
        """总消息数。"""
        return len(self.state.get_messages())


# ── Convenience Functions ──────────────────────────────────────────────────


async def quick_query(prompt: str, model: Optional[str] = None) -> str:
    """运行快速单次查询，无需 REPL 状态。

    适用于不需要对话历史的简单查询。
    """
    from AutoRUN_v1.api.client import get_client
    from AutoRUN_v1.context import build_env_info, get_user_context
    from AutoRUN_v1.messages.types import create_user_message
    from AutoRUN_v1.messages.utils import normalize_messages_for_api
    from AutoRUN_v1.prompts.system_prompt import get_system_prompt

    engine_model = model or get_model()
    user_context = await get_user_context()
    system_prompt = await get_system_prompt(set(), engine_model)
    env_info = build_env_info(engine_model)

    if isinstance(system_prompt, list):
        full_prompt = "\n".join(system_prompt or []) + "\n" + env_info
    else:
        full_prompt = (system_prompt or "") + "\n" + env_info

    context_text = build_context_text(user_context)

    user_msg = create_user_message(context_text + "\n" + prompt)
    api_messages = normalize_messages_for_api([user_msg])

    client = get_client()
    response_text = ""
    try:
        async for event in client.stream_message(
            messages=api_messages,
            system_prompt=full_prompt,
            model=engine_model,
        ):
            if event.get("type") == "text_delta":
                response_text += event.get("text", "")
            elif event.get("type") == "error":
                response_text += f"\n[Error: {event.get('error')}]"
    except Exception as e:
        response_text += f"\n[Error: {e}]"

    return response_text.strip()
