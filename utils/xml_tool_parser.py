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

_TOOL_CALLS_RE = re.compile(
    r'(?:<tool_calls?\b[^>]*>.*?</tool_calls?>'
    r'|<\|DSML\|tool_calls[^>]*>.*?</\|DSML\|tool_calls>'
    r'|<\|DSML\|invoke\s+name="[^"]+"[^>]*>.*?(?:</\|DSML\|invoke>|(?=<\|DSML\|invoke\s)|(?=<\|DSML\|/tool_calls)|$))',
    re.DOTALL,
)
_PARAM_A_RE = re.compile(
    r'<parameter\s+name="([^"]+)"\s+string="(true|false)"\s*>(.*?)</parameter>',
    re.DOTALL,
)
_TOOL_ELEM_RE = re.compile(r'<(\w+)>(.*?)</\1>', re.DOTALL)

# DSML (DeepSeek Markup Language) format regexes
# Example: <|DSML|tool_calls><|DSML|invoke name="Read">...</|DSML|invoke></|DSML|tool_calls>
_DSML_TOOL_CALLS_RE = re.compile(r'<\|DSML\|tool_calls[^>]*>.*?</\|DSML\|tool_calls>', re.DOTALL)
_DSML_INVOKE_RE = re.compile(
    r'<\|DSML\|invoke\s+name="([^"]+)"[^>]*>(.*?)</\|DSML\|invoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<\|DSML\|parameter\s+name="([^"]+)"\s+string="(true|false)"\s*>(.*?)</\|DSML\|parameter>',
    re.DOTALL,
)
# Cleanup stray DSML fragments
_DSML_STRAY_RE = re.compile(r'</?\|DSML\|[a-z_]+\s*[^>]*>')

# Loose DSML: handles fragments without proper closing tags (streaming artifacts)
# Matches <|DSML|invoke name="X"> followed by content until next invoke or end
_DSML_INVOKE_LOOSE_RE = re.compile(
    r'<\|DSML\|invoke\s+name="([^"]+)"[^>]*>(.*?)(?=<\|DSML\|invoke\s+name="|<\|DSML\|/tool_calls>|$)',
    re.DOTALL,
)
# Matches <|DSML|parameter name="X" string="B">VALUE — without requiring </|DSML|parameter>
_DSML_PARAM_LOOSE_RE = re.compile(
    r'<\|DSML\|parameter\s+name="([^"]+)"\s+string="(true|false)"\s*>(.*?)(?=</\|DSML\||<\|DSML\||$)',
    re.DOTALL,
)


def has_xml_tool_calls(text: str) -> bool:
    """Check if text contains XML tool_call/calls tags (including DSML format)."""
    return ('<tool_calls' in text or '<tool_call' in text
            or '<|DSML|tool_calls' in text or '<|DSML|invoke' in text)


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

        # DSML format: <|DSML|tool_calls><|DSML|invoke name="X">...</|DSML|invoke></|DSML|tool_calls>
        if '<|DSML|' in xml_str:
            # Try strict (closed tags) first
            invokes = list(_DSML_INVOKE_RE.finditer(xml_str))
            param_re = _DSML_PARAM_RE
            # Fallback to loose (unclosed streaming fragments) if strict produces nothing
            if not invokes:
                invokes = list(_DSML_INVOKE_LOOSE_RE.finditer(xml_str))
                param_re = _DSML_PARAM_LOOSE_RE
            for im in invokes:
                name = im.group(1)
                inner_xml = im.group(2)
                inp: Dict[str, Any] = {}
                for pm in param_re.finditer(inner_xml):
                    pname = pm.group(1)
                    pval = pm.group(3)
                    if pm.group(2) == "true":
                        try:
                            pval = json.loads(pval)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    inp[pname] = pval
                if not inp:
                    logger.warning(
                        "DSML invoke <%s> has no parameters, skipping: %s",
                        name, xml_str[:200],
                    )
                    continue
                tool_blocks.append({
                    "type": "tool_use",
                    "id": f"xml_{name}_{len(tool_blocks)}",
                    "name": name,
                    "input": inp,
                })
            return ""

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
            if not inp:
                logger.warning(
                    "Format A tool_calls <%s> has no parameters, skipping: %s",
                    name, xml_str[:200],
                )
                return ""
            tool_blocks.append({
                "type": "tool_use",
                "id": f"xml_{name}_{len(tool_blocks)}",
                "name": name,
                "input": inp,
            })
            return ""

        # Format A2: <tool_calls><tool_call name="X">...</tool_call></tool_calls>
        inner_tool_calls = re.findall(
            r'<tool_call\s+name="([^"]+)"[^>]*>(.*?)</tool_call>',
            xml_str, re.DOTALL
        )
        if inner_tool_calls:
            for t_name, t_inner in inner_tool_calls:
                inp: Dict[str, Any] = {}
                for pm in _PARAM_A_RE.finditer(t_inner):
                    pname = pm.group(1)
                    pval = pm.group(3)
                    if pm.group(2) == "true":
                        try:
                            pval = json.loads(pval)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    inp[pname] = pval
                if not inp:
                    logger.warning(
                        "Format A2 tool_call <%s> has no parameters, skipping: %s",
                        t_name, xml_str[:200],
                    )
                    continue
                tool_blocks.append({
                    "type": "tool_use",
                    "id": f"xml_{t_name}_{len(tool_blocks)}",
                    "name": t_name,
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
                if not inp:
                    logger.warning(
                        "Format B tool <%s> has no parameters, skipping: %s",
                        name, xml_str[:200],
                    )
                    continue
                tool_blocks.append({
                    "type": "tool_use",
                    "id": f"xml_{name}_{len(tool_blocks)}",
                    "name": name,
                    "input": inp,
                })
            return ""

        return ""

    cleaned = _TOOL_CALLS_RE.sub(_replace, text)
    # Remove stray XML fragments (both standard and DSML)
    cleaned = re.sub(r'</?tool_calls?\b[^>]*>', '', cleaned)
    cleaned = _DSML_STRAY_RE.sub('', cleaned)
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
            full_text += (block.text or "")
        elif isinstance(block, dict) and block.get('type') == 'text':
            full_text += (block.get('text') or "")

    if not has_xml_tool_calls(full_text):
        return [], []

    blocks: List[Any] = []
    warnings: List[str] = []
    _parse_failures = 0

    def _parse_match(m: re.Match) -> str:
        nonlocal _parse_failures
        xml_str = m.group(0)
        tag_snippet = xml_str[:200]

        # DSML format: <|DSML|tool_calls><|DSML|invoke name="X">...</|DSML|invoke></|DSML|tool_calls>
        if '<|DSML|' in xml_str:
            invokes = list(_DSML_INVOKE_RE.finditer(xml_str))
            param_re = _DSML_PARAM_RE
            if not invokes:
                invokes = list(_DSML_INVOKE_LOOSE_RE.finditer(xml_str))
                param_re = _DSML_PARAM_LOOSE_RE
            for im in invokes:
                name = im.group(1)
                inner_xml = im.group(2)
                inp: Dict[str, Any] = {}
                for pm in param_re.finditer(inner_xml):
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
                        f"DSML tool_call <{name}> parsed but no parameters found: {tag_snippet}"
                    )
                    continue
                blocks.append(ToolUseBlock(
                    id=f"xml_{name}_{len(blocks)}",
                    name=name,
                    input=inp,
                ))
            return ""

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
                return ""
            blocks.append(ToolUseBlock(
                id=f"xml_{name}_{len(blocks)}",
                name=name,
                input=inp,
            ))
            return ""

        # Format A2: <tool_calls><tool_call name="X">...</tool_call></tool_calls>
        inner_tool_calls = re.findall(
            r'<tool_call\s+name="([^"]+)"[^>]*>(.*?)</tool_call>',
            xml_str, re.DOTALL
        )
        if inner_tool_calls:
            for t_name, t_inner in inner_tool_calls:
                inp: Dict[str, Any] = {}
                for pm in _PARAM_A_RE.finditer(t_inner):
                    pname = pm.group(1)
                    pval = pm.group(3)
                    if pm.group(2) == "true":
                        try:
                            pval = json.loads(pval)
                        except Exception:
                            pass
                    inp[pname] = pval
                if not inp:
                    _parse_failures += 1
                    warnings.append(
                        f"XML inner tool_call <{t_name}> parsed but no parameters found: {tag_snippet}"
                    )
                    continue
                blocks.append(ToolUseBlock(
                    id=f"xml_{t_name}_{len(blocks)}",
                    name=t_name,
                    input=inp,
                ))
            return ""

        # Format B
        inner = re.search(r'<tool_calls?[^>]*>(.*?)</tool_calls?>', xml_str, re.DOTALL)
        if inner:
            inner_content = inner.group(1).strip()
            parsed_any = False
            for tm in _TOOL_ELEM_RE.finditer(inner_content):
                name = tm.group(1)
                params_xml = tm.group(2).strip()
                inp = {}
                for pm in _TOOL_ELEM_RE.finditer(params_xml):
                    inp[pm.group(1)] = (pm.group(2) or "").strip()
                if not inp:
                    _parse_failures += 1
                    warnings.append(
                        f"Format B tool <{name}> has no parameters: {tag_snippet}"
                    )
                    continue
                parsed_any = True
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
            block.text = _TOOL_CALLS_RE.sub(_parse_match, block.text or "")
            block.text = _DSML_STRAY_RE.sub('', block.text)
        elif isinstance(block, dict) and 'text' in block:
            block['text'] = _TOOL_CALLS_RE.sub(_parse_match, block['text'] or "")
            block['text'] = _DSML_STRAY_RE.sub('', block['text'])

    return blocks, warnings
