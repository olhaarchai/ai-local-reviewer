import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Overwrite

from src.agents.security_agent import build_security_agent
from src.agents.state import ReviewerState
from src.agents.style_agent import build_style_agent
from src.agents.summarizer_agent import build_summarizer_agent

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


_SECURITY_PERSONA = _load_prompt("security-persona.md")
_STYLE_PERSONA = _load_prompt("style-persona.md")
_SUMMARIZER_PERSONA = _load_prompt("summarizer-persona.md")
_OWASP_FOCUS = _load_prompt("owasp-focus.md")
_SECURITY_FORMAT = _load_prompt("security-format.md")
_STYLE_FORMAT = _load_prompt("style-format.md")

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
    if isinstance(parsed, dict) and "comments" in parsed:
        parsed = parsed.get("comments", [])
    elif isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"Unexpected JSON type: {type(parsed)}")
    logger.info("[%s] Parsed %d structured comments", node, len(parsed))
    return parsed


def _extract_agent_result(result: Any, node: str) -> tuple[list, AIMessage | None, str]:
    if not isinstance(result, dict):
        return ([], None, "")

    messages = result.get("messages") or []
    last_message = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_message = msg
            break
    if last_message is None:
        return ([], None, "")

    raw_content = getattr(last_message, "content", "")
    structured = result.get("structured_response")
    if structured is not None:
        if isinstance(structured, dict):
            comments = structured.get("comments", [])
            return (comments or [], last_message, raw_content)
        if hasattr(structured, "comments"):
            return (
                list(getattr(structured, "comments") or []),
                last_message,
                raw_content,
            )
    if raw_content:
        try:
            comments = _parse_json_response(raw_content, node)
            return (comments, last_message, raw_content)
        except Exception as exc:
            logger.error("[%s] JSON parse failed: %s", node, exc)
    return ([], last_message, raw_content)


def _extract_last_ai_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage):
            text = getattr(msg, "content", "")
            if isinstance(text, str) and text.strip():
                return text
    return ""


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


def _extract_comment_meta(comment: Any) -> tuple[str | None, str | None]:
    if hasattr(comment, "owasp_id") or hasattr(comment, "severity"):
        return (
            getattr(comment, "owasp_id", None),
            getattr(comment, "severity", None),
        )
    if isinstance(comment, dict):
        return (comment.get("owasp_id"), comment.get("severity"))
    return (None, None)


def _build_summary_from_comments(comments: list[Any]) -> str | None:
    findings_lines = []
    files: set[str] = set()
    for comment in comments:
        path, line, body = _extract_comment_fields(comment)
        if not (path and line and body):
            continue
        files.add(path)
        owasp_id, severity = _extract_comment_meta(comment)
        prefix = ""
        if severity:
            prefix += f"[{str(severity).upper()}] "
        if owasp_id:
            prefix += f"[{owasp_id}] "
        findings_lines.append(f"- {prefix}{path}:{line} — {body}")

    if not findings_lines:
        return None

    total = len(findings_lines)
    file_count = len(files)
    lines = [
        "Executive Summary",
        "",
        f"Found {total} issue(s) across {file_count} file(s).",
        "",
        "Key findings:",
        *findings_lines,
        "",
        "Recommendations:",
        "- Address the findings above and re-run the review.",
    ]
    return "\n".join(lines).strip()


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
    return {
        "diff": filtered,
        "comments": Overwrite(value=[]),
        "messages": Overwrite(value=[]),
        "raw_responses": Overwrite(value=[]),
        "timings": Overwrite(value=[]),
        "critic_issues": [],
        "critic_feedback": None,
        "iterations": 0,
        "is_valid": False,
        "summary_override": None,
    }


async def security_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_SECURITY")
    logger.info("[security_analyst] Starting OWASP audit with model=%s", model_name)
    started_at = time.perf_counter()

    system_content = _SECURITY_PERSONA + _OWASP_FOCUS + "\n" + _SECURITY_FORMAT
    guidelines = _state_get(state, "guidelines", [])
    if guidelines:
        rules_text = "\n".join(f"- {r}" for r in guidelines)
        system_content += (
            f"\n\nADDITIONAL PROJECT RULES (enforce these too):\n{rules_text}"
        )
        logger.info("[security_analyst] Injected %d project rules", len(guidelines))
    critic_feedback = _state_get(state, "critic_feedback", None)
    if critic_feedback:
        system_content += (
            "\n\nCRITIC FEEDBACK (fix these issues in your output):\n" + critic_feedback
        )

    diff = _state_get(state, "diff", "")
    agent = build_security_agent(system_content)
    result = await agent.ainvoke({"messages": [HumanMessage(content=f"DIFF:\n{diff}")]})

    comments, last_message, raw_response = _extract_agent_result(
        result, "security_analyst"
    )
    duration = time.perf_counter() - started_at
    logger.info("[security_analyst] Completed in %.2fs", duration)
    if last_message:
        logger.debug("[security_analyst] Raw response:\n%s", raw_response)
    payload: dict[str, Any] = {}
    if last_message:
        payload["messages"] = [last_message]
    if raw_response:
        payload["raw_responses"] = [raw_response]
    if comments:
        payload["comments"] = comments
    payload["timings"] = [{"security_analyst": duration}]
    return payload


async def style_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_STYLE")
    logger.info("[style_analyst] Starting with model=%s", model_name)
    started_at = time.perf_counter()

    system_content = _STYLE_PERSONA + _STYLE_FORMAT
    guidelines = _state_get(state, "guidelines", [])
    if guidelines:
        rules_text = "\n".join(f"- {r}" for r in guidelines)
        system_content += (
            f"\n\nADDITIONAL PROJECT RULES (enforce these too):\n{rules_text}"
        )
        logger.info("[style_analyst] Injected %d project rules", len(guidelines))
    critic_feedback = _state_get(state, "critic_feedback", None)
    if critic_feedback:
        system_content += (
            "\n\nCRITIC FEEDBACK (fix these issues in your output):\n" + critic_feedback
        )

    diff = _state_get(state, "diff", "")
    agent = build_style_agent(system_content)
    result = await agent.ainvoke({"messages": [HumanMessage(content=f"DIFF:\n{diff}")]})

    comments, last_message, raw_response = _extract_agent_result(
        result, "style_analyst"
    )
    duration = time.perf_counter() - started_at
    logger.info("[style_analyst] Completed in %.2fs", duration)
    if last_message:
        logger.debug("[style_analyst] Raw response:\n%s", raw_response)
    payload: dict[str, Any] = {}
    if last_message:
        payload["messages"] = [last_message]
    if raw_response:
        payload["raw_responses"] = [raw_response]
    if comments:
        payload["comments"] = comments
    payload["timings"] = [{"style_analyst": duration}]
    return payload


def critic_node(state: ReviewerState) -> dict:
    comments = _state_get(state, "comments", []) or []
    guidelines = _state_get(state, "guidelines", []) or []
    raw_responses = _state_get(state, "raw_responses", []) or []
    diff = _state_get(state, "diff", "")
    iterations = int(_state_get(state, "iterations", 0) or 0) + 1

    guideline_rules = _extract_rule_ids(guidelines)
    issues: list[dict[str, Any]] = []
    feedback_lines: list[str] = []

    if not comments:
        if raw_responses:
            issues.append(
                {
                    "path": "unknown",
                    "line": 0,
                    "rule_id": "FORMAT",
                    "message": "Analyst output is not valid JSON. Preserve content and fix formatting.",
                }
            )
            feedback_lines.append(
                "Analyst output is not valid JSON. Preserve content and fix formatting."
            )
        if _diff_has_obvious_issues(diff):
            issues.append(
                {
                    "path": "unknown",
                    "line": 0,
                    "rule_id": "MISSING",
                    "message": "No findings returned despite obvious risk keywords in the diff.",
                }
            )
            feedback_lines.append(
                "No findings returned despite obvious risk keywords in the diff."
            )

    for comment in comments:
        path, line, body = _extract_comment_fields(comment)
        if not path or line <= 0:
            issues.append(
                {
                    "path": path or "unknown",
                    "line": line,
                    "rule_id": "FORMAT",
                    "message": "Comment missing valid path/line.",
                }
            )
            feedback_lines.append(
                f"Comment missing valid path/line: {path or 'unknown'}:{line}."
            )
        if not body.strip():
            issues.append(
                {
                    "path": path or "unknown",
                    "line": line,
                    "rule_id": "FORMAT",
                    "message": "Comment body is empty.",
                }
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
                    {
                        "path": path or "unknown",
                        "line": line,
                        "rule_id": rule_id,
                        "message": "Rule ID not found in project guidelines.",
                    }
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


def retry_node(state: ReviewerState) -> dict:
    iterations = _state_get(state, "iterations", 0)
    logger.info("[critic] Retry requested (iteration %s)", iterations)
    return {
        "comments": Overwrite(value=[]),
        "messages": Overwrite(value=[]),
        "raw_responses": Overwrite(value=[]),
        "critic_issues": [],
    }


async def summary_node(state: ReviewerState):
    use_llm = os.getenv("SUMMARIZER_USE_LLM", "false").lower() in {"1", "true", "yes"}
    comments = _state_get(state, "comments", [])
    summary_override = _state_get(state, "summary_override", None)
    logger.info(
        "[summarizer] Starting (llm=%s), total structured comments=%d",
        use_llm,
        len(comments),
    )
    if summary_override:
        logger.info("[summarizer] Using human-provided summary override")
        return {"messages": [AIMessage(content=summary_override)]}

    if use_llm:
        agent = build_summarizer_agent(_SUMMARIZER_PERSONA)
        payload = json.dumps(comments, ensure_ascii=False)
        started_at = time.perf_counter()
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=f"COMMENTS:\n{payload}")]}
        )
        duration = time.perf_counter() - started_at
        summary_text = _extract_last_ai_text(result)
        if summary_text:
            logger.info("[summarizer] Using LLM summary")
            return {
                "messages": [AIMessage(content=summary_text)],
                "timings": [{"summarizer": duration}],
            }

    summary = _build_summary_from_comments(comments)
    if summary:
        logger.info("[summarizer] Using deterministic summary")
        return {"messages": [AIMessage(content=summary)]}

    logger.info("[summarizer] No findings; returning empty summary")
    return {"messages": [AIMessage(content="Executive Summary\n\nNo issues found.")]}
