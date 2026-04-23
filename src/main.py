import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from langgraph.errors import GraphInterrupt

from src.agents.graph import build_reviewer_app, enter_checkpointer
from src.utils.github_client import GitHubClient

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("pymilvus").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.checkpointer_stack = AsyncExitStack()
    await app.state.checkpointer_stack.__aenter__()
    checkpointer = await enter_checkpointer(app.state.checkpointer_stack)
    app.state.reviewer_app = build_reviewer_app(
        checkpointer, interrupt_before=["summarizer"]
    )
    try:
        yield
    finally:
        await app.state.checkpointer_stack.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
BOT_NAME = os.getenv("GITHUB_BOT_NAME")


async def _prompt_user(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _collect_multiline(prompt: str) -> str:
    logger.info(prompt)
    lines: list[str] = []
    while True:
        line = await _prompt_user("")
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _looks_like_json(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        json.loads(stripped)
        return True
    except json.JSONDecodeError:
        return False


def _build_summary_from_comments(comments: list[dict]) -> str | None:
    findings = []
    files: set[str] = set()
    for comment in comments:
        path = str(comment.get("path", "")).strip()
        line = comment.get("line", 0)
        body = str(comment.get("body", "")).strip()
        if not (path and line and body):
            continue
        files.add(path)
        owasp_id = comment.get("owasp_id")
        severity = comment.get("severity")
        prefix = ""
        if severity:
            prefix += f"[{str(severity).upper()}] "
        if owasp_id:
            prefix += f"[{owasp_id}] "
        findings.append(f"- {prefix}{path}:{line} — {body}")

    if not findings:
        return None

    total = len(findings)
    file_count = len(files)
    return "\n".join(
        [
            "Executive Summary",
            "",
            f"Found {total} issue(s) across {file_count} file(s).",
            "",
            "Key findings:",
            *findings,
            "",
            "Recommendations:",
            "- Address the findings above and re-run the review.",
        ]
    ).strip()


def _format_timings(timings: list[dict[str, float]] | None) -> str | None:
    if not timings:
        return None
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
        return None
    parts = [f"{name}={totals[name]:.2f}s" for name in sorted(totals)]
    return ", ".join(parts)


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


@app.post("/webhook")
async def handle_webhook(request: Request, x_github_event: str = Header(None)):
    await verify_signature(request)

    payload = await request.json()
    action = payload.get("action")

    if x_github_event == "pull_request":
        pr_data = payload.get("pull_request")
        pr_number = pr_data.get("number")
        repo_name = payload.get("repository", {}).get("full_name")
        installation_id = payload.get("installation", {}).get("id")

        # Logic for triggered review
        if action in ["review_requested", "synchronize"]:
            logger.info("── PR #%s  %s  action=%s ──", pr_number, repo_name, action)
            gh_client = GitHubClient(installation_id)

            try:
                diff_text = await gh_client.get_pull_request_diff(repo_name, pr_number)
                initial_state = {"diff": diff_text, "comments": [], "messages": []}
                thread_id = f"{repo_name}#{pr_number}"
                reviewer_app = request.app.state.reviewer_app
                config = {"configurable": {"thread_id": thread_id}}
                try:
                    final_output = await reviewer_app.ainvoke(
                        initial_state, config=config
                    )
                except GraphInterrupt:
                    snapshot = await reviewer_app.aget_state(config)
                    values = (
                        snapshot.values if isinstance(snapshot.values, dict) else {}
                    )
                    ai_comments = values.get("comments", [])
                    logger.info("── HITL pause before summarizer ──")
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
                    decision = (
                        (
                            await _prompt_user(
                                "Approve review? [y]es / [e]dit summary / [n]o: "
                            )
                        )
                        .strip()
                        .lower()
                    )
                    if decision in {"n", "no"}:
                        logger.info("HITL: review aborted by user.")
                        return {"status": "ok"}
                    if decision in {"e", "edit"}:
                        summary_override = await _collect_multiline(
                            "Enter summary (finish with a single '.' line):"
                        )
                        if summary_override:
                            await reviewer_app.aupdate_state(
                                config, {"summary_override": summary_override}
                            )
                    final_output = await reviewer_app.ainvoke(
                        None, config=config, interrupt_before=[]
                    )

                ai_comments = final_output.get("comments", [])
                ai_summary = final_output["messages"][-1].content
                if _looks_like_json(ai_summary):
                    fallback = _build_summary_from_comments(ai_comments)
                    if fallback:
                        ai_summary = fallback

                timings_text = _format_timings(final_output.get("timings"))
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
                    logger.info("posting %d comment(s) to GitHub…", len(ai_comments))
                    await gh_client.post_review(
                        repo_name, pr_number, ai_summary, ai_comments
                    )
                    logger.info("review posted ✓")

            except Exception as e:
                logger.exception("review failed for PR #%s: %s", pr_number, e)

    return {"status": "ok"}
