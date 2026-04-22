import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from src.agents.state import ReviewerState

load_dotenv()

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"


def _load_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[nodes] Prompt file not found: %s", path)
        return ""


llm_security = ChatOllama(
    model=os.getenv("OLLAMA_MODEL_SECURITY"), temperature=0, format="json"
)
llm_style = ChatOllama(
    model=os.getenv("OLLAMA_MODEL_STYLE"), temperature=0.2, format="json"
)
llm_fast = ChatOllama(model=os.getenv("OLLAMA_MODEL_FAST"), temperature=0.1)

_LINE_RULE = (
    'CRITICAL: Only comment on lines that start with "+" in the diff. '
    "Use the hunk header (@@ -L,l +L,l @@) to calculate the correct absolute line number. "
    "If you are unsure of the exact line number, skip the comment — do NOT guess."
)

_RULE_ID_INSTRUCTION = (
    "If a finding matches an ADDITIONAL PROJECT RULE, "
    "start the 'body' field with the Rule ID in brackets, "
    "e.g., '[TS001] Use unknown instead of any'."
)

_SECURITY_FORMAT = (
    "Output ONLY a raw JSON array. No markdown, no explanation, no code blocks.\n"
    'Format: [{"path": "file.ts", "line": 10, "owasp_id": "A03:2021", '
    '"severity": "High", "body": "description"}]\n'
    "Severity values: Critical, High, Medium, Low.\n"
    + _LINE_RULE
    + "\n"
    + _RULE_ID_INSTRUCTION
)

_STYLE_FORMAT = (
    "Output ONLY a raw JSON array. No markdown, no explanation, no code blocks.\n"
    'Format: [{"path": "file.ts", "line": 10, "body": "description"}]\n'
    + _LINE_RULE
    + "\n"
    + _RULE_ID_INSTRUCTION
)

_SECURITY_PERSONA = _load_prompt("security-persona.md")
_STYLE_PERSONA = _load_prompt("style-persona.md")
_SUMMARIZER_PERSONA = _load_prompt("summarizer-persona.md")
_OWASP_FOCUS = _load_prompt("owasp-focus.md")

_NOISE_FILE_PATTERNS = (
    "package-lock.json",
    "yarn.lock",
    "Pipfile.lock",
    "poetry.lock",
    ".snap",
    "dist/",
    ".next/",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".ttf",
    ".eot",
)


def filter_diff(raw_diff: str) -> str:
    chunks = re.split(r"(?=diff --git )", raw_diff)
    kept = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        header = chunk.split("\n")[0]
        if any(p in header for p in _NOISE_FILE_PATTERNS):
            logger.info("[filter_diff] Dropped: %s", header)
        else:
            kept.append(chunk)
    return "".join(kept)


def _parse_json_response(content: str, node: str) -> list:
    clean = content.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(clean)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"Unexpected JSON type: {type(parsed)}")
    logger.info("[%s] Parsed %d structured comments", node, len(parsed))
    return parsed


def _state_get(state: ReviewerState | dict, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def filter_node(state: ReviewerState) -> dict:
    diff = _state_get(state, "diff", "")
    filtered = filter_diff(diff)
    logger.info("[filter_node] Diff size: %d → %d chars", len(diff), len(filtered))
    return {"diff": filtered}


async def security_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_SECURITY")
    logger.info("[security_analyst] Starting OWASP audit with model=%s", model_name)

    system_content = _SECURITY_PERSONA + _OWASP_FOCUS + "\n" + _SECURITY_FORMAT
    guidelines = _state_get(state, "guidelines", [])
    if guidelines:
        rules_text = "\n".join(f"- {r}" for r in guidelines)
        system_content += (
            f"\n\nADDITIONAL PROJECT RULES (enforce these too):\n{rules_text}"
        )
        logger.info("[security_analyst] Injected %d project rules", len(guidelines))

    diff = _state_get(state, "diff", "")
    response = await llm_security.ainvoke(
        [
            SystemMessage(content=system_content),
            HumanMessage(content=f"DIFF:\n{diff}"),
        ]
    )

    logger.debug("[security_analyst] Raw response:\n%s", response.content)

    try:
        comments = _parse_json_response(response.content, "security_analyst")
        return {"messages": [response], "comments": comments}
    except Exception as e:
        logger.error("[security_analyst] JSON parse failed: %s", e)
        return {"messages": [response]}


async def style_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_STYLE")
    logger.info("[style_analyst] Starting with model=%s", model_name)

    system_content = _STYLE_PERSONA + _STYLE_FORMAT
    guidelines = _state_get(state, "guidelines", [])
    if guidelines:
        rules_text = "\n".join(f"- {r}" for r in guidelines)
        system_content += (
            f"\n\nADDITIONAL PROJECT RULES (enforce these too):\n{rules_text}"
        )
        logger.info("[style_analyst] Injected %d project rules", len(guidelines))

    diff = _state_get(state, "diff", "")
    response = await llm_style.ainvoke(
        [
            SystemMessage(content=system_content),
            HumanMessage(content=f"DIFF:\n{diff}"),
        ]
    )

    logger.debug("[style_analyst] Raw response:\n%s", response.content)

    try:
        comments = _parse_json_response(response.content, "style_analyst")
        return {"messages": [response], "comments": comments}
    except Exception as e:
        logger.error("[style_analyst] JSON parse failed: %s", e)
        return {"messages": [response]}


async def summary_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_FAST")
    comments = _state_get(state, "comments", [])
    logger.info(
        "[summarizer] Starting with model=%s, total structured comments=%d",
        model_name,
        len(comments),
    )

    analyst_outputs = [
        msg.content
        for msg in _state_get(state, "messages", [])
        if hasattr(msg, "content") and msg.content
    ]
    combined = "\n\n---\n\n".join(analyst_outputs)

    response = await llm_fast.ainvoke(
        [
            SystemMessage(content=_SUMMARIZER_PERSONA),
            HumanMessage(content=f"REVIEWS:\n{combined}"),
        ]
    )

    logger.info("[summarizer] Done. Response length=%d chars", len(response.content))
    logger.debug("[summarizer] Response:\n%s", response.content)
    return {"messages": [response]}
