# ai-local-reviewer

A GitHub App bot that automatically reviews Pull Requests. When added as a reviewer, it receives a webhook from GitHub, fetches the PR diff, and (in the future) sends it to LangGraph for AI analysis.

---

## Architecture

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant GH as GitHub
    participant App as ai-local-reviewer (FastAPI)
    participant GHApi as GitHub API

    Dev->>GH: Add bot as PR reviewer
    GH->>App: POST /webhook (event: pull_request, action: review_requested)
    App->>App: Verify HMAC signature (X-Hub-Signature-256)
    App->>GHApi: POST /app/installations/{id}/access_tokens (JWT)
    GHApi-->>App: Installation Access Token
    App->>GHApi: GET /repos/{owner}/{repo}/pulls/{pr} (diff)
    GHApi-->>App: PR diff text
    App->>App: TODO: Send diff to LangGraph
    App-->>GH: 200 OK
```

```mermaid
graph TD
    subgraph "ai-local-reviewer"
        A[main.py\nFastAPI webhook handler] --> B[verify_signature\nHMAC-SHA256]
        A --> C[utils/github_client.py\nGitHubClient]
        C --> D[_get_jwt\nRS256 Private Key]
        C --> E[get_token\nInstallation Access Token]
        C --> F[get_pull_request_diff\nPR diff]
    end

    GH[GitHub Webhooks] -->|POST /webhook| A
    C -->|HTTPS| GHAPI[GitHub REST API]
    A -.->|TODO| LG[LangGraph AI Review]
```

---

## Requirements

- Python 3.11+
- [GitHub App](https://docs.github.com/en/apps/creating-github-apps) with a private key (`.pem`)
- A publicly accessible URL for the webhook (e.g. via [ngrok](https://ngrok.com/))

---

## Setup & Run

### 1. Clone the repository

```bash
git clone <repo-url>
cd ai-local-reviewer
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy `.env_example` to `.env` and fill in the values:

```bash
cp .env_example .env
```

```env
GITHUB_APP_ID=123456                              # Your GitHub App ID
GITHUB_WEBHOOK_SECRET=your_webhook_secret         # Secret from GitHub App settings
GITHUB_PRIVATE_KEY_PATH=./oh-local-reviewer-ai.pem  # Path to the .pem file
GITHUB_BOT_NAME=your-bot-name                     # Bot login (without [bot] suffix)
```

### 4. Start the server

```bash
uvicorn src.main:app --reload --port 8000
```

The server will be available at `http://localhost:8000`.

### 5. Expose the webhook via ngrok (for local development)

```bash
ngrok http 8000
```

Copy the HTTPS URL from ngrok and set it in your GitHub App settings:
`Webhook URL: https://<ngrok-id>.ngrok.io/webhook`

---

## How it works

1. A developer adds the bot as a reviewer on a PR.
2. GitHub sends `POST /webhook` with event `pull_request` and `action: review_requested`.
3. The app verifies the request signature via HMAC-SHA256.
4. If the requested reviewer is the bot, it authenticates with GitHub API using JWT → Installation Access Token.
5. Downloads the PR diff in `.diff` format.
6. *(TODO)* Sends the diff to LangGraph for AI review and posts comments on the PR.
