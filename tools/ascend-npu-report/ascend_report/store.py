"""Local state: SQLite (dedup + run audit) and raw log snapshots on disk."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_jobs (
    job_id       TEXT PRIMARY KEY,
    build_number INTEGER,
    state        TEXT,
    source       TEXT,
    report_date  TEXT,
    first_seen   TEXT
);
CREATE TABLE IF NOT EXISTS pr_cache (
    commit_sha  TEXT PRIMARY KEY,
    number      INTEGER,
    title       TEXT,
    status      TEXT,
    html_url    TEXT,
    author      TEXT,
    fetched_at  TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    window_start TEXT,
    window_end  TEXT,
    ok          INTEGER,
    runs_scanned INTEGER,
    failures    INTEGER,
    findings    INTEGER
);
"""


@dataclass
class JobRecord:
    job_id: str
    build_number: int | None
    state: str
    source: str


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.db")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- dedup ------------------------------------------------------------

    def is_processed(self, job_id: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM processed_jobs WHERE job_id = ?", (job_id,))
        return cur.fetchone() is not None

    def mark_processed(self, job_id: str, build_number: int | None,
                       state: str, source: str, report_date: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processed_jobs VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, build_number, state, source, report_date,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.db.commit()

    # --- run audit ----------------------------------------------------------

    def start_run(self, run_id: str, window_start: str, window_end: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, window_start, window_end, ok)"
            " VALUES (?, ?, ?, ?, 0)",
            (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             window_start, window_end))
        self.db.commit()

    def finish_run(self, run_id: str, ok: bool, runs_scanned: int,
                   failures: int, findings: int) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, runs_scanned = ?,"
            " failures = ?, findings = ? WHERE run_id = ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             int(ok), runs_scanned, failures, findings, run_id))
        self.db.commit()

    def last_successful_run_end(self) -> str | None:
        cur = self.db.execute(
            "SELECT MAX(COALESCE(finished_at, started_at)) FROM runs WHERE ok = 1")
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def last_successful_run_end_parsed(self) -> datetime | None:
        s = self.last_successful_run_end()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    # --- PR lookup cache (saves GitHub API quota across runs) ---------------

    def get_cached_pr(self, commit_sha: str) -> dict | None:
        cur = self.db.execute(
            "SELECT number, title, status, html_url, author FROM pr_cache"
            " WHERE commit_sha = ?", (commit_sha,))
        row = cur.fetchone()
        if not row:
            return None
        return {"number": row[0], "title": row[1], "status": row[2],
                "html_url": row[3], "author": row[4]}

    def save_pr_cache(self, commit_sha: str, number: int, title: str | None,
                      status: str, html_url: str | None, author: str | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO pr_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
            (commit_sha, number, title, status, html_url, author,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.db.commit()

    # --- log snapshots --------------------------------------------------------

    def save_log_snapshot(self, run_date: str, build_number: int | None,
                          job_id: str, content: str) -> Path:
        log_dir = self.root / "logs" / run_date
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{build_number or 'unknown'}_{job_id}.log"
        path.write_text(content, encoding="utf-8")
        return path

    def load_log_snapshot(self, run_date: str, build_number: int | None,
                          job_id: str) -> str | None:
        """Cached log text by the same key save_log_snapshot wrote; None if absent."""
        path = (self.root / "logs" / run_date
                / f"{build_number or 'unknown'}_{job_id}.log")
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
