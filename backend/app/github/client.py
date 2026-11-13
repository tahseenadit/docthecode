"""Thin async GitHub REST client for one GitHub host (github.com or GHE Server).

Two kinds of credentials are used:
  * App installation tokens: everything DocTheCode does on its own (read PR diffs,
    create check runs, comment on PRs). Requires the DocTheCode GitHub App to be
    installed on the code repo.
  * User tokens (from OAuth login): listing what the signed-in user can see and
    checking their permissions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from ..config import GitHubHost

API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str | None
    expires_at: float | None


class GitHubClient:
    def __init__(self, host: GitHubHost, http: httpx.AsyncClient | None = None):
        self.host = host
        self.http = http or httpx.AsyncClient(timeout=30)
        self._install_tokens: dict[int, tuple[str, float]] = {}

    # ------------------------------------------------------------- low level
    async def _request(self, method: str, path: str, token: str, *, accept: str = "application/vnd.github+json",
                       json: Any = None, params: dict | None = None, raw: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.host.api_url}{path}"
        r = await self.http.request(method, url, json=json, params=params, headers={
            "Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "DocTheCode",
        })
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise GitHubError(r.status_code, msg)
        if raw:
            return r.text
        return r.json() if r.content else None

    async def _paginate(self, path: str, token: str, key: str | None = None, max_pages: int = 30,
                        extra: dict | None = None) -> list:
        items, page = [], 1
        while page <= max_pages:
            data = await self._request("GET", path, token, params={**(extra or {}), "per_page": 100, "page": page})
            batch = data[key] if key else data
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    # ------------------------------------------------------------- app auth
    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.host.app_id},
                          self.host.private_key, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        cached = self._install_tokens.get(installation_id)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        data = await self._request("POST", f"/app/installations/{installation_id}/access_tokens", self.app_jwt())
        # expires_at is ISO8601; tokens live 1h, cache for 50 min.
        self._install_tokens[installation_id] = (data["token"], time.time() + 3000)
        return data["token"]

    async def app_installations(self) -> list[dict]:
        return await self._request("GET", "/app/installations", self.app_jwt(), params={"per_page": 100})

    async def repo_installation_id(self, owner: str, repo: str) -> int | None:
        try:
            data = await self._request("GET", f"/repos/{owner}/{repo}/installation", self.app_jwt())
            return data["id"]
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def install_url(self) -> str:
        return f"{self.host.web_url}/apps/{self.host.app_slug}/installations/new"

    # ------------------------------------------------------------- user OAuth
    def authorize_url(self, redirect_uri: str, state: str) -> str:
        q = urlencode({"client_id": self.host.client_id, "redirect_uri": redirect_uri, "state": state})
        return f"{self.host.web_url}/login/oauth/authorize?{q}"

    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthToken:
        return await self._token_request({"code": code, "redirect_uri": redirect_uri})

    async def refresh(self, refresh_token: str) -> OAuthToken:
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _token_request(self, extra: dict) -> OAuthToken:
        r = await self.http.post(f"{self.host.web_url}/login/oauth/access_token", headers={"Accept": "application/json"},
                                 data={"client_id": self.host.client_id, "client_secret": self.host.client_secret, **extra})
        data = r.json()
        if "access_token" not in data:
            raise GitHubError(r.status_code, data.get("error_description") or data.get("error") or "token exchange failed")
        exp = data.get("expires_in")
        return OAuthToken(data["access_token"], data.get("refresh_token"), time.time() + int(exp) if exp else None)

    async def current_user(self, user_token: str) -> dict:
        return await self._request("GET", "/user", user_token)

    async def user_primary_email(self, user_token: str) -> str | None:
        try:
            emails = await self._request("GET", "/user/emails", user_token)
            return next((e["email"] for e in emails if e.get("primary")), None)
        except GitHubError:
            return None

    async def user_installations(self, user_token: str) -> list[dict]:
        return await self._paginate("/user/installations", user_token, key="installations")

    async def user_installation_repos(self, user_token: str, installation_id: int) -> list[dict]:
        return await self._paginate(f"/user/installations/{installation_id}/repositories", user_token,
                                    key="repositories")

    async def user_repo_permission(self, user_token: str, owner: str, repo: str) -> str:
        """Returns admin/maintain/write/triage/read/none for the signed-in user."""
        try:
            data = await self._request("GET", f"/repos/{owner}/{repo}", user_token)
        except GitHubError as e:
            if e.status in (403, 404):
                return "none"
            raise
        perms = data.get("permissions") or {}
        for level in ("admin", "maintain", "push", "triage", "pull"):
            if perms.get(level):
                return {"push": "write", "pull": "read"}.get(level, level)
        return "none"

    # ------------------------------------------------------------- repo data
    async def get_repo(self, token: str, owner: str, repo: str) -> dict:
        return await self._request("GET", f"/repos/{owner}/{repo}", token)

    async def get_pull(self, token: str, owner: str, repo: str, number: int) -> dict:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}", token)

    async def recent_pulls(self, token: str, owner: str, repo: str, etag: str | None = None
                           ) -> tuple[list[dict] | None, str | None]:
        """Most recently updated PRs (all states). Returns (None, etag) when nothing changed since `etag`.

        Conditional requests that return 304 don't count against GitHub's rate limit, so checking is cheap.
        """
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION, "User-Agent": "DocTheCode"}
        if etag:
            headers["If-None-Match"] = etag
        r = await self.http.get(f"{self.host.api_url}/repos/{owner}/{repo}/pulls", headers=headers,
                                params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 50})
        if r.status_code == 304:
            return None, etag
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text[:300])
        return r.json(), r.headers.get("ETag")

    async def pull_files(self, token: str, owner: str, repo: str, number: int) -> list[dict]:
        return await self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/files", token)

    async def compare_diff(self, token: str, owner: str, repo: str, base: str, head: str) -> str:
        return await self._request("GET", f"/repos/{owner}/{repo}/compare/{base}...{head}", token,
                                   accept="application/vnd.github.diff", raw=True)

    async def file_content(self, token: str, owner: str, repo: str, path: str, ref: str) -> str | None:
        try:
            return await self._request("GET", f"/repos/{owner}/{repo}/contents/{quote(path)}", token,
                                       params={"ref": ref}, accept="application/vnd.github.raw+json", raw=True)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    async def tree(self, token: str, owner: str, repo: str, ref: str) -> list[str]:
        data = await self._request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}", token, params={"recursive": "1"})
        return [t["path"] for t in data.get("tree", []) if t["type"] == "blob"]

    async def repo_collaborators(self, token: str, owner: str, repo: str) -> list[dict]:
        """Everyone with access to the repo (direct, via teams, org base permission, outside collaborators)."""
        return await self._paginate(f"/repos/{owner}/{repo}/collaborators", token, extra={"affiliation": "all"})

    # ------------------------------------------------------------- checks / comments
    async def create_check_run(self, token: str, owner: str, repo: str, **body) -> dict:
        return await self._request("POST", f"/repos/{owner}/{repo}/check-runs", token, json=body)

    async def update_check_run(self, token: str, owner: str, repo: str, check_run_id: int, **body) -> dict:
        return await self._request("PATCH", f"/repos/{owner}/{repo}/check-runs/{check_run_id}", token, json=body)

    async def comment(self, token: str, owner: str, repo: str, number: int, body: str) -> dict:
        return await self._request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", token,
                                   json={"body": body})

    async def update_comment(self, token: str, owner: str, repo: str, comment_id: int, body: str) -> dict:
        return await self._request("PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}", token,
                                   json={"body": body})

    async def require_check_on_branch(self, user_token: str, owner: str, repo: str, branch: str,
                                      check_name: str) -> None:
        """Make the DocTheCode check required on `branch` (needs repo admin; uses the user's token)."""
        base = f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection"
        try:
            await self._request("POST", f"{base}/required_status_checks/contexts", user_token, json=[check_name])
            return
        except GitHubError as e:
            if e.status != 404:
                raise
        # No protection on the branch yet: create a minimal rule.
        await self._request("PUT", base, user_token, json={
            "required_status_checks": {"strict": False, "contexts": [check_name]},
            "enforce_admins": None, "required_pull_request_reviews": None, "restrictions": None,
        })
