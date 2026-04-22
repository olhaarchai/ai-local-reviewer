import hashlib
import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# 1. Import your compiled LangGraph app
from src.agents.graph import reviewer_app
from src.utils.github_client import GitHubClient

load_dotenv()

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
            # Check for bot mention or request (simplified logic here)
            print(f"[*] Processing AI Review for PR #{pr_number}...")

            gh_client = GitHubClient(installation_id)

            try:
                diff_text = await gh_client.get_pull_request_diff(repo_name, pr_number)

                # 2. Prepare the initial State for LangGraph
                initial_state = {"diff": diff_text, "comments": [], "messages": []}

                # 3. RUN THE BRAINS (Ollama + LangGraph)
                # This will trigger Security (Qwen) and Style (Mistral) analysts
                final_output = await reviewer_app.ainvoke(initial_state)

                # 4. Extract the results
                # For now, let's just print the summary from the 'summarizer' node
                ai_feedback = final_output["messages"][-1].content

                print("\n" + "=" * 50)
                print(f"AI REVIEW COMPLETE FOR PR #{pr_number}")
                print(ai_feedback)
                print("=" * 50 + "\n")

                # TODO: Pass the final_output['comments'] to gh_client to post them on GitHub

            except Exception as e:
                print(f"[!] Error during AI Review: {e}")

    return {"status": "ok"}
