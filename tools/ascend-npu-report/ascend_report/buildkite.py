"""Buildkite REST API client: cross-check scan + job log fetching.

Endpoints used (all read-only, read_builds scope):
  GET /v2/organizations/{org}/pipelines/{pipeline}/builds?state=failed,failing&created_from=...
  GET /v2/organizations/{org}/pipelines/{pipeline}/builds/{number}
  GET /v2/organizations/{org}/pipelines/{pipeline}/builds/{number}/jobs/{job_id}/log
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime

import httpx

from .config import BuildkiteConfig, JobConfig
from .net import get_with_retry

log = logging.getLogger(__name__)

API_BASE = "https://api.buildkite.com"
PER_PAGE = 100


class BuildkiteError(RuntimeError):
    pass


@dataclass
class BKJob:
    job_id: str
    name: str | None
    step_key: str | None
    state: str
    web_url: str | None
    build_number: int


@dataclass
class BKBuild:
    number: int
    branch: str | None
    message: str | None
    web_url: str | None
    jobs: list[BKJob]


class BuildkiteClient:
    def __init__(self, cfg: BuildkiteConfig, job_cfg: JobConfig,
                 client: httpx.Client | None = None):
        self.cfg = cfg
        self.job_cfg = job_cfg
        token = os.environ.get(cfg.api_token_env, "")
        if not token:
            raise BuildkiteError(
                f"missing token: set environment variable {cfg.api_token_env}")
        if client is not None:
            client.headers["Authorization"] = f"Bearer {token}"
            self.client = client
        else:
            self.client = httpx.Client(
                base_url=API_BASE, timeout=120.0,
                headers={"Authorization": f"Bearer {token}"})

    # --- builds -----------------------------------------------------------

    def _builds_url(self) -> str:
        return (f"/v2/organizations/{self.cfg.org}"
                f"/pipelines/{self.cfg.pipeline_slug}/builds")

    def iter_builds(self, created_from: datetime,
                    states: list[str] | None = None,
                    created_to: datetime | None = None) -> list[dict]:
        """All builds created after `created_from` (UTC), following pagination.

        Note: the REST API validates `state` as a single value (comma-separated
        lists get 422), so we query one state per request and merge.
        """
        builds: list[dict] = []
        queries = states if states else [None]
        for state in queries:
            page = 1
            while True:
                params: dict[str, str] = {
                    "per_page": str(PER_PAGE),
                    "page": str(page),
                    "created_from": created_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                if created_to is not None:
                    params["created_to"] = created_to.strftime("%Y-%m-%dT%H:%M:%SZ")
                if state:
                    params["state"] = state
                resp = get_with_retry(self.client, self._builds_url(), params=params)
                if resp.status_code != 200:
                    raise BuildkiteError(
                        f"builds list returned {resp.status_code}: {resp.text[:200]}")
                batch = resp.json()
                builds.extend(batch)
                if len(batch) < PER_PAGE:
                    break
                page += 1
        return builds

    def get_build(self, number: int) -> dict:
        resp = get_with_retry(self.client, f"{self._builds_url()}/{number}")
        if resp.status_code != 200:
            raise BuildkiteError(f"build {number} returned {resp.status_code}")
        return resp.json()

    # --- job matching -----------------------------------------------------

    def match_job(self, build: dict) -> BKJob | None:
        """Find the target job in a build payload (by step_key, then by name)."""
        for j in build.get("jobs") or []:
            if not isinstance(j, dict):
                continue
            jid = j.get("id")
            if not jid:
                continue
            step_key = j.get("step_key")
            name = j.get("name")
            if (self.job_cfg.step_key and step_key == self.job_cfg.step_key) or \
               (self.job_cfg.name and name == self.job_cfg.name):
                return BKJob(job_id=jid, name=name, step_key=step_key,
                             state=j.get("state") or "",
                             web_url=j.get("web_url"),
                             build_number=build.get("number", 0))
        return None

    def scan_failed_jobs(self, created_from: datetime,
                         failure_states: list[str],
                         created_to: datetime | None = None) -> list[BKJob]:
        """Cross-check pass: find target jobs in failure states within window."""
        found: list[BKJob] = []
        for build in self.iter_builds(created_from, self.cfg.cross_check_states,
                                      created_to=created_to):
            job = self.match_job(build)
            if job and job.state in failure_states:
                found.append(job)
        return found

    # --- logs -------------------------------------------------------------

    def get_job_log(self, build_number: int, job_id: str) -> str:
        """Full log content, following Link-header pagination."""
        url = f"{self._builds_url()}/{build_number}/jobs/{job_id}/log"
        parts: list[str] = []
        while True:
            resp = get_with_retry(self.client, url, params={"per_page": "5000"})
            if resp.status_code != 200:
                raise BuildkiteError(
                    f"log for build {build_number} job {job_id} returned {resp.status_code}")
            payload = resp.json()
            content = payload.get("content") or ""
            parts.append(content)
            nxt = resp.links.get("next", {}).get("url")
            if not nxt or not content:
                break
            url = nxt
        return "".join(parts)
