import hashlib
import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from src.agents.graph import reviewer_app
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

app = FastAPI()

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
BOT_NAME = os.getenv("GITHUB_BOT_NAME")


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
                final_output = await reviewer_app.ainvoke(initial_state)

                ai_summary = final_output["messages"][-1].content
                ai_comments = final_output.get("comments", [])

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
