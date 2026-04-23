import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Overwrite

from src.integrations.retriever import (
    _ALWAYS_INCLUDE,
    _classify_file,
    extract_pr_files,
)
from src.integrations.sparse_index import SPARSE_INDEX
from src.review.agents.security_agent import build_security_agent
from src.review.agents.style_agent import build_style_agent
from src.review.agents.summarizer_agent import build_summarizer_agent
from src.review.state import ReviewerState
from src.tools.static_analysis import run_ruff_on_file

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


def _extract_rule_ids(guidelines: list) -> set[str]:
    rule_ids: set[str] = set()
    for g in guidelines:
        if hasattr(g, "id"):
            if g.id != "UNKNOWN":
                rule_ids.add(g.id)
        else:
            for match in _GUIDELINE_RULE_PATTERN.finditer(str(g) or ""):
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


def _build_added_line_map(diff: str) -> dict[str, set[int]]:
    """Map `path` → set of absolute line numbers that appear on `+` lines."""
    added: dict[str, set[int]] = {}
    current_path: str | None = None
    current_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            added.setdefault(current_path, set())
        elif line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            current_line = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if current_path:
                added[current_path].add(current_line)
            current_line += 1
        elif not line.startswith("-"):
            current_line += 1
    return added


def _build_added_content_map(diff: str) -> dict[str, str]:
    """Map `path` → joined added-line content (for lint/grep reasoning)."""
    content: dict[str, list[str]] = {}
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            content.setdefault(current_path, [])
        elif line.startswith("+") and not line.startswith("+++"):
            if current_path:
                content[current_path].append(line[1:])
    return {p: "\n".join(lines) for p, lines in content.items()}


_BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


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
        "lint_findings": [],
    }


async def security_analyst_node(state: ReviewerState):
    from src.core.config import settings

    model_name = settings.ollama_model_security
    logger.info("[security_analyst] Starting OWASP audit with model=%s", model_name)
    started_at = time.perf_counter()

    system_content = _SECURITY_PERSONA + _OWASP_FOCUS + "\n" + _SECURITY_FORMAT
    stack_context = _state_get(state, "stack_context", "")
    if stack_context:
        system_content = f"PR CONTEXT:\n{stack_context}\n\n" + system_content
    guidelines = _state_get(state, "guidelines", [])
    if guidelines:
        rules_text = "\n".join(
            f"- {g.text if hasattr(g, 'text') else g}" for g in guidelines
        )
        system_content += (
            f"\n\nADDITIONAL PROJECT RULES (enforce these too):\n{rules_text}"
        )
        logger.info("[security_analyst] Injected %d project rules", len(guidelines))
    lint_findings = _state_get(state, "lint_findings", []) or []
    if lint_findings:
        lint_block = "\n".join(f"- {f}" for f in lint_findings)
        system_content += (
            "\n\nDETERMINISTIC PRE-FINDINGS (already ground-truth — you may cite these verbatim):\n"
            + lint_block
            + "\n"
        )
    critic_feedback = _state_get(state, "critic_feedback", None)
    if critic_feedback:
        system_content += (
            "\n\nCRITIC FEEDBACK (fix these issues in your output):\n" + critic_feedback
        )

    diff = _state_get(state, "diff", "")
    agent = build_security_agent(system_content)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=f"DIFF:\n{diff}")]},
        config={"recursion_limit": settings.agent_recursion_limit},
    )

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
    from src.core.config import settings

    model_name = settings.ollama_model_style
    logger.info("[style_analyst] Starting with model=%s", model_name)
    started_at = time.perf_counter()

    system_content = _STYLE_PERSONA + _STYLE_FORMAT
    stack_context = _state_get(state, "stack_context", "")
    if stack_context:
        system_content = f"PR CONTEXT:\n{stack_context}\n\n" + system_content
    guidelines = _state_get(state, "guidelines", [])
    if guidelines:
        rules_text = "\n".join(
            f"- {g.text if hasattr(g, 'text') else g}" for g in guidelines
        )
        system_content += (
            f"\n\nADDITIONAL PROJECT RULES (enforce these too):\n{rules_text}"
        )
        logger.info("[style_analyst] Injected %d project rules", len(guidelines))
    lint_findings = _state_get(state, "lint_findings", []) or []
    if lint_findings:
        lint_block = "\n".join(f"- {f}" for f in lint_findings)
        system_content += (
            "\n\nDETERMINISTIC PRE-FINDINGS (already ground-truth — you may cite these verbatim):\n"
            + lint_block
            + "\n"
        )
    critic_feedback = _state_get(state, "critic_feedback", None)
    if critic_feedback:
        system_content += (
            "\n\nCRITIC FEEDBACK (fix these issues in your output):\n" + critic_feedback
        )

    diff = _state_get(state, "diff", "")
    agent = build_style_agent(system_content)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=f"DIFF:\n{diff}")]},
        config={"recursion_limit": settings.agent_recursion_limit},
    )

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


def _reject(
    comment: Any,
    reason_code: str,
    detail: str,
    rejections: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    counts[reason_code] = counts.get(reason_code, 0) + 1
    rejections.append({"comment": comment, "reason": f"{reason_code}: {detail}"})


def critic_node(state: ReviewerState) -> dict:
    """Prune hallucinated comments via deterministic checks (G1-G4) + rule ID guard.

    Retry is only triggered when the comment list is empty AND the diff shows
    obvious risk patterns — per plan §5.2, the critic's job is to prune, not regen.
    """
    comments = _state_get(state, "comments", []) or []
    guidelines = _state_get(state, "guidelines", []) or []
    raw_responses = _state_get(state, "raw_responses", []) or []
    diff = _state_get(state, "diff", "")
    iterations = int(_state_get(state, "iterations", 0) or 0) + 1

    # Zero-comments fallback: preserve the existing retry-on-miss behaviour.
    if not comments:
        issues: list[dict[str, Any]] = []
        feedback_lines: list[str] = []
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
        is_valid = len(issues) == 0
        critic_feedback = "\n".join(feedback_lines).strip() or None
        logger.info(
            "[critic] Empty comments — %s with %d issue(s)",
            "passed" if is_valid else "requesting retry",
            len(issues),
        )
        return {
            "is_valid": is_valid,
            "critic_feedback": critic_feedback,
            "critic_issues": issues,
            "iterations": iterations,
        }

    added_lines = _build_added_line_map(diff)
    added_content = _build_added_content_map(diff)
    guideline_rules = _extract_rule_ids(guidelines)

    survivors: list[Any] = []
    rejections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for comment in comments:
        path, line, body = _extract_comment_fields(comment)
        body_stripped = body.strip()

        # Basic shape checks — skip empty / unparseable comments.
        if not path or line <= 0 or not body_stripped:
            _reject(
                comment,
                "FORMAT",
                f"missing path/line/body ({path}:{line})",
                rejections,
                counts,
            )
            continue

        # G2: path must appear in the diff.
        if path not in added_lines:
            _reject(
                comment,
                "G2",
                f"path '{path}' not in diff",
                rejections,
                counts,
            )
            continue

        # G1: line must be a + line for that path.
        if line not in added_lines[path]:
            _reject(
                comment,
                "G1",
                f"line {line} not in '+'-set for '{path}'",
                rejections,
                counts,
            )
            continue

        # G3: any backtick-quoted identifier must appear in the file's added text.
        identifiers = _BACKTICK_IDENT_RE.findall(body_stripped)
        if identifiers:
            file_added = added_content.get(path, "")
            if not any(ident in file_added for ident in identifiers):
                _reject(
                    comment,
                    "G3",
                    f"none of {identifiers} found in added lines of '{path}'",
                    rejections,
                    counts,
                )
                continue

        # G4 + guideline-ID check: if body starts with [RULEID], validate category.
        rule_match = _RULE_ID_PATTERN.match(body_stripped)
        if rule_match:
            rule_id = rule_match.group("rule")
            rule_meta = SPARSE_INDEX.lookup_by_id(rule_id)
            expected_cat = _classify_file(path)
            if rule_meta is None:
                if guideline_rules and rule_id not in guideline_rules:
                    _reject(
                        comment,
                        "UNKNOWN_RULE",
                        f"rule [{rule_id}] unknown to index and guidelines",
                        rejections,
                        counts,
                    )
                    continue
            else:
                rule_cat = rule_meta.get("category")
                if (
                    expected_cat is not None
                    and rule_cat != expected_cat
                    and rule_cat not in _ALWAYS_INCLUDE
                ):
                    _reject(
                        comment,
                        "G4",
                        f"rule [{rule_id}] category '{rule_cat}' != file cat '{expected_cat}'",
                        rejections,
                        counts,
                    )
                    continue
                if guideline_rules and rule_id not in guideline_rules:
                    _reject(
                        comment,
                        "GUIDELINE_MISS",
                        f"rule [{rule_id}] not in retrieved guidelines",
                        rejections,
                        counts,
                    )
                    continue

        survivors.append(comment)

    count_str = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "none"
    logger.info("[critic] survivors=%d rejected={%s}", len(survivors), count_str)

    return {
        "comments": Overwrite(value=survivors),
        "is_valid": True,
        "critic_feedback": None,
        "critic_issues": [
            {"comment": r["comment"], "reason": r["reason"]} for r in rejections
        ],
        "iterations": iterations,
    }


def linter_node(state: ReviewerState) -> dict:
    """Run ruff over added-line content of .py files; stash findings in state.

    Plan A scope: Python only. If ruff is missing or disabled, returns empty.
    Findings are informational — they seed analysts with ground-truth, they
    do NOT directly populate `comments` (that stays LLM-driven).
    """
    from src.core.config import settings

    if not settings.linter_enabled:
        return {}

    diff = _state_get(state, "diff", "")
    if not diff:
        return {"lint_findings": []}

    added_content = _build_added_content_map(diff)
    pr_files = extract_pr_files(diff)
    findings_out: list[str] = []
    checked = 0

    for path in pr_files:
        if not path.endswith(".py"):
            continue
        content = added_content.get(path, "").strip()
        if not content:
            continue
        checked += 1
        raw = run_ruff_on_file(path, content)
        for finding in raw:
            # ruff JSON: {code, message, location:{row,column}, filename, ...}
            code = finding.get("code") or "ruff"
            msg = finding.get("message") or ""
            loc = finding.get("location") or {}
            row = loc.get("row") or 0
            findings_out.append(f"{path}:{row} - [ruff:{code}] {msg}")

    logger.info(
        "[linter] py_files_checked=%d findings=%d",
        checked,
        len(findings_out),
    )
    return {"lint_findings": findings_out}


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
    from src.core.config import settings

    use_llm = settings.summarizer_use_llm
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
