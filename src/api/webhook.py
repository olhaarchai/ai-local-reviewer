import asyncio
import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request
from langgraph.types import Command

from src.core.config import settings
from src.core.review_log import write_review_log
from src.integrations.github_client import GitHubClient
from src.review.utils import dedup_comments, format_timings_inline, to_dict

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = settings.github_webhook_secret

router = APIRouter()


async def _prompt_user(prompt: str) -> str:
    import sys

    def _read() -> str:
        print(prompt, end="", flush=True, file=sys.stderr)
        return input()

    return await asyncio.to_thread(_read)


def _log_hitl_comments(comments: list) -> None:
    for c in (to_dict(x) for x in comments or []):
        owasp = f" [{c.get('owasp_id')}]" if c.get("owasp_id") else ""
        sev = f" ({c.get('severity', '').upper()})" if c.get("severity") else ""
        logger.info(
            "  %s%s %s:%s — %s",
            sev,
            owasp,
            c.get("path"),
            c.get("line"),
            c.get("body"),
        )


async def _read_hitl_decision(interrupt_value: dict) -> dict:
    """Show the interrupt payload and ask the user for approve/retry.

    Returns a resume dict: `{"action": "approve"}` or
    `{"action": "retry", "feedback": str}`.
    """
    if settings.hitl_auto_approve:
        return {"action": "approve"}

    comments = (
        interrupt_value.get("comments", []) if isinstance(interrupt_value, dict) else []
    )
    iterations = (
        interrupt_value.get("iterations", 0) if isinstance(interrupt_value, dict) else 0
    )
    logger.info(
        "── HITL pause (comments=%d, critic iter=%d) ──",
        len(comments),
        iterations,
    )
    _log_hitl_comments(comments)

    while True:
        try:
            raw = await _prompt_user("HITL — [a]pprove / [r]etry: ")
        except EOFError:
            logger.warning(
                "HITL: stdin closed (non-interactive run?) — auto-approving."
            )
            return {"action": "approve"}
        decision = raw.strip().lower()

        if decision in {"a", "approve", "y", "yes"}:
            logger.info("HITL: approved — proceeding to summarizer.")
            return {"action": "approve"}

        if decision in {"r", "retry"}:
            try:
                guidance = (
                    await _prompt_user(
                        "Optional guidance for the next pass (blank to skip): "
                    )
                ).strip()
            except EOFError:
                guidance = ""
            logger.info(
                "HITL: retrying analysts%s",
                " with guidance" if guidance else "",
            )
            return {"action": "retry", "feedback": guidance}

        logger.info("HITL: unknown choice %r, expected 'a' or 'r'.", decision)


async def _run_with_hitl(
    reviewer_app, initial_state, config
) -> tuple[dict, str, str | None]:
    """Run the graph, honouring interrupt() calls inside hitl_gate_node.

    Returns (final_state_values, hitl_action, hitl_feedback).
    """
    last_action = "approve"
    last_feedback: str | None = None

    async def _drain(stream_input) -> dict | None:
        async for chunk in reviewer_app.astream(
            stream_input, config=config, stream_mode="updates"
        ):
            if "__interrupt__" in chunk:
                interrupts = chunk["__interrupt__"]
                if interrupts:
                    value = getattr(interrupts[0], "value", None)
                    if value is None and isinstance(interrupts[0], dict):
                        value = interrupts[0].get("value")
                    return value or {}
        return None

    interrupt_value = await _drain(initial_state)

    while interrupt_value is not None:
        decision = await _read_hitl_decision(interrupt_value)
        last_action = decision.get("action", "approve")
        last_feedback = decision.get("feedback") or None
        interrupt_value = await _drain(Command(resume=decision))

    snapshot = await reviewer_app.aget_state(config)
    final_values = snapshot.values if isinstance(snapshot.values, dict) else {}
    return final_values, last_action, last_feedback


async def verify_signature(request: Request):
    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        raise HTTPException(status_code=401, detail="Signature missing")

    payload = await request.body()
    expected_signature = (
        "sha256="
        + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    )

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


@router.post("/webhook")
async def handle_webhook(request: Request, x_github_event: str = Header(None)):
    await verify_signature(request)

    payload = await request.json()
    action = payload.get("action")

    if x_github_event == "pull_request":
        pr_data = payload.get("pull_request")
        pr_number = pr_data.get("number")
        repo_name = payload.get("repository", {}).get("full_name")
        installation_id = payload.get("installation", {}).get("id")

        if action in ["review_requested", "synchronize"]:
            logger.info("── PR #%s  %s  action=%s ──", pr_number, repo_name, action)
            gh_client = GitHubClient(installation_id)

            try:
                diff_text = await gh_client.get_pull_request_diff(repo_name, pr_number)
                initial_state = {"diff": diff_text, "comments": [], "messages": []}
                thread_id = f"{repo_name}#{pr_number}"
                reviewer_app = request.app.state.reviewer_app
                config = {"configurable": {"thread_id": thread_id}}
                final_output, hitl_action, hitl_feedback = await _run_with_hitl(
                    reviewer_app, initial_state, config
                )

                write_review_log(
                    repo_name,
                    pr_number,
                    final_output,
                    hitl_action,
                    hitl_feedback=hitl_feedback,
                )

                ai_comments = [
                    to_dict(c) for c in final_output.get("comments", []) or []
                ]
                messages = final_output.get("messages") or []
                ai_summary = getattr(messages[-1], "content", "") if messages else ""

                timings_text = format_timings_inline(final_output.get("timings"))
                if timings_text:
                    logger.info("── timings ── %s", timings_text)
                logger.info("── review complete  issues=%d ──", len(ai_comments))
                for c in ai_comments:
                    owasp = f" [{c.get('owasp_id')}]" if c.get("owasp_id") else ""
                    sev = (
                        f" ({c.get('severity', '').upper()})"
                        if c.get("severity")
                        else ""
                    )
                    logger.info(
                        "  %s%s %s:%s — %s",
                        sev,
                        owasp,
                        c.get("path"),
                        c.get("line"),
                        c.get("body"),
                    )
                logger.info("── summary ──\n%s", ai_summary)

                if ai_comments:
                    ai_comments = dedup_comments(ai_comments)
                    logger.info("posting %d comment(s) to GitHub…", len(ai_comments))
                    await gh_client.post_review(
                        repo_name, pr_number, ai_summary, ai_comments
                    )
                    logger.info("review posted ✓")

            except Exception as exc:
                logger.exception("review failed for PR #%s: %s", pr_number, exc)

    return {"status": "ok"}
