"""ci.vllm.ai dashboard client: /api/jobs/runs failure index (no auth needed).

Verified live (2026-09-04): GET {base}/api/jobs/runs?jobName=Ascend NPU Test
&pipeline=CI&branch=main&startDate=...&endDate=... returns
{"runs": [{job_id, web_url, state, started_at, finished_at, duration_secs,
           commit_sha, build_created_at}]}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import httpx

from .config import DashboardConfig, JobConfig
from .net import get_with_retry

BUILD_NUM_RE = re.compile(r"/builds/(\d+)")


@dataclass
class DashboardRun:
    job_id: str
    web_url: str
    state: str
    started_at: str | None
    finished_at: str | None
    commit_sha: str | None
    build_number: int | None  # parsed from web_url


def parse_build_number(web_url: str | None) -> int | None:
    if not web_url:
        return None
    m = BUILD_NUM_RE.search(web_url)
    return int(m.group(1)) if m else None


class DashboardError(RuntimeError):
    pass


class DashboardClient:
    def __init__(self, cfg: DashboardConfig, job_cfg: JobConfig,
                 client: httpx.Client | None = None):
        self.cfg = cfg
        self.job_cfg = job_cfg
        self.client = client or httpx.Client(base_url=cfg.base_url, timeout=60.0)

    def fetch_runs(self, start_date: date, end_date: date) -> list[DashboardRun]:
        """All runs of the target job within [start_date, end_date] (both inclusive)."""
        params = {
            "jobName": self.job_cfg.name,
            "pipeline": self.cfg.pipeline,
            "branch": self.cfg.branch,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        }
        resp = get_with_retry(self.client, self.cfg.runs_endpoint, params=params)
        if resp.status_code != 200:
            raise DashboardError(f"dashboard returned {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        runs = []
        for r in payload.get("runs", []):
            runs.append(DashboardRun(
                job_id=r["job_id"],
                web_url=r.get("web_url") or "",
                state=r.get("state") or "",
                started_at=r.get("started_at"),
                finished_at=r.get("finished_at"),
                commit_sha=r.get("commit_sha"),
                build_number=parse_build_number(r.get("web_url")),
            ))
        return runs

    def fetch_failed_runs(self, start_date: date, end_date: date,
                          failure_states: list[str]) -> list[DashboardRun]:
        runs = self.fetch_runs(start_date, end_date)
        return [r for r in runs if r.state in failure_states and r.build_number is not None]
