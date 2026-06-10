"""
上下文压缩服务。

对应 src/services/compact/compact.ts — 通过将较早的对话消息
总结为简洁的摘要来管理上下文窗口长度。
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from AutoRUN_v1.messages.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextBlock,
)
from AutoRUN_v1.utils.config import (
    get_api_key,
    get_api_type,
    get_api_url,
    get_model,
)

logger = logging.getLogger(__name__)

# Thresholds are now dynamic, based on the model's context window.
# See AutoRUN_v1.utils.tokens: get_compact_token_threshold() / get_micro_compact_token_threshold().

# Compact indicators in messages
COMPACT_INDICATOR = "[COMPACTED]"

# Phase label pattern for smart folding
_PHASE_PATTERN = re.compile(
    r'^\s*(?:<analyze>|<implement>|<report>|<redirect>)\s*',
    re.IGNORECASE
)

# Phase weight factors for smart folding:
#   analyze/implement → low weight (compress more aggressively)
#   report/redirect   → high weight (preserve more)
PHASE_RETENTION_WEIGHTS = {
    "analyze": 0.2,
    "implement": 0.3,
    "report": 0.85,
    "redirect": 0.9,
    "unknown": 0.5,
}

def classify_message_phase(msg: Message) -> str:
    """识别消息的对话阶段标签。

    检查消息（尤其是 assistant 消息）的文本开头，返回阶段类型。

    Returns:
        "analyze", "implement", "report", "redirect", 或 "unknown"
    """
    text = ""
    if hasattr(msg, "get_text"):
        text = msg.get_text()
    elif hasattr(msg, "content"):
        content = msg.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break
                elif hasattr(block, "text"):
                    text = block.text
                    break

    if not text:
        return "unknown"

    match = _PHASE_PATTERN.match(text)
    if match:
        tag = match.group().strip().lower()
        return tag.strip("<>")

    return "unknown"


def detect_task_boundaries(messages: List[Message]) -> List[int]:
    """检测对话中的任务边界索引。

    任务边界定义为：一个任务完成/方向变更的转换点。
    检测以下信号：
    - <report> 阶段消息（任务完成信号）
    - <redirect> 阶段消息（方向变更信号）
    - 用户消息前一条 assistant 是 <report>/<redirect>（新任务开始）
    - 已有的 compact 边界

    Returns:
        任务边界消息的索引列表（递减排序，方便从后往前处理）。
    """
    boundaries: List[int] = []
    n = len(messages)

    for i, msg in enumerate(messages):
        # 已有的压缩边界
        if getattr(msg, "is_compact_summary", False):
            boundaries.append(i)
            continue

        phase = classify_message_phase(msg)

        # <report> 和 <redirect> 是自然任务边界
        if phase in ("report", "redirect"):
            boundaries.append(i)
            continue

        # 用户消息前一条 assistant 是 report/redirect → 新任务开始
        if msg.type == "user" and i > 0:
            prev_msg = messages[i - 1]
            prev_phase = classify_message_phase(prev_msg)
            if prev_phase in ("report", "redirect"):
                boundaries.append(i)

    # 去重并降序排列（方便从后往前截取）
    boundaries = sorted(set(boundaries), reverse=True)
    return boundaries


def smart_compact_messages(
    messages: List[Message],
    boundaries: List[int],
    max_tokens: int,
) -> Tuple[List[Message], str]:
    """智能折叠消息列表。

    规则：
    - <analyze>/<implement> 块 → 折叠为简短摘要（保留低权重摘要）
    - <report>/<redirect> 块 → 保留原文或轻度压缩
    - 在任务边界之间折叠，每个任务区间生成一个摘要块

    Args:
        messages: 完整的消息列表。
        boundaries: 任务边界索引（由 detect_task_boundaries 返回）。
        max_tokens: 目标 token 上限。

    Returns:
        (压缩后的消息列表, 压缩摘要字符串)
    """
    if not boundaries or len(messages) <= 1:
        return messages, ""

    n = len(messages)
    # 找到需要压缩的截止点
    cumulative = 0
    cutoff_idx = n
    for i in range(n):
        cumulative += estimate_token_count([messages[i]])
        if cumulative > max_tokens:
            cutoff_idx = i
            break

    if cutoff_idx >= n:
        return messages, ""  # 不需要压缩

    # 在 cutoff_idx 之前找到最近的任务边界
    compact_boundary = 0
    for b in boundaries:
        if b <= cutoff_idx:
            compact_boundary = b
            break

    if compact_boundary == 0:
        # 没有找到合适的任务边界，使用 cutoff_idx
        compact_boundary = cutoff_idx

    messages_to_compact = messages[:compact_boundary]
    remaining = messages[compact_boundary:]

    # 按阶段分类需压缩的消息
    grouped: List[Tuple[str, List[Message]]] = []
    current_phase = None
    current_group: List[Message] = []

    for msg in messages_to_compact:
        phase = classify_message_phase(msg)
        # 对非 assistant 消息保留原始类型用于统计
        if current_phase is None:
            current_phase = phase
        if phase == current_phase or phase == "unknown":
            current_group.append(msg)
        else:
            if current_group:
                grouped.append((current_phase, current_group))
            current_phase = phase
            current_group = [msg]

    if current_group:
        grouped.append((current_phase or "unknown", current_group))

    # 生成每个组的摘要
    summary_parts = []
    for phase, group_msgs in grouped:
        weight = PHASE_RETENTION_WEIGHTS.get(phase, 0.5)
        token_est = estimate_token_count(group_msgs)

        if phase in ("report", "redirect"):
            # 高保留：对 report/redirect 做轻度压缩
            summary_parts.append(_summarize_high_retention(group_msgs, phase))
        else:
            # 低保留：analyze/implement 折叠为简短摘要
            summary_parts.append(_summarize_low_retention(group_msgs, phase, weight))

    summary_text = "\n\n".join(summary_parts)
    summary_boundary = (
        f"[智能折叠: 合计压缩 {len(messages_to_compact)} 条消息 "
        f"({len(boundaries)} 个任务边界)]\n\n{summary_text}"
    )

    # 创建压缩边界消息
    boundary_msg = SystemMessage(content=summary_boundary, is_compact_summary=True)
    compacted_messages = [boundary_msg] + list(remaining)

    return compacted_messages, summary_text


def _summarize_low_retention(
    messages: List[Message],
    phase: str,
    weight: float,
) -> str:
    """对低保留消息（analyze/implement）生成简短摘要。"""
    user_count = sum(1 for m in messages if m.type == "user")
    assistant_count = sum(1 for m in messages if m.type == "assistant")
    tool_names: Set[str] = set()

    for msg in messages:
        if msg.type == "assistant":
            for block in (getattr(msg, "get_tool_use_blocks", lambda: [])()):
                tool_names.add(block.name)

    lines = [f"### {phase} 阶段 ({user_count} 用户消息, {assistant_count} 助手回复)"]
    if tool_names:
        lines.append(f"使用工具: {', '.join(sorted(tool_names))}")

    # 提取用户输入的首行作为关键意图提示
    user_intents = []
    for msg in messages:
        if msg.type == "user" and not (hasattr(msg, "get_tool_result_blocks") and msg.get_tool_result_blocks()):
            text = msg.get_text() if hasattr(msg, "get_text") else ""
            if text:
                first_line = text.split("\n")[0][:120]
                user_intents.append(first_line)

    if user_intents:
        lines.append("用户意图: " + "; ".join(user_intents[:5]))

    return "\n".join(lines)


def _summarize_high_retention(
    messages: List[Message],
    phase: str,
) -> str:
    """对高保留消息（report/redirect）做轻度压缩摘要。"""
    user_count = sum(1 for m in messages if m.type == "user")
    assistant_count = sum(1 for m in messages if m.type == "assistant")

    lines = [f"### {phase} 阶段 ({user_count} 用户消息, {assistant_count} 助手回复)"]

    # 保留 assistant 消息的关键文本
    for msg in messages:
        if msg.type == "assistant":
            text = msg.get_text() if hasattr(msg, "get_text") else ""
            if text:
                # 截取每段的前面部分
                excerpt = text[:300].replace("\n", " ").strip()
                if excerpt:
                    lines.append(f"- {excerpt}...")
                    if len(lines) > 15:  # 限制条目数
                        lines.append("- ... (更多内容已截断)")
                        break

    return "\n".join(lines)


def find_compaction_range_task_aware(
    messages: List[Message],
    max_tokens: Optional[int] = None,
) -> Tuple[int, List[int]]:
    """任务感知的压缩范围查找。

    结合任务边界信息，找到最优的压缩截止点。

    Returns:
        (压缩截止索引, 任务边界列表)
    """
    if max_tokens is None:
        from AutoRUN_v1.utils.tokens import get_compact_token_threshold
        max_tokens = get_compact_token_threshold()

    boundaries = detect_task_boundaries(messages)
    cumulative_tokens = 0
    cutoff_idx = len(messages)

    for i, msg in enumerate(messages):
        if getattr(msg, "is_compact_summary", False):
            cumulative_tokens = 0
            continue
        cumulative_tokens += estimate_token_count([msg])
        if cumulative_tokens > max_tokens:
            cutoff_idx = i
            break

    # 在 cutoff_idx 之前找到最近的任务边界
    best_boundary = 0
    for b in boundaries:
        if b <= cutoff_idx:
            best_boundary = b
            break

    return best_boundary if best_boundary > 0 else cutoff_idx, boundaries


def estimate_token_count(messages: List[Message]) -> int:
    """粗略 token 估算（英语：字符数 / 4）。

    这是一个近似值。生产环境建议集成
    合适的 tokenizer，如 tiktoken。
    """
    total_chars = 0
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(str(block))
                elif hasattr(block, 'text'):
                    total_chars += len(block.text)
                elif hasattr(block, 'content'):
                    total_chars += len(str(block.content))
    return total_chars // 4


def find_compaction_range(messages: List[Message],
                          max_tokens: Optional[int] = None) -> int:
    """查找消息应压缩到的索引位置。

    返回索引（不含）— messages[:idx] 应被压缩。
    """
    if max_tokens is None:
        from AutoRUN_v1.utils.tokens import get_compact_token_threshold
        max_tokens = get_compact_token_threshold()
    cumulative_tokens = 0
    for i, msg in enumerate(messages):
        if hasattr(msg, 'is_compact_summary') and msg.is_compact_summary:
            # Already compacted, skip
            cumulative_tokens = 0
            continue
        cumulative_tokens += estimate_token_count([msg])
        if cumulative_tokens > max_tokens:
            return i
    return -1  # No compaction needed


async def compact_conversation(messages: List[Message],
                               model: Optional[str] = None) -> str:
    """通过总结较早消息来压缩对话。

    创建压缩摘要边界，使较早的消息从未来的 API 调用中排除。

    返回适合作为系统消息插入的摘要字符串。
    """
    idx = find_compaction_range(messages)
    if idx <= 0:
        return "不需要压缩：上下文大小在限制之内。"

    messages_to_compact = messages[:idx]
    summary_input = _extract_conversation_text(messages_to_compact)

    # Try calling the model for a proper summary
    try:
        summary = await _call_model_for_summary(summary_input, model=model)
        if summary:
            logger.info(f"Compaction: generated summary for {len(messages_to_compact)} messages via API")
            return (
                f"[对话压缩: 总结了 {len(messages_to_compact)} 条历史消息]\n\n"
                f"{summary}"
            )
    except Exception as e:
        logger.warning(f"Compaction API call failed, falling back to local summary: {e}")

    # Fallback to local summary if API call fails
    summary = _build_local_summary(messages_to_compact, summary_input)
    return summary


def manual_compact(state=None) -> str:
    """手动压缩触发器（由 /compact 命令调用）。"""
    from AutoRUN_v1.state.app_state import get_app_state

    if state is None:
        state = get_app_state()
    messages = state.get_messages()
    estimated = estimate_token_count(messages)
    idx = find_compaction_range(messages)

    if idx <= 0:
        return (
            f"当前上下文: ~{estimated} tokens, {len(messages)} 条消息。"
            f"暂不需要压缩。"
        )

    return (
        f"当前上下文: ~{estimated} tokens, {len(messages)} 条消息。\n"
        f"建议压缩前 {idx} 条消息以释放上下文空间。"
        f"使用 /compact --force 强制执行压缩。"
    )


async def force_compact(state=None) -> str:
    """强制压缩当前对话，将摘要边界插入消息列表。"""
    from AutoRUN_v1.state.app_state import get_app_state

    if state is None:
        state = get_app_state()
    messages = state.get_messages()
    idx = find_compaction_range(messages)

    if idx <= 0:
        estimated = estimate_token_count(messages)
        return (
            f"当前上下文: ~{estimated} tokens, {len(messages)} 条消息。"
            f"无需压缩。"
        )

    summary = await compact_conversation(messages)
    boundary = SystemMessage(content=summary, is_compact_summary=True)

    # Replace messages before the boundary with the compact summary
    state.set_messages([boundary] + messages[idx:])
    estimated_after = estimate_token_count(state.get_messages())

    return (
        f"压缩完成: {len(messages[:idx])} 条消息 → 1 条摘要。\n"
        f"剩余 ~{estimated_after} tokens, {len(state.get_messages())} 条消息。"
    )


def _extract_conversation_text(messages: List[Message]) -> str:
    """从对话消息中提取可读文本。"""
    lines = []
    for msg in messages:
        if msg.type == "user":
            text = msg.get_text()
            if text:
                lines.append(f"用户: {text[:500]}")
        elif msg.type == "assistant":
            text = msg.get_text()
            if text:
                lines.append(f"助手: {text[:500]}")
        elif msg.type == "system" and not msg.is_compact_summary:
            text = msg.get_text()
            if text:
                lines.append(f"[系统]: {text[:300]}")
    return "\n".join(lines)


# ── Compact system prompt ─────────────────────────────────────────────────────

COMPACT_SYSTEM_PROMPT = """\
你是一个对话压缩助手。你的任务是将较长的对话历史总结为一份简洁但信息完整的摘要。

要求：
1. 保留用户的核心目标、意图和所有重要决策。
2. 记录已完成的关键操作（工具调用、文件修改、代码生成等）及其结果。
3. 记录重要的用户反馈、偏好和约束。
4. 记录所有未完成的任务和待解决的问题。
5. 保留技术上下文：使用的技术栈、文件路径、关键代码模式。
6. 使用中文撰写摘要（技术术语保留英文）。
7. 摘要应该让后续对话能够在不丢失上下文的情况下继续。

请直接输出摘要文本，不要加任何前缀或后缀说明。"""


async def _call_model_for_summary(conversation_text: str,
                                   model: Optional[str] = None) -> str:
    """调用 LLM 生成对话摘要。

    Args:
        conversation_text: 需要总结的对话文本。
        model: 可选模型覆写（默认使用配置中的模型）。

    Returns:
        模型生成的摘要文本。

    Raises:
        ValueError: API 配置不完整。
        httpx.HTTPError: 网络请求失败。
    """
    api_key = get_api_key()
    api_url = get_api_url()
    api_type = get_api_type()
    effective_model = model or get_model()

    if not api_key or not api_url or not effective_model:
        raise ValueError("API 配置不完整，无法调用模型生成摘要")

    user_prompt = (
        f"请总结以下对话历史：\n\n"
        f"<conversation>\n{conversation_text}\n</conversation>"
    )

    if api_type == "openai":
        return await _summary_via_openai(api_key, api_url, effective_model, user_prompt)
    elif api_type == "anthropic":
        return await _summary_via_anthropic(api_key, api_url, effective_model, user_prompt)
    else:
        raise ValueError(f"不支持的 API 类型: {api_type}")


async def _summary_via_openai(api_key: str, base_url: str, model: str,
                               user_prompt: str) -> str:
    """通过 OpenAI 兼容 API 生成摘要。"""
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()


async def _summary_via_anthropic(api_key: str, base_url: str, model: str,
                                  user_prompt: str) -> str:
    """通过 Anthropic 兼容 API 生成摘要。"""
    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "system": COMPACT_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 2048,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"].strip()


def _build_local_summary(messages: List[Message], text: str) -> str:
    """构建消息的本地摘要（API 不可用时的回退方案）。

    提取对话中的关键信息作为结构化摘要。
    """
    user_count = sum(1 for m in messages if m.type == "user")
    assistant_count = sum(1 for m in messages if m.type == "assistant")
    tool_count = sum(
        1 for m in messages
        if m.type == "user" and m.get_tool_result_blocks()
    )

    summary_lines = [
        f"[对话压缩（本地摘要）: 总结了 {len(messages)} 条历史消息]",
        f"({user_count} 条用户消息, {assistant_count} 条助手回复, {tool_count} 条工具结果)",
        "",
        "关键对话要点:",
    ]

    # Collect all tool names used
    tool_names = set()
    for msg in messages:
        if msg.type == "assistant":
            for block in msg.get_tool_use_blocks():
                tool_names.add(block.name)

    if tool_names:
        summary_lines.append(f"  使用的工具: {', '.join(sorted(tool_names))}")

    # Extract user questions (non-tool-result)
    user_questions = []
    for msg in messages:
        if msg.type == "user" and not msg.get_tool_result_blocks():
            t = msg.get_text()
            if t and len(t) > 10:
                user_questions.append(t[:300])

    if user_questions:
        summary_lines.append("  用户提问:")
        for q in user_questions[:5]:
            summary_lines.append(f"    - {q}")

    # Extract tool call names and their condensed inputs
    tool_actions = []
    for msg in messages:
        if msg.type == "assistant":
            for block in msg.get_tool_use_blocks():
                inp_summary = json.dumps(block.input, ensure_ascii=False)
                if len(inp_summary) > 150:
                    inp_summary = inp_summary[:150] + "..."
                tool_actions.append(f"    - {block.name}: {inp_summary}")

    if tool_actions:
        summary_lines.append("  执行的操作:")
        summary_lines.extend(tool_actions[:10])

    return "\n".join(summary_lines)


def insert_compact_boundary(messages: List[Message], summary: str) -> List[Message]:
    """向对话中插入压缩边界消息。

    边界之前的消息将从未来的 API 调用中排除。
    """
    boundary = SystemMessage(
        content=summary,
        is_compact_summary=True,
    )
    # Find the last compact boundary and replace everything before it
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].is_compact_summary:
            return [boundary] + messages[i + 1:]

    # No existing boundary — insert at the beginning
    return [boundary] + messages
