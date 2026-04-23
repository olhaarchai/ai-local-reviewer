"""LangChain @tool wrapper for persisting per-PR review transcripts.

Pattern mirrors hw-7's `save_report` in `hw-7/tools.py:50-76`: typed
args_schema + `@tool` decorator. Unlike hw-7, this tool is NOT called
by an LLM today — it's invoked directly from the webhook. The
decorator is there so a future archivist/summarizer agent can pick
it up without further plumbing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.core.config import settings
from src.core.review_log import _slug, _timestamp

logger = logging.getLogger(__name__)


class SaveReviewLogInput(BaseModel):
    repo_name: str = Field(description="GitHub repo full name, e.g. 'org/repo'.")
    pr_number: int = Field(description="Pull request number.")
    hitl_action: str = Field(
        description="'approve' or 'retry' — the last HITL decision."
    )
    hitl_feedback: str | None = Field(
        default=None, description="Optional retry guidance typed by the user."
    )
    content: str = Field(description="Pre-rendered markdown body to persist.")


@tool("save_review_log", args_schema=SaveReviewLogInput)
def save_review_log(
    repo_name: str,
    pr_number: int,
    hitl_action: str,
    hitl_feedback: str | None,
    content: str,
) -> str:
    """Persist the per-PR review transcript to OUTPUT_DIR as markdown.

    Always writes; no HITL gate. Returns the destination path as a
    string on success, or an error message. Meant to be called once
    at the end of the review pipeline.
    """
    try:
        out_dir = Path(settings.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{_slug(repo_name)}-pr{pr_number}-{_timestamp()}.md"
        path = out_dir / filename
        path.write_text(content, encoding="utf-8")
        logger.info("[review_log] Wrote %s", path)
        return str(path)
    except Exception as exc:  # noqa: BLE001 — tool errors must not crash callers
        logger.warning("[review_log] save_review_log failed: %s", exc)
        return f"Error: {exc}"
