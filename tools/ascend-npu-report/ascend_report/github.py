"""GitHub API: resolve the PR behind a failed build and its current status."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from .config import GithubConfig
from .net import get_with_retry

log = logging.getLogger(__name__)

GH_API = "https://api.github.com"


@dataclass
class PRInfo:
    number: int
    title: str | None
    status: str            # Open / Merged / Closed
    html_url: str | None
    author: str | None
    resolved_by: str       # commit_sha / pr_number


class GithubRateLimitError(RuntimeError):
    """GitHub API quota exhausted (anonymous: 60 req/h)."""


def status_of(state: str, merged: bool) -> str:
    if merged:
        return "Merged"
    return "Open" if state == "open" else "Closed"


class GithubClient:
    def __init__(self, cfg: GithubConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.rate_limited = False   # once tripped, skip further calls this run
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get(cfg.token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(base_url=GH_API, timeout=60.0,
                                             headers=headers)

    def _lookup(self, path: str, context: str) -> httpx.Response | None:
        if self.rate_limited:
            raise GithubRateLimitError(
                f"GitHub rate limit already hit; skipping {context}")
        resp = get_with_retry(self.client, path)
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            self.rate_limited = True
            raise GithubRateLimitError(
                f"GitHub rate limit exceeded while {context}; set {self.cfg.token_env}"
                " to raise the limit (anonymous: 60 req/h)")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            log.warning("%s returned %d", context, resp.status_code)
            return None
        return resp

    def lookup_by_commit(self, sha: str) -> PRInfo | None:
        """Primary path: GET /repos/{repo}/commits/{sha}/pulls."""
        if not sha:
            return None
        resp = self._lookup(f"/repos/{self.cfg.repo}/commits/{sha}/pulls",
                            f"commit->PR lookup for {sha[:10]}")
        if resp is None:
            return None
        pulls = resp.json()
        if not pulls:
            return None
        p = pulls[0]
        return PRInfo(number=p["number"], title=p.get("title"),
                      status=status_of(p.get("state", ""), bool(p.get("merged"))),
                      html_url=p.get("html_url"),
                      author=(p.get("user") or {}).get("login"),
                      resolved_by="commit_sha")

    def lookup_by_number(self, number: int) -> PRInfo | None:
        """Fallback path: GET /repos/{repo}/pulls/{number}."""
        resp = self._lookup(f"/repos/{self.cfg.repo}/pulls/{number}",
                            f"PR #{number} lookup")
        if resp is None:
            return None
        p = resp.json()
        return PRInfo(number=p["number"], title=p.get("title"),
                      status=status_of(p.get("state", ""), bool(p.get("merged"))),
                      html_url=p.get("html_url"),
                      author=(p.get("user") or {}).get("login"),
                      resolved_by="pr_number")
