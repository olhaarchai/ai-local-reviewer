import logging

from langchain_core.messages import AIMessage

from src.review.state import ReviewerState
from src.review.utils import build_summary_from_comments, state_get

logger = logging.getLogger(__name__)


def summary_node(state: ReviewerState):
    """Deterministic executive summary — no LLM involved."""
    comments = state_get(state, "comments", [])
    logger.info(
        "[summarizer] Building deterministic summary from %d comment(s)", len(comments)
    )

    summary = build_summary_from_comments(comments)
    metrics = {"comments": len(comments)}
    if summary:
        return {
            "messages": [AIMessage(content=summary)],
            "_progress_metrics": metrics,
        }
    return {
        "messages": [AIMessage(content="Executive Summary\n\nNo issues found.")],
        "_progress_metrics": metrics,
    }
