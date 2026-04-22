import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from src.agents.state import CriticIssue, ReviewerState

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


_RULE_ID_PATTERN = re.compile(r"^\[(?P<rule>[A-Z]{2,}\d{3,})\]")
_GUIDELINE_RULE_PATTERN = re.compile(r"\[(?P<rule>[A-Za-z]+[0-9]{3,})\]")
_OBVIOUS_PATTERNS = (
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bsecret\b", re.IGNORECASE),
    re.compile(r"\bapi_key\b", re.IGNORECASE),
    re.compile(r"\beval\s*\(", re.IGNORECASE),
    re.compile(r"\bexec\s*\(", re.IGNORECASE),
    re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
    re.compile(r"\bsubprocess\.", re.IGNORECASE),
    re.compile(r"\binnerHTML\b"),
    re.compile(r"dangerouslySetInnerHTML"),
)


def _extract_rule_ids(guidelines: list[str]) -> set[str]:
    rule_ids: set[str] = set()
    for guideline in guidelines:
        for match in _GUIDELINE_RULE_PATTERN.finditer(guideline or ""):
            rule_ids.add(match.group("rule"))
    return rule_ids


def _extract_comment_fields(comment: Any) -> tuple[str, int, str]:
    if hasattr(comment, "path"):
        line_val = getattr(comment, "line", 0)
        try:
            line = int(line_val or 0)
        except (TypeError, ValueError):
            line = 0
        return (
            str(getattr(comment, "path", "")),
            line,
            str(getattr(comment, "body", "")),
        )
    if isinstance(comment, dict):
        line_val = comment.get("line", 0)
        try:
            line = int(line_val or 0)
        except (TypeError, ValueError):
            line = 0
        return (
            str(comment.get("path", "")),
            line,
            str(comment.get("body", "")),
        )
    return ("", 0, str(comment) if comment is not None else "")


def _added_lines(diff: str) -> list[str]:
    lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return lines


def _diff_has_obvious_issues(diff: str) -> bool:
    for line in _added_lines(diff):
        for pattern in _OBVIOUS_PATTERNS:
            if pattern.search(line):
                return True
    return False


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
    raw_response = response.content

    try:
        comments = _parse_json_response(response.content, "security_analyst")
        return {
            "messages": [response],
            "comments": comments,
            "raw_responses": [raw_response],
        }
    except Exception as e:
        logger.error("[security_analyst] JSON parse failed: %s", e)
        return {"messages": [response], "raw_responses": [raw_response]}


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
    raw_response = response.content

    try:
        comments = _parse_json_response(response.content, "style_analyst")
        return {
            "messages": [response],
            "comments": comments,
            "raw_responses": [raw_response],
        }
    except Exception as e:
        logger.error("[style_analyst] JSON parse failed: %s", e)
        return {"messages": [response], "raw_responses": [raw_response]}


def critic_node(state: ReviewerState) -> dict:
    comments = _state_get(state, "comments", []) or []
    guidelines = _state_get(state, "guidelines", []) or []
    raw_responses = _state_get(state, "raw_responses", []) or []
    diff = _state_get(state, "diff", "")
    iterations = int(_state_get(state, "iterations", 0) or 0) + 1

    guideline_rules = _extract_rule_ids(guidelines)
    issues: list[CriticIssue] = []
    feedback_lines: list[str] = []

    if not comments:
        if raw_responses:
            issues.append(
                CriticIssue(
                    path="unknown",
                    line=0,
                    rule_id="FORMAT",
                    message="Analyst output is not valid JSON. Preserve content and fix formatting.",
                )
            )
            feedback_lines.append(
                "Analyst output is not valid JSON. Preserve content and fix formatting."
            )
        if _diff_has_obvious_issues(diff):
            issues.append(
                CriticIssue(
                    path="unknown",
                    line=0,
                    rule_id="MISSING",
                    message="No findings returned despite obvious risk keywords in the diff.",
                )
            )
            feedback_lines.append(
                "No findings returned despite obvious risk keywords in the diff."
            )

    for comment in comments:
        path, line, body = _extract_comment_fields(comment)
        if not path or line <= 0:
            issues.append(
                CriticIssue(
                    path=path or "unknown",
                    line=line,
                    rule_id="FORMAT",
                    message="Comment missing valid path/line.",
                )
            )
            feedback_lines.append(
                f"Comment missing valid path/line: {path or 'unknown'}:{line}."
            )
        if not body.strip():
            issues.append(
                CriticIssue(
                    path=path or "unknown",
                    line=line,
                    rule_id="FORMAT",
                    message="Comment body is empty.",
                )
            )
            feedback_lines.append(
                f"Comment body is empty for {path or 'unknown'}:{line}."
            )
            continue
        rule_match = _RULE_ID_PATTERN.match(body.strip())
        if rule_match:
            rule_id = rule_match.group("rule")
            if guideline_rules and rule_id not in guideline_rules:
                issues.append(
                    CriticIssue(
                        path=path or "unknown",
                        line=line,
                        rule_id=rule_id,
                        message="Rule ID not found in project guidelines.",
                    )
                )
                feedback_lines.append(
                    f"Rule ID [{rule_id}] is not present in project guidelines."
                )

    is_valid = len(issues) == 0
    critic_feedback = "\n".join(feedback_lines).strip() or None

    logger.info(
        "[critic] Validation %s with %d issue(s)",
        "passed" if is_valid else "failed",
        len(issues),
    )

    return {
        "is_valid": is_valid,
        "critic_feedback": critic_feedback,
        "critic_issues": issues,
        "iterations": iterations,
    }


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
