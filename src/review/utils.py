"""Shared helpers used across nodes, webhook, retriever and review_log.

Single source of truth for:
- state accessor that works on both dict and ReviewerState
- dict normalization for Pydantic / dict / arbitrary comment objects
- diff parsing (filter noise, extract paths, build added-line/content maps)
- comment field extraction and deterministic summary rendering
- LangChain message repr for diagnostic traces
- timings formatting
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def state_get(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


# ---------------------------------------------------------------------------
# dict normalization
# ---------------------------------------------------------------------------


def to_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {"value": str(obj)}


# ---------------------------------------------------------------------------
# diff parsing
# ---------------------------------------------------------------------------


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

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/\S+ b/(\S+)", re.MULTILINE)


def filter_diff_noise(raw_diff: str) -> str:
    """Drop binary/lockfile chunks from a unified diff."""
    chunks = re.split(r"(?=diff --git )", raw_diff)
    kept: list[str] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        header = chunk.split("\n")[0]
        if any(p in header for p in _NOISE_FILE_PATTERNS):
            logger.info("[filter_diff] Dropped: %s", header)
        else:
            kept.append(chunk)
    return "".join(kept)


def extract_pr_files(diff: str) -> list[str]:
    """Extract changed file paths from `diff --git` headers only."""
    return _DIFF_GIT_RE.findall(diff)


def build_added_line_map(diff: str) -> dict[str, set[int]]:
    """Map `path` → set of absolute line numbers that appear on `+` lines."""
    added: dict[str, set[int]] = {}
    current_path: str | None = None
    current_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            added.setdefault(current_path, set())
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            current_line = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if current_path:
                added[current_path].add(current_line)
            current_line += 1
        elif not line.startswith("-"):
            current_line += 1
    return added


def build_added_content_map(diff: str) -> dict[str, str]:
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


_EXT_TO_FENCE_LANG: dict[str, str] = {
    "py": "python",
    "ts": "typescript",
    "tsx": "tsx",
    "jsx": "jsx",
    "js": "javascript",
    "go": "go",
    "rs": "rust",
    "tf": "hcl",
    "sh": "bash",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "md": "markdown",
    "toml": "toml",
}


def format_diff_for_llm(diff: str) -> str:
    """Convert unified diff into per-file markdown with explicit line numbers.

    Output shape (one block per file with added lines):

        ## <path> (added <N> lines)
        ```<lang>
           5: const JWT_SECRET = 'x';
           6: const DB = 'y';
        ```

    Only `+` lines survive — analysts comment only on them anyway (critic G1
    rejects anything outside the `+` set). Dropping context/removed/hunk-header
    noise cuts 30-50% of prefill tokens vs raw unified diff, and making line
    numbers explicit removes the @@-hunk-math error class that we saw trigger
    G1 rejections in qwen2.5:7b runs.
    """
    files = extract_pr_files(diff)
    ordered: dict[str, list[tuple[int, str]]] = {p: [] for p in files}
    current_path: str | None = None
    current_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            ordered.setdefault(current_path, [])
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            current_line = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if current_path:
                ordered[current_path].append((current_line, line[1:]))
            current_line += 1
        elif not line.startswith("-"):
            current_line += 1

    out: list[str] = []
    for path in files:
        lines = ordered.get(path) or []
        if not lines:
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        lang = _EXT_TO_FENCE_LANG.get(ext, "")
        width = max(3, len(str(lines[-1][0])))
        out.append(f"## {path} (added {len(lines)} lines)")
        out.append(f"```{lang}")
        for num, content in lines:
            out.append(f"{str(num).rjust(width)}: {content}")
        out.append("```")
        out.append("")

    return "\n".join(out).rstrip()


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------


def extract_comment_fields(comment: Any) -> tuple[str, int, str]:
    """Return `(path, line, body)` normalised to `(str, int, str)`."""
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


def extract_comment_meta(comment: Any) -> tuple[str | None, str | None]:
    """Return `(owasp_id, severity)` — present on SecurityComment, absent on StyleComment."""
    if hasattr(comment, "owasp_id") or hasattr(comment, "severity"):
        return (
            getattr(comment, "owasp_id", None),
            getattr(comment, "severity", None),
        )
    if isinstance(comment, dict):
        return (comment.get("owasp_id"), comment.get("severity"))
    return (None, None)


def build_summary_from_comments(comments: list[Any]) -> str | None:
    """Render a deterministic 'Executive Summary' markdown block, or None if empty."""
    findings_lines: list[str] = []
    files: set[str] = set()
    for comment in comments:
        path, line, body = extract_comment_fields(comment)
        if not (path and line and body):
            continue
        files.add(path)
        owasp_id, severity = extract_comment_meta(comment)
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


def dedup_comments(comments: list[Any]) -> list[Any]:
    """Deduplicate comments by (path, body) — same finding on the same path wins once."""
    seen: set[tuple] = set()
    out: list[Any] = []
    for c in comments:
        path, _line, body = extract_comment_fields(c)
        key = (path, body.strip())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# message repr — LangChain messages serialised for the diagnostic log
# ---------------------------------------------------------------------------


_MESSAGE_CONTENT_PREVIEW_CHARS = 500


def message_repr(msg: Any) -> dict:
    """Compact dict representation of an AIMessage/HumanMessage/ToolMessage."""
    msg_type = type(msg).__name__
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    content_str = str(content) if content is not None else ""
    preview = content_str[:_MESSAGE_CONTENT_PREVIEW_CHARS]
    truncated = len(content_str) > _MESSAGE_CONTENT_PREVIEW_CHARS
    out: dict[str, Any] = {
        "type": msg_type,
        "content": preview + ("…[truncated]" if truncated else ""),
    }
    name = getattr(msg, "name", None)
    if name:
        out["name"] = name
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = [
            {
                "name": tc.get("name")
                if isinstance(tc, dict)
                else getattr(tc, "name", None),
                "args": tc.get("args")
                if isinstance(tc, dict)
                else getattr(tc, "args", None),
            }
            for tc in tool_calls
        ]
    return out


# ---------------------------------------------------------------------------
# timings
# ---------------------------------------------------------------------------


def format_timings_inline(timings: list[dict] | None) -> str | None:
    """One-line `name=1.23s, other=4.56s` — for stdout."""
    totals = _sum_timings(timings)
    if not totals:
        return None
    return ", ".join(f"{name}={totals[name]:.2f}s" for name in sorted(totals))


def format_timings_table(timings: list[dict] | None) -> str:
    """Two-column markdown table — for review_log."""
    totals = _sum_timings(timings)
    if not totals:
        return "_(no timings)_"
    lines = ["| node | seconds |", "|---|---|"]
    for name in sorted(totals):
        lines.append(f"| {name} | {totals[name]:.2f} |")
    return "\n".join(lines)


def _sum_timings(timings: list[dict] | None) -> dict[str, float]:
    totals: dict[str, float] = {}
    if not timings:
        return totals
    for entry in timings:
        if not isinstance(entry, dict):
            continue
        for name, seconds in entry.items():
            try:
                totals[name] = totals.get(name, 0.0) + float(seconds)
            except (TypeError, ValueError):
                continue
    return totals
