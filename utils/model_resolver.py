"""
低模型解析器 — 将主模型映射到低成本变体，用于后台索引等轻量任务。

规则:
- deepseek-v4-pro → deepseek-v4-flash
- deepseek-v4 (不含pro) → deepseek-v4-flash
- gpt-5 系列 → 同模型 + reasoning_effort="low"
- 已经是低模型 → 直接返回
- 其他 → 同模型，无额外参数
"""

from typing import Any, Dict, Optional, Tuple


def resolve_low_model(model: str, api_type: str = "openai") -> Tuple[str, Optional[Dict[str, Any]]]:
    """返回 (low_model_name, extra_kwargs_or_None) 用于后台任务。

    Args:
        model: 主模型名称，如 "deepseek-v4-pro", "gpt-5.5"
        api_type: API 类型 ("openai" 或 "anthropic")

    Returns:
        (低模型名称, 额外 API 参数字典 或 None)
    """
    if not model:
        return (model, None)

    lower = model.lower()
    api_type_lower = api_type.lower() if api_type else ""

    # DeepSeek 系列: pro → flash
    if "deepseek" in lower or "deep-seek" in lower:
        if "flash" in lower or "v4-flash" in lower:
            # 已经是低模型
            return (model, None)
        if "reasoner" in lower or "r1" in lower:
            # DeepSeek-R1 系列 → deepseek-chat (V3)
            return ("deepseek-chat", None)
        # pro → flash, 其他 deepseek → flash
        return ("deepseek-v4-flash", None)

    # GPT-5 系列: 降低推理开销
    if "gpt-5" in lower:
        return (model, {"reasoning_effort": "low"})

    # Claude 系列: 用 haiku
    if "claude" in lower and api_type_lower == "anthropic":
        if "haiku" in lower:
            return (model, None)
        return ("claude-haiku-4-5", None)

    # 默认: 同模型无额外参数
    return (model, None)
