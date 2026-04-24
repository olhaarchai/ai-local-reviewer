"""Per-PR markdown review transcripts.

Each webhook run writes one file into `settings.output_dir` with the full
review trajectory: retrieved rules, lint findings, raw analyst outputs,
critic rejections, surviving comments, summary, HITL decision, timings.

Plus per-stage diagnostic traces (rag_trace, analyst_traces, critic_counts)
so hallucinations can be attributed to a specific stage by reading one file.

The writer is intentionally resilient — any exception is logged and swallowed
so that a broken log can never take down the webhook.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import settings
from src.review.utils import format_timings_table, state_get, to_dict

logger = logging.getLogger(__name__)


def _slug(repo_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", repo_name).strip("-") or "unknown-repo"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _safe(fn, *args, **kwargs) -> str:
    """Call a formatter; on failure return a placeholder so the log still renders."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review_log] formatter %s failed: %s", fn.__name__, exc)
        return f"_(formatter {fn.__name__} failed: {exc})_"


def _format_guidelines(guidelines: list) -> str:
    if not guidelines:
        return "_(none retrieved)_"
    groups: dict[str, list[str]] = {}
    for g in guidelines:
        d = to_dict(g)
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
        d = to_dict(c)
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
        d = to_dict(issue)
        reason = d.get("reason") or d.get("message") or d.get("rule_id", "?")
        comment = d.get("comment")
        if comment is not None:
            cd = to_dict(comment)
            path = cd.get("path", "?")
            line = cd.get("line", "?")
            body = cd.get("body", "")
            out.append(f"- **{reason}** — {path}:{line} — {body}")
        else:
            path = d.get("path", "?")
            line = d.get("line", "?")
            out.append(f"- **{reason}** — {path}:{line}")
    return "\n".join(out)


def _extract_summary(state: Any) -> str:
    messages = state_get(state, "messages", []) or []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return "_(no summary)_"


# ---------------------------------------------------------------------------
# Diagnostic sections (stage 5)
# ---------------------------------------------------------------------------


def _format_critic_counts(counts: dict | None) -> str:
    if not counts:
        return "_(no rejections)_"
    lines = ["| reason | count |", "|---|---|"]
    for reason in sorted(counts):
        lines.append(f"| {reason} | {counts[reason]} |")
    return "\n".join(lines)


def _format_rag_trace(rag_trace: list) -> str:
    if not rag_trace:
        return "_(no RAG trace captured)_"

    first = rag_trace[0] if rag_trace else {}
    inputs = first.get("inputs", {}) if isinstance(first, dict) else {}

    out: list[str] = []
    # Shared inputs block (same for every category this PR)
    out.append("### Shared inputs\n")
    sq = inputs.get("search_query", "")
    out.append("**search_query** (sent to Milvus and BM25):")
    out.append("```")
    out.append(sq if sq else "(empty)")
    out.append("```")
    out.append("")
    out.append(f"**bm25_tokens** (first 30): `{inputs.get('bm25_tokens', [])}`")
    out.append(f"**detected_stack**: `{inputs.get('detected_stack', [])}`")
    out.append(f"**file_paths**: `{inputs.get('file_paths', [])}`")
    out.append(
        f"**milvus_ok**: `{inputs.get('milvus_ok')}` · "
        f"**bm25_enabled**: `{inputs.get('bm25_enabled')}` · "
        f"**rerank**: `{inputs.get('use_reranker')}`"
    )
    out.append(
        f"**thresholds**: `distance≤{inputs.get('milvus_score_threshold')}`, "
        f"`per_cat_final={inputs.get('milvus_rules_per_category')}`, "
        f"`overfetch={inputs.get('milvus_overfetch_multiplier')}x`"
    )
    out.append("")

    # Per-category breakdown
    for entry in rag_trace:
        if not isinstance(entry, dict):
            continue
        cat = entry.get("category", "?")
        dense = entry.get("dense_hits", []) or []
        sparse = entry.get("sparse_hits", []) or []
        fused = entry.get("fused_order", []) or []
        kept = entry.get("kept", []) or []
        reranked = entry.get("reranked", False)

        out.append(
            f"### `{cat}` — dense={len(dense)} bm25={len(sparse)} kept={len(kept)}"
        )
        out.append("")

        if dense:
            out.append("**Dense (Milvus) hits:**\n")
            out.append("| text | distance |")
            out.append("|---|---|")
            for h in dense[:20]:
                t = str(h.get("text", "")).replace("|", r"\|")
                d = h.get("distance", 0.0)
                out.append(f"| {t} | {d:.3f} |")
            out.append("")
        else:
            out.append("**Dense (Milvus) hits:** _(none)_\n")

        if sparse:
            out.append("**BM25 hits:**\n")
            out.append("| text | score |")
            out.append("|---|---|")
            for h in sparse[:20]:
                t = str(h.get("text", "")).replace("|", r"\|")
                s = h.get("score", 0.0)
                out.append(f"| {t} | {s:.3f} |")
            out.append("")
        else:
            out.append("**BM25 hits:** _(none)_\n")

        if fused:
            out.append("**Fused order (RRF):**")
            for i, t in enumerate(fused[:20], start=1):
                out.append(f"{i}. {t}")
            out.append("")

        label = " [RERANKED]" if reranked else ""
        if kept:
            out.append(f"**Kept{label}:**")
            for t in kept:
                out.append(f"- {t}")
        else:
            out.append(f"**Kept{label}:** _(none)_")
        out.append("")

    return "\n".join(out).strip()


def _format_analyst_traces(traces: list) -> str:
    if not traces:
        return "_(no analyst traces captured)_"
    out: list[str] = []
    for t in traces:
        if not isinstance(t, dict):
            continue
        agent = t.get("agent", "?")
        model = t.get("model", "?")
        temp = t.get("temperature")
        num_ctx = t.get("num_ctx")
        dur = t.get("duration_s", 0.0)
        inp = t.get("inputs", {}) or {}
        outp = t.get("outputs", {}) or {}
        sys_chars = inp.get("system_chars", 0)
        diff_chars = inp.get("diff_chars", 0)
        raw_chars = outp.get("raw_chars", 0)
        structured_ok = outp.get("structured_ok")
        comments_count = outp.get("comments_count", 0)

        out.append(
            f"### `{agent}` — model=`{model}` temp={temp} "
            f"num_ctx={num_ctx} sys_chars={sys_chars} diff_chars={diff_chars} "
            f"raw_chars={raw_chars} structured={structured_ok} "
            f"comments={comments_count} took={dur:.2f}s"
        )
        out.append("")
        out.append("<details><summary>system_content (full)</summary>\n")
        out.append("```")
        out.append(str(inp.get("system_content", "")))
        out.append("```")
        out.append("\n</details>")
        out.append("")
        out.append("<details><summary>diff (full)</summary>\n")
        out.append("```diff")
        out.append(str(inp.get("diff", "")))
        out.append("```")
        out.append("\n</details>")
        out.append("")
        out.append("<details><summary>messages trace</summary>\n")
        out.append("```json")
        out.append(json.dumps(outp.get("messages", []), indent=2, ensure_ascii=False))
        out.append("```")
        out.append("\n</details>")
        out.append("")
        out.append("**raw_text (full):**\n")
        out.append("```json")
        out.append(str(outp.get("raw_text", "")))
        out.append("```")
        out.append("")
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_review_md(
    repo_name: str,
    pr_number: int | str,
    state: Any,
    hitl_action: str,
    hitl_feedback: str | None = None,
) -> str:
    """Render the per-PR markdown body. Pure — no IO."""
    guidelines = state_get(state, "guidelines", []) or []
    lint_findings = state_get(state, "lint_findings", []) or []
    critic_issues = state_get(state, "critic_issues", []) or []
    critic_counts = state_get(state, "critic_counts", {}) or {}
    rag_trace = state_get(state, "rag_trace", []) or []
    analyst_traces = state_get(state, "analyst_traces", []) or []
    comments = state_get(state, "comments", []) or []
    iterations = state_get(state, "iterations", 0)
    stack_context = state_get(state, "stack_context", "") or ""
    timings = state_get(state, "timings", []) or []

    models = (
        f"- provider: `{settings.type_agents}`\n"
        f"- security: `{settings.model_security}`\n"
        f"- style: `{settings.model_style}`"
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

## RAG breakdown

{_safe(_format_rag_trace, rag_trace)}

## Retrieved guidelines ({len(guidelines)})

{_safe(_format_guidelines, guidelines)}

## Lint findings ({len(lint_findings)})

{_safe(_format_lint, lint_findings)}

## Analyst prompts & responses ({len(analyst_traces)})

{_safe(_format_analyst_traces, analyst_traces)}

## Critic breakdown

{_safe(_format_critic_counts, critic_counts)}

## Critic rejections ({len(critic_issues)})

{_safe(_format_rejections, critic_issues)}

## Surviving comments ({len(comments)})

{_safe(_format_comments, comments)}

## Summary

{_extract_summary(state)}

## Timings

{_safe(format_timings_table, timings)}
"""


def write_review_log(
    repo_name: str,
    pr_number: int | str,
    state: Any,
    hitl_action: str,
    hitl_feedback: str | None = None,
    output_dir: str | Path | None = None,
) -> Path | None:
    """Render markdown and persist via the save_review_log @tool."""
    from src.tools.review_log_tool import save_review_log

    try:
        content = render_review_md(
            repo_name, pr_number, state, hitl_action, hitl_feedback
        )
        if output_dir is not None:
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = _timestamp()
            path = out_dir / f"{_slug(repo_name)}-pr{int(pr_number)}-{stamp}.md"
            path.write_text(content, encoding="utf-8")
            logger.info("[review_log] Wrote %s", path)
            diff = state_get(state, "diff", "")
            if diff:
                diff_path = (
                    out_dir / f"{_slug(repo_name)}-pr{int(pr_number)}-{stamp}.diff"
                )
                diff_path.write_text(diff, encoding="utf-8")
                logger.info("[review_log] Wrote %s", diff_path)
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
        md_path = Path(result) if isinstance(result, str) else None
        # Dump raw diff alongside the .md for offline inspection.
        diff = state_get(state, "diff", "")
        if md_path and diff:
            diff_path = md_path.with_suffix(".diff")
            try:
                diff_path.write_text(diff, encoding="utf-8")
                logger.info("[review_log] Wrote %s", diff_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[review_log] Failed to write diff: %s", exc)
        return md_path
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review_log] Failed to write log: %s", exc)
        return None
