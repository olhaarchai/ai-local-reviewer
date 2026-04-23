"""Per-PR markdown review transcripts.

Each webhook run writes one file into `settings.output_dir` with the full
review trajectory: retrieved rules, lint findings, raw analyst outputs,
critic rejections, surviving comments, summary, HITL decision, timings.

The writer is intentionally resilient — any exception is logged and
swallowed so that a broken log can never take down the webhook.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)

_MAX_RAW_RESPONSE_CHARS = 4000


def _slug(repo_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", repo_name).strip("-") or "unknown-repo"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _state_get(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _to_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {"value": str(obj)}


def _format_guidelines(guidelines: list) -> str:
    if not guidelines:
        return "_(none retrieved)_"
    groups: dict[str, list[str]] = {}
    for g in guidelines:
        d = _to_dict(g)
        cat = d.get("category", "?")
        groups.setdefault(cat, []).append(str(d.get("text", "")))
    lines: list[str] = []
    for cat in sorted(groups):
        lines.append(f"**{cat}** ({len(groups[cat])}):")
        for text in groups[cat]:
            lines.append(f"- {text}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_lint(findings: list) -> str:
    if not findings:
        return "_(no lint findings)_"
    return "\n".join(f"- {line}" for line in findings)


def _format_comments(comments: list) -> str:
    if not comments:
        return "_(no surviving comments)_"
    out: list[str] = []
    for c in comments:
        d = _to_dict(c)
        path = d.get("path", "?")
        line = d.get("line", "?")
        body = d.get("body", "")
        owasp = f" [{d['owasp_id']}]" if d.get("owasp_id") else ""
        sev = f" ({str(d['severity']).upper()})" if d.get("severity") else ""
        out.append(f"- **{path}:{line}**{sev}{owasp} — {body}")
    return "\n".join(out)


def _format_rejections(critic_issues: list) -> str:
    if not critic_issues:
        return "_(none)_"
    out: list[str] = []
    for issue in critic_issues:
        d = _to_dict(issue)
        reason = d.get("reason") or d.get("message") or d.get("rule_id", "?")
        comment = d.get("comment")
        if comment is not None:
            cd = _to_dict(comment)
            path = cd.get("path", "?")
            line = cd.get("line", "?")
            body = cd.get("body", "")
            out.append(f"- **{reason}** — {path}:{line} — {body}")
        else:
            path = d.get("path", "?")
            line = d.get("line", "?")
            out.append(f"- **{reason}** — {path}:{line}")
    return "\n".join(out)


def _format_timings(timings: list) -> str:
    if not timings:
        return "_(no timings)_"
    totals: dict[str, float] = {}
    for entry in timings:
        if not isinstance(entry, dict):
            continue
        for name, seconds in entry.items():
            try:
                totals[name] = totals.get(name, 0.0) + float(seconds)
            except (TypeError, ValueError):
                continue
    if not totals:
        return "_(no timings)_"
    lines = ["| node | seconds |", "|---|---|"]
    for name in sorted(totals):
        lines.append(f"| {name} | {totals[name]:.2f} |")
    return "\n".join(lines)


def _format_raw_responses(raw_responses: list) -> str:
    if not raw_responses:
        return "_(no raw responses)_"
    blocks: list[str] = []
    for i, raw in enumerate(raw_responses, start=1):
        text = str(raw)
        if len(text) > _MAX_RAW_RESPONSE_CHARS:
            text = (
                text[:_MAX_RAW_RESPONSE_CHARS]
                + f"\n... [truncated {len(raw) - _MAX_RAW_RESPONSE_CHARS} chars]"
            )
        blocks.append(f"#### Response {i}\n\n```json\n{text}\n```")
    return "\n\n".join(blocks)


def _extract_summary(state: Any) -> str:
    messages = _state_get(state, "messages", []) or []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return "_(no summary)_"


def render_review_md(
    repo_name: str,
    pr_number: int | str,
    state: Any,
    hitl_action: str,
    hitl_feedback: str | None = None,
) -> str:
    """Render the per-PR markdown body. Pure — no IO."""
    guidelines = _state_get(state, "guidelines", []) or []
    lint_findings = _state_get(state, "lint_findings", []) or []
    raw_responses = _state_get(state, "raw_responses", []) or []
    critic_issues = _state_get(state, "critic_issues", []) or []
    comments = _state_get(state, "comments", []) or []
    iterations = _state_get(state, "iterations", 0)
    stack_context = _state_get(state, "stack_context", "") or ""
    timings = _state_get(state, "timings", []) or []

    models = (
        f"- security: `{settings.ollama_model_security}`\n"
        f"- style: `{settings.ollama_model_style}`\n"
        f"- fast: `{settings.ollama_model_fast}`"
    )

    feedback_block = f"\n> **Guidance:** {hitl_feedback}" if hitl_feedback else ""

    return f"""# Review — {repo_name} PR #{pr_number}

Generated: `{_timestamp()}`

## HITL

- **Action:** `{hitl_action}`{feedback_block}
- **Critic iterations:** {iterations}

## Models

{models}

## Stack / context

```
{stack_context or "(none)"}
```

## Retrieved guidelines ({len(guidelines)})

{_format_guidelines(guidelines)}

## Lint findings ({len(lint_findings)})

{_format_lint(lint_findings)}

## Analyst raw responses ({len(raw_responses)})

{_format_raw_responses(raw_responses)}

## Critic rejections ({len(critic_issues)})

{_format_rejections(critic_issues)}

## Surviving comments ({len(comments)})

{_format_comments(comments)}

## Summary

{_extract_summary(state)}

## Timings

{_format_timings(timings)}
"""


def write_review_log(
    repo_name: str,
    pr_number: int | str,
    state: Any,
    hitl_action: str,
    hitl_feedback: str | None = None,
    output_dir: str | Path | None = None,
) -> Path | None:
    """Render markdown and persist via the save_review_log @tool.

    Kept as the single call site for callers (webhook, tests) so the
    public surface doesn't churn. Delegates to the tool for the write.
    """
    # Local import avoids circular: review_log_tool imports _slug/_timestamp
    # from this module.
    from src.tools.review_log_tool import save_review_log

    try:
        content = render_review_md(
            repo_name, pr_number, state, hitl_action, hitl_feedback
        )
        # output_dir override is honoured by writing directly when set —
        # the tool always uses settings.output_dir.
        if output_dir is not None:
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{_slug(repo_name)}-pr{int(pr_number)}-{_timestamp()}.md"
            path.write_text(content, encoding="utf-8")
            logger.info("[review_log] Wrote %s", path)
            return path

        result = save_review_log.invoke(
            {
                "repo_name": repo_name,
                "pr_number": int(pr_number),
                "hitl_action": hitl_action,
                "hitl_feedback": hitl_feedback,
                "content": content,
            }
        )
        if isinstance(result, str) and result.startswith("Error:"):
            logger.warning("[review_log] save_review_log reported: %s", result)
            return None
        return Path(result) if isinstance(result, str) else None
    except Exception as exc:  # noqa: BLE001 — logging must never break the caller
        logger.warning("[review_log] Failed to write log: %s", exc)
        return None
