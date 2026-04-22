import os
import time

import httpx
import jwt


class GitHubClient:
    def __init__(self, installation_id: int):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.private_key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")
        self.installation_id = installation_id
        self.base_url = "https://api.github.com"

    def _get_jwt(self) -> str:
        """Create a signed JWT to prove identity to GitHub."""
        with open(self.private_key_path, "r") as f:
            private_key = f.read()

        payload = {
            "iat": int(time.time()) - 60,  # Issued at (backdated 60s for safety)
            "exp": int(time.time()) + (10 * 60),  # Expiration (10 mins)
            "iss": self.app_id,
        }
        return jwt.encode(payload, private_key, algorithm="RS256")

    async def get_token(self) -> str:
        """Exchange JWT for an Installation Access Token."""
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
        """Fetch the PR changes in .diff format."""
        token = await self.get_token()
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.diff",  # This header is crucial!
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
        """Post a PR review with inline comments to GitHub."""
        token = await self.get_token()
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        github_comments = [
            {"path": c["path"], "line": int(c["line"]), "body": c["body"]}
            for c in comments
            if c.get("path") and c.get("line") and c.get("body")
        ]

        payload = {
            "body": body,
            "event": "COMMENT",
            "comments": github_comments,
        }

        async with httpx.AsyncClient(headers=headers) as client:
            url = f"{self.base_url}/repos/{repo_name}/pulls/{pr_number}/reviews"
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                import logging
                logging.getLogger(__name__).error(
                    "[github_client] post_review failed %d: %s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            return response.json()
