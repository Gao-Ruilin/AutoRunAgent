"""
Shared XML tool call parser for DeepSeek/Anthropic XML formats.

Centralizes the regex patterns and parsing logic used by:
- query.py: _extract_xml_tool_use_blocks() → parse_and_strip_xml()
- ui/cli/app.py: _parse_xml_tool_calls() → parse_xml_tool_calls()
- ui/cli/app_textual.py: _parse_xml_tool_calls() → parse_xml_tool_calls()

DeepSeek outputs tool calls as XML embedded in text content:
  Format A: <tool_calls name="Bash"><parameter name="cmd" string="true">v</parameter></tool_calls>
  Format A2: <tool_call name="Bash">\n<parameter name="cmd" string="true">v</parameter>\n</tool_call>
  Format B: <tool_calls><Bash><cmd>v</cmd></Bash></tool_calls>
"""

import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ── Compiled regexes (compiled once, reused across all callers) ──────────

_TOOL_CALLS_RE = re.compile(r'<tool_calls?\b[^>]*>.*?</tool_calls?>', re.DOTALL)
_PARAM_A_RE = re.compile(
    r'<parameter\s+name="([^"]+)"\s+string="(true|false)"\s*>(.*?)</parameter>',
    re.DOTALL,
)
_TOOL_ELEM_RE = re.compile(r'<(\w+)>(.*?)</\1>', re.DOTALL)


def has_xml_tool_calls(text: str) -> bool:
    """Check if text contains XML tool_call/calls tags."""
    return '<tool_calls' in text or '<tool_call' in text


def parse_xml_tool_calls(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse XML <tool_call(s)> from text.

    Returns (cleaned_text, tool_blocks) where tool_blocks are dicts with
    keys: type, id, name, input. The cleaned_text has all XML tags removed.

    Used by CLI renderers (app.py, app_textual.py) to strip XML from
    streaming text and extract tool blocks for rendering.
    """
    tool_blocks: List[Dict[str, Any]] = []

    def _replace(m: re.Match) -> str:
        xml_str = m.group(0)
        # Try Format A/A2: name attribute + <parameter> children
        name_match = re.search(r'<tool_calls?\s+name="([^"]+)"', xml_str)
        if name_match:
            name = name_match.group(1)
            inp: Dict[str, Any] = {}
            for pm in _PARAM_A_RE.finditer(xml_str):
                pname = pm.group(1)
                pval = pm.group(3)
                if pm.group(2) == "true":
                    try:
                        pval = json.loads(pval)
                    except (json.JSONDecodeError, ValueError):
                        pass
                inp[pname] = pval
            tool_blocks.append({
                "type": "tool_use",
                "id": f"xml_{name}_{len(tool_blocks)}",
                "name": name,
                "input": inp,
            })
            return ""

        # Format B: <ToolName><param>value</param></ToolName> as children
        inner = re.search(r'<tool_calls?[^>]*>(.*?)</tool_calls?>', xml_str, re.DOTALL)
        if inner:
            for tm in _TOOL_ELEM_RE.finditer(inner.group(1)):
                name = tm.group(1)
                params_xml = tm.group(2).strip()
                inp = {}
                for pm in _TOOL_ELEM_RE.finditer(params_xml):
                    inp[pm.group(1)] = (pm.group(2) or "").strip()
                tool_blocks.append({
                    "type": "tool_use",
                    "id": f"xml_{name}_{len(tool_blocks)}",
                    "name": name,
                    "input": inp,
                })
            return ""

        return ""

    cleaned = _TOOL_CALLS_RE.sub(_replace, text)
    # Remove stray XML fragments
    cleaned = re.sub(r'</?tool_calls?\b[^>]*>', '', cleaned)
    return cleaned.strip(), tool_blocks


def parse_and_strip_xml(assistant_content: List[Any]) -> Tuple[List[Any], List[str]]:
    """Scan text blocks in assistant_content for XML tool_calls and parse them.

    Mutates text blocks in-place: strips XML tool_call tags from their text.
    Returns (tool_use_blocks, warnings) where tool_use_blocks are ToolUseBlock
    objects and warnings describe any parse failures.

    Used by query.py to post-process complete assistant messages.
    """
    from AutoRUN_v1.messages.types import ToolUseBlock

    # Gather text from all text blocks
    full_text = ""
    for block in assistant_content:
        if hasattr(block, 'text'):
            full_text += block.text
        elif isinstance(block, dict) and block.get('type') == 'text':
            full_text += block.get('text', '')

    if not has_xml_tool_calls(full_text):
        return [], []

    blocks: List[Any] = []
    warnings: List[str] = []
    _parse_failures = 0

    def _parse_match(m: re.Match) -> str:
        nonlocal _parse_failures
        xml_str = m.group(0)
        tag_snippet = xml_str[:200]

        name_match = re.search(r'<tool_calls?\s+name="([^"]+)"', xml_str)
        if name_match:
            name = name_match.group(1)
            inp: Dict[str, Any] = {}
            for pm in _PARAM_A_RE.finditer(xml_str):
                pname = pm.group(1)
                pval = pm.group(3)
                if pm.group(2) == "true":
                    try:
                        pval = json.loads(pval)
                    except Exception:
                        logger.debug(
                            "Failed to parse JSON parameter value for '%s'", pname,
                            exc_info=True,
                        )
                inp[pname] = pval
            if not inp:
                _parse_failures += 1
                warnings.append(
                    f"XML tool_call <{name}> parsed but no parameters found: {tag_snippet}"
                )
            blocks.append(ToolUseBlock(
                id=f"xml_{name}_{len(blocks)}",
                name=name,
                input=inp,
            ))
            return ""

        # Format B
        inner = re.search(r'<tool_calls?[^>]*>(.*?)</tool_calls?>', xml_str, re.DOTALL)
        if inner:
            inner_content = inner.group(1).strip()
            parsed_any = False
            for tm in _TOOL_ELEM_RE.finditer(inner_content):
                parsed_any = True
                name = tm.group(1)
                params_xml = tm.group(2).strip()
                inp = {}
                for pm in _TOOL_ELEM_RE.finditer(params_xml):
                    inp[pm.group(1)] = (pm.group(2) or "").strip()
                blocks.append(ToolUseBlock(
                    id=f"xml_{name}_{len(blocks)}",
                    name=name,
                    input=inp,
                ))
            if not parsed_any:
                _parse_failures += 1
                warnings.append(
                    f"XML tool_calls tag found but no inner tool elements matched: {tag_snippet}"
                )
            return ""

        _parse_failures += 1
        warnings.append(
            f"XML tool_calls tag has unrecognized format: {tag_snippet}"
        )
        return ""

    # Clean text blocks of XML tool_calls in-place
    for block in assistant_content:
        if hasattr(block, 'text'):
            block.text = _TOOL_CALLS_RE.sub(_parse_match, block.text)
        elif isinstance(block, dict) and 'text' in block:
            block['text'] = _TOOL_CALLS_RE.sub(_parse_match, block['text'])

    return blocks, warnings
