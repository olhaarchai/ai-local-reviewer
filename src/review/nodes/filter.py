import logging

from langgraph.types import Overwrite

from src.review.state import ReviewerState
from src.review.utils import filter_diff_noise, state_get

logger = logging.getLogger(__name__)


def filter_node(state: ReviewerState) -> dict:
    diff = state_get(state, "diff", "")
    filtered = filter_diff_noise(diff)
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
        "lint_findings": [],
    }
