import logging

from langgraph.types import Overwrite, interrupt

from src.review.state import ReviewerState
from src.review.utils import state_get, to_dict

logger = logging.getLogger(__name__)


def hitl_gate_node(state: ReviewerState) -> dict:
    """Pause before summarizer for human approve/retry.

    Auto-approve short-circuits the interrupt(). On retry, we reset comments/
    messages/raw_responses and route back to retry_node via `state.route`.
    """
    from src.core.config import settings

    if settings.hitl_auto_approve:
        logger.info("[hitl_gate] auto-approve enabled — passing through")
        return {"route": "approve"}

    comments = state_get(state, "comments", []) or []
    iterations = state_get(state, "iterations", 0)
    lint_findings = state_get(state, "lint_findings", []) or []
    critic_issues = state_get(state, "critic_issues", []) or []

    payload = {
        "comments": [to_dict(c) for c in comments],
        "iterations": iterations,
        "lint_findings_count": len(lint_findings),
        "rejected_count": len(critic_issues),
    }
    logger.info(
        "[hitl_gate] pausing for user decision (comments=%d, iter=%d)",
        len(comments),
        iterations,
    )
    decision = interrupt(payload)

    action = (decision or {}).get("action", "approve")
    if action == "retry":
        feedback = (decision or {}).get("feedback") or None
        logger.info(
            "[hitl_gate] user requested retry%s",
            " with guidance" if feedback else "",
        )
        return {
            "comments": Overwrite(value=[]),
            "messages": Overwrite(value=[]),
            "raw_responses": Overwrite(value=[]),
            "critic_issues": [],
            "critic_feedback": feedback,
            "is_valid": False,
            "iterations": 0,
            "route": "retry",
        }

    logger.info("[hitl_gate] user approved")
    return {"route": "approve"}
