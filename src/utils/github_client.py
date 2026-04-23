import logging
import os
import time

import httpx
import jwt

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, installation_id: int):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.private_key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")
        self.installation_id = installation_id
        self.base_url = "https://api.github.com"

    def _get_jwt(self) -> str:
        with open(self.private_key_path, "r") as f:
            private_key = f.read()

        payload = {
            "iat": int(time.time()) - 60,
            "exp": int(time.time()) + (10 * 60),
            "iss": self.app_id,
        }
        return jwt.encode(payload, private_key, algorithm="RS256")

    async def get_token(self) -> str:
        jwt_token = self._get_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/app/installations/{self.installation_id}/access_tokens"
            response = await client.post(url, headers=headers)
            response.raise_for_status()
            return response.json()["token"]

    async def get_pull_request_diff(self, repo_name: str, pr_number: int) -> str:
        token = await self.get_token()
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.diff",
        }

        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/repos/{repo_name}/pulls/{pr_number}"
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text

    async def post_review(
        self,
        repo_name: str,
        pr_number: int,
        body: str,
        comments: list,
    ) -> dict:
        token = await self.get_token()
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        url = f"{self.base_url}/repos/{repo_name}/pulls/{pr_number}/reviews"

        def _format_body(c: dict) -> str:
            parts = []
            if c.get("severity"):
                parts.append(f"**{c['severity'].upper()}**")
            if c.get("owasp_id"):
                parts.append(f"`{c['owasp_id']}`")
            parts.append(c["body"])
            return " ".join(parts)

        github_comments = [
            {"path": c["path"], "line": int(c["line"]), "body": _format_body(c)}
            for c in comments
            if c.get("path") and c.get("line") and c.get("body")
        ]

        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.post(
                url,
                json={
                    "body": body,
                    "event": "COMMENT",
                    "comments": github_comments,
                },
            )

            if response.status_code == 422:
                logger.warning(
                    "[github_client] 422 on inline comments (%s), falling back to summary-only",
                    response.json().get("errors"),
                )
                # Append only missing findings to avoid duplicates.
                missing_lines = []
                for c in github_comments:
                    marker = f"{c['path']}:{c['line']}"
                    if marker in body:
                        continue
                    missing_lines.append(f"- `{marker}` — {c['body']}")
                if missing_lines:
                    issues_md = "\n".join(missing_lines)
                    fallback_body = f"{body}\n\n---\n**Findings (inline comments unavailable):**\n{issues_md}"
                else:
                    fallback_body = body
                response = await client.post(
                    url,
                    json={
                        "body": fallback_body,
                        "event": "COMMENT",
                    },
                )

            if not response.is_success:
                logger.error(
                    "[github_client] post_review failed %d: %s",
                    response.status_code,
                    response.text,
                )

            response.raise_for_status()
            return response.json()
