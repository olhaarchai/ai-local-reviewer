import asyncio
import hashlib
import hmac
import json
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Request
from langgraph.errors import GraphInterrupt

from src.core.config import settings
from src.core.types import Timings
from src.integrations.github_client import GitHubClient

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = settings.github_webhook_secret

router = APIRouter()


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


def _parse_diff_lines(diff: str) -> dict[str, set[int]]:
    valid: dict[str, set[int]] = {}
    current_file: str | None = None
    current_line = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" b/", 1)
            current_file = parts[1].strip() if len(parts) == 2 else None
            if current_file:
                valid.setdefault(current_file, set())
            current_line = 0
        elif raw.startswith("@@") and current_file is not None:
            m = re.search(r"\+(\d+)", raw)
            current_line = int(m.group(1)) - 1 if m else 0
        elif current_file is not None:
            if raw.startswith("+++") or raw.startswith("---"):
                continue
            if raw.startswith("-"):
                continue
            current_line += 1
            valid[current_file].add(current_line)
    return valid


def _dedup_comments(comments: list) -> list:
    seen: set[tuple] = set()
    out = []
    for c in comments:
        key = (str(c.get("path", "")), str(c.get("body", "")).strip())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _format_timings(timings: Timings | None) -> str | None:
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
                    ai_comments = _dedup_comments(ai_comments)
                    diff_lines = _parse_diff_lines(diff_text)
                    valid = [
                        c
                        for c in ai_comments
                        if c.get("path") in diff_lines
                        and int(c.get("line") or 0) in diff_lines[c["path"]]
                    ]
                    dropped = len(ai_comments) - len(valid)
                    if dropped:
                        logger.info(
                            "[webhook] Dropped %d comment(s) outside diff", dropped
                        )
                    ai_comments = valid
                    logger.info("posting %d comment(s) to GitHub…", len(ai_comments))
                    await gh_client.post_review(
                        repo_name, pr_number, ai_summary, ai_comments
                    )
                    logger.info("review posted ✓")

            except Exception as exc:
                logger.exception("review failed for PR #%s: %s", pr_number, exc)

    return {"status": "ok"}
