import hashlib
import hmac
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from utils.github_client import GitHubClient

load_dotenv()

app = FastAPI()

# Get your secret from .env
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
BOT_NAME = os.getenv("GITHUB_BOT_NAME")


async def verify_signature(request: Request):
    """Verify that the payload comes from GitHub."""
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
    # 1. Security check
    await verify_signature(request)

    payload = await request.json()
    action = payload.get("action")

    print(f"--- Received Event: {x_github_event} | Action: {action} ---")

    if x_github_event == "pull_request":
        pr_data = payload.get("pull_request")
        pr_number = pr_data.get("number")
        repo_name = payload.get("repository", {}).get("full_name")
        installation_id = payload.get("installation", {}).get("id")

        if action == "review_requested":
            # Check if bot was requested
            requested_reviewer = payload.get("requested_reviewer", {}).get("login")
            print(f"Review requested for PR #{pr_number} in {repo_name}")
            print(f"Target Reviewer: {requested_reviewer}")

            if (
                requested_reviewer == BOT_NAME
                or requested_reviewer == f"{BOT_NAME}[bot]"
            ):
                print(f"Bot {BOT_NAME} invited to review PR #{pr_number}")

                # 2. Initialize GitHub Client
                gh_client = GitHubClient(installation_id)

                # 3. Fetch the code changes
                try:
                    diff_text = await gh_client.get_pull_request_diff(
                        repo_name, pr_number
                    )
                    print(f"Successfully fetched diff for PR #{pr_number}")
                    print(
                        f"Diff sample: {diff_text[:200]}..."
                    )  # Print a small sample for debugging

                    # TODO: Send diff_text to LangGraph
                except Exception as e:
                    print(f"Error fetching diff: {e}")

        elif action == "synchronize":
            print(f"New code pushed to PR #{pr_number}. Updating review...")

    return {"status": "ok"}
