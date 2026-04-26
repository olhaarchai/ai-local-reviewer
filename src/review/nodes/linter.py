import logging

from src.review.state import ReviewerState
from src.review.utils import build_added_content_map, extract_pr_files, state_get
from src.tools.static_analysis import run_ruff_on_file

logger = logging.getLogger(__name__)


def linter_node(state: ReviewerState) -> dict:
    """Run ruff over added-line content of .py files; stash findings in state.

    Python only. If ruff is missing or disabled, returns empty. Findings are
    informational — they seed analysts with ground-truth; `comments` stays
    LLM-driven.
    """
    from src.core.config import settings

    if not settings.linter_enabled:
        return {}

    diff = state_get(state, "diff", "")
    if not diff:
        return {"lint_findings": []}

    added_content = build_added_content_map(diff)
    pr_files = extract_pr_files(diff)
    findings_out: list[str] = []
    checked = 0
    per_file_cap = settings.linter_max_findings_per_file
    total_cap = settings.linter_max_findings_total
    raw_total = 0

    for path in pr_files:
        if not path.endswith(".py"):
            continue
        content = added_content.get(path, "").strip()
        if not content:
            continue
        checked += 1
        raw = run_ruff_on_file(path, content)
        raw_total += len(raw)
        # Dedupe by code within a file — multiple E501 on the same file
        # don't help the LLM; one representative is plenty.
        seen_codes: set[str] = set()
        file_count = 0
        for finding in raw:
            if file_count >= per_file_cap:
                break
            code = finding.get("code") or "ruff"
            if code in seen_codes:
                continue
            seen_codes.add(code)
            msg = finding.get("message") or ""
            loc = finding.get("location") or {}
            row = loc.get("row") or 0
            findings_out.append(f"{path}:{row} - [ruff:{code}] {msg}")
            file_count += 1
            if len(findings_out) >= total_cap:
                break
        if len(findings_out) >= total_cap:
            break

    logger.info(
        "[linter] py_files_checked=%d raw_findings=%d kept=%d (cap=%d/file, %d/total)",
        checked,
        raw_total,
        len(findings_out),
        per_file_cap,
        total_cap,
    )
    return {
        "lint_findings": findings_out,
        "_progress_metrics": {"py_files": checked, "findings": len(findings_out)},
    }
