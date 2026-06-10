"""
Token 计数工具。

对应 Anthropic 的 token 计数 — 提供估算函数用于管理上下文窗口预算。
"""

from typing import Any, Dict, List, Optional

# Approximate character-to-token ratios for different languages
CHARS_PER_TOKEN_ENGLISH = 4.0  # ~4 chars per token for English text
CHARS_PER_TOKEN_CHINESE = 1.5  # ~1.5 chars per token for Chinese text
CHARS_PER_TOKEN_CODE = 3.0     # ~3 chars per token for code


def estimate_tokens(text: str) -> int:
    """从文本估算 token 数。

    使用英文字符和中文字符比率的启发式混合算法。
    生产环境建议集成 tiktoken 或提供商的 tokenizer。
    """
    if not text:
        return 0

    total_chars = len(text)
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english_chars = total_chars - chinese_chars

    tokens = (english_chars / CHARS_PER_TOKEN_ENGLISH +
              chinese_chars / CHARS_PER_TOKEN_CHINESE)
    return max(1, int(tokens))


def estimate_message_tokens(messages: List[Any]) -> int:
    """估算消息列表的总 token 数。"""
    total = 0
    for msg in messages:
        if hasattr(msg, 'content'):
            content = msg.content
        elif isinstance(msg, dict):
            content = msg.get("content", "")
        else:
            content = ""

        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(str(block))
                elif hasattr(block, 'text'):
                    total += estimate_tokens(block.text)
                elif hasattr(block, 'to_dict'):
                    total += estimate_tokens(str(block.to_dict()))
    return total


def estimate_system_prompt_tokens(system_prompt: str) -> int:
    """估算系统提示词的 token 数。"""
    return estimate_tokens(system_prompt)


# Reserved budget for system prompt and overhead
TOKEN_OVERHEAD = 8000  # ~2k tokens for system + safety margins

# Compaction thresholds as ratios of the context window
# Note: primary compaction is now task-boundary-driven (see services/compact.py).
# These token ratios are fallbacks only.
COMPACT_RATIO = 1.0              # 上下文使用率达到 100% 触发压缩（兜底，任务边界优先）
MICRO_COMPACT_RATIO = 0.95       # 微压缩阈值
AUTO_COMPACT_WARNING_RATIO = 0.95  # 自动压缩警告阈值（字符数基于此比例换算）


def get_context_window() -> int:
    """获取当前配置的上下文窗口大小。"""
    from AutoRUN_v1.utils.config import get_context_window as _get
    return _get()


def get_compact_token_threshold() -> int:
    """获取上下文压缩的 token 阈值（上下文窗口 * COMPACT_RATIO）。"""
    return int(get_context_window() * COMPACT_RATIO)


def get_micro_compact_token_threshold() -> int:
    """获取微压缩的 token 阈值。"""
    return int(get_context_window() * MICRO_COMPACT_RATIO)


def get_auto_compact_char_threshold() -> int:
    """获取自动压缩的字符数阈值（上下文窗口 * 95% * 4 chars/token）。"""
    return int(get_context_window() * AUTO_COMPACT_WARNING_RATIO * 4)


def get_compact_warning_message_count() -> int:
    """获取触发压缩警告的消息数阈值，至少 30 条。"""
    return max(30, int(get_context_window() / 10000))


def calculate_remaining_budget(model: str,
                               messages: List[Any],
                               system_prompt: str = "") -> int:
    """计算对话的剩余 token 预算。"""
    context_window = get_context_window()

    system_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    message_tokens = estimate_message_tokens(messages)
    used = system_tokens + message_tokens + TOKEN_OVERHEAD
    remaining = context_window - used
    return max(0, remaining)


def get_max_output_tokens(messages: List[Any],
                           system_prompt: str = "") -> int:
    """计算合适的 max_output_tokens 值，充分利用上下文窗口。

    为输出预留至少 context_window 的 10%，最多 64000 tokens。
    """
    context_window = get_context_window()
    system_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    message_tokens = estimate_message_tokens(messages)
    used = system_tokens + message_tokens + TOKEN_OVERHEAD
    available = context_window - used
    # Allocate ~80% of remaining for output, clamped between 2000 and 200000
    output = max(2000, min(200000, int(available * 0.8)))
    return output


def is_near_context_limit(model: str, messages: List[Any],
                          threshold_ratio: float = 0.8) -> bool:
    """检查对话是否接近上下文限制。"""
    context_window = get_context_window()

    used = estimate_message_tokens(messages) + TOKEN_OVERHEAD
    return used > (context_window * threshold_ratio)
