"""CLI entry: run / backfill / dry-run.

Pipeline: dashboard failure index -> (cross-check) -> log fetch -> analyze
-> report (breaks-only main table with live PR status).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .analyzer import AnalysisResult, analyze_log
from .buildkite import BuildkiteClient, BuildkiteError
from .config import AppConfig, load_config
from .dashboard import DashboardClient, DashboardError, DashboardRun
from .github import GithubClient, GithubRateLimitError, PRInfo
from .reporter import (
    ReportRecord,
    dedup_merged_rows,
    parse_break_table,
    render_merged_report,
    render_report,
    window_slug,
)
from .store import Store

log = logging.getLogger(__name__)

_INTERFACE_CUTOFF = date(2026, 9, 3)  # CI 未上线 vllm-interface 的截止日期
_REPORT_RETENTION_DAYS = 90  # 保留最近 3 个月的报告

PR_NUM_RE = re.compile(r"\(#(\d+)\)\s*$")
BRANCH_PR_RE = re.compile(r"(?:pull-request|^pr)[/-](\d+)")


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.yaml"


def _compute_window(cfg: AppConfig, args) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if args.date:
        d = date.fromisoformat(args.date)
        return (datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
                datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1))
    if args.backfill:
        return now - timedelta(days=args.backfill), now
    last = args.window_start  # pre-filled from store by cmd_run
    if last:
        return last, now
    return now - timedelta(hours=cfg.schedule.lookback_default_hours), now


def _collect_failed_jobs(cfg: AppConfig, window_start: datetime,
                         window_end: datetime) -> tuple[list[DashboardRun], str]:
    """Primary: dashboard index. Fallback: Buildkite scan. Returns (jobs, source)."""
    dash = DashboardClient(cfg.dashboard, cfg.job)
    try:
        runs = dash.fetch_failed_runs(window_start.date(), window_end.date(),
                                      cfg.job.failure_states)
        return runs, "dashboard"
    except Exception as exc:  # noqa: BLE001 - degrade per design section 8
        log.warning("dashboard index failed (%s: %s)", type(exc).__name__, exc)
        if not cfg.buildkite.cross_check:
            raise
    log.info("degrading to Buildkite builds scan for the failure index")
    bk = BuildkiteClient(cfg.buildkite, cfg.job)
    jobs = bk.scan_failed_jobs(window_start, cfg.job.failure_states)
    runs = [DashboardRun(job_id=j.job_id, web_url=j.web_url or "", state=j.state,
                         started_at=None, finished_at=None, commit_sha=None,
                         build_number=j.build_number)
            for j in jobs]
    return runs, "buildkite"


def _cross_check(cfg: AppConfig, window_start: datetime, window_end: datetime,
                 jobs: list[DashboardRun]) -> list[DashboardRun]:
    """Merge Buildkite-side scan results (catches states the dashboard misses)."""
    if not cfg.buildkite.cross_check:
        return jobs
    try:
        bk = BuildkiteClient(cfg.buildkite, cfg.job)
    except BuildkiteError as exc:
        log.warning("cross-check skipped: %s", exc)
        return jobs
    known = {j.job_id for j in jobs}
    merged = list(jobs)
    for j in bk.scan_failed_jobs(window_start, cfg.job.failure_states,
                                 created_to=window_end):
        if j.job_id in known:
            continue
        merged.append(DashboardRun(
            job_id=j.job_id, web_url=j.web_url or "", state=j.state,
            started_at=None, finished_at=None, commit_sha=None,
            build_number=j.build_number))
        log.info("cross-check added job %s (build %s, state %s) not in dashboard index",
                 j.job_id, j.build_number, j.state)
    return merged


def _resolve_pr(gh: GithubClient, bk: BuildkiteClient | None, run: DashboardRun,
                store: Store) -> PRInfo | None:
    # cache first: saves GitHub quota across runs / reruns of the same PR
    if run.commit_sha:
        cached = store.get_cached_pr(run.commit_sha)
        if cached:
            return PRInfo(number=cached["number"], title=cached["title"],
                          status=cached["status"], html_url=cached["html_url"],
                          author=cached["author"], resolved_by="cache")
    pr = None
    try:
        pr = gh.lookup_by_commit(run.commit_sha) if run.commit_sha else None
        if pr is None and bk is not None:
            build = bk.get_build(run.build_number)
            pr_meta = (build.get("pull_request") or {}).get("id")
            if pr_meta and str(pr_meta).isdigit():
                pr = gh.lookup_by_number(int(pr_meta))
    except GithubRateLimitError as exc:
        log.warning("PR lookup skipped: %s", exc)
        return pr  # None
    if pr and run.commit_sha:
        store.save_pr_cache(run.commit_sha, pr.number, pr.title, pr.status,
                            pr.html_url, pr.author)
    return pr


def cmd_run(args) -> int:
    cfg = load_config(args.config or _default_config_path())
    store = Store(Path(cfg.store.dir))
    try:
        if getattr(args, "merge", False):
            rc = _cmd_merge(cfg, store, args)
        else:
            window_start, window_end = _compute_window(cfg, args)
            args.window_start = store.last_successful_run_end_parsed() or window_start
            window_start, window_end = _compute_window(cfg, args)
            rc = _run(cfg, store, window_start, window_end, args)
        _cleanup_old_reports(Path(cfg.report.output_dir))
        return rc
    finally:
        store.close()



def _cleanup_old_reports(out_dir: Path) -> None:
    """Delete report files whose window start date is older than the retention period."""
    if not out_dir.exists():
        return
    cutoff = date.today() - timedelta(days=_REPORT_RETENTION_DAYS)
    for p in sorted(out_dir.glob("*.md")):
        # Strip known prefixes to get the window slug
        name = p.name
        for prefix in ("merged-report-", "report-"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        # name should now be YYYYMMDDTHHMMZ-YYYYMMDDTHHMMZ
        parts = name.split("Z-")
        if len(parts) != 2:
            continue
        try:
            window_start = datetime.strptime(parts[0], "%Y%m%dT%H%M").date()
        except ValueError:
            continue
        if window_start < cutoff:
            p.unlink()
            log.info("cleaned up old report: %s", p.name)


def _find_report_for_date(out_dir: Path, d: date) -> Path | None:
    """Find an existing report file for a given date.

    Looks for window-keyed filenames that span the given date (i.e. the
    date falls within [window_start, window_end)).  Also checks the legacy
    ``report-YYYY-MM-DD.md`` naming.
    """
    # Legacy naming: report-YYYY-MM-DD.md
    legacy = out_dir / f"report-{d.isoformat()}.md"
    if legacy.exists():
        return legacy

    # Window-keyed naming: report-{start}Z-{end}Z.md
    # A window covers [start, end).  Match if d is in that range.
    day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    for p in sorted(out_dir.glob("report-*Z-*.md"), reverse=True):
        if p.name.startswith("merged-"):
            continue
        try:
            stem = p.stem  # report-XXXXXXXXTXXXXZ-XXXXXXXXTXXXXZ
            parts = stem.replace("report-", "").split("Z-")
            if len(parts) != 2:
                continue
            ws = datetime.strptime(parts[0], "%Y%m%dT%H%M").replace(
                tzinfo=timezone.utc)
            we = datetime.strptime(parts[1].rstrip("Z"), "%Y%m%dT%H%M").replace(
                tzinfo=timezone.utc)
            if ws <= day_start and day_end <= we:
                return p
        except ValueError:
            continue
    return None


def _cmd_merge(cfg: AppConfig, store: Store, args) -> int:
    """Merge existing daily reports into a single summary (no API calls for
    existing reports; missing days are fetched individually)."""
    now = datetime.now(timezone.utc)

    if args.date:
        d = date.fromisoformat(args.date)
        dates = [d]
    elif args.backfill:
        start = (now - timedelta(days=args.backfill)).date()
        dates = [start + timedelta(days=i) for i in range(args.backfill)]
    else:
        log.error("--merge requires --backfill or --date")
        return 1

    # Filter out dates before the interface cutoff
    valid_dates = [d for d in dates if d > _INTERFACE_CUTOFF]
    skipped = [d for d in dates if d <= _INTERFACE_CUTOFF]
    if skipped:
        log.info("skipped %d dates before interface cutoff %s",
                 len(skipped), _INTERFACE_CUTOFF)
    if not valid_dates:
        log.error("no processable dates in range (all before %s)",
                  _INTERFACE_CUTOFF)
        return 1

    out_dir = Path(cfg.report.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_reports: list[Path] = []

    for d in valid_dates:
        report_path = _find_report_for_date(out_dir, d)
        if report_path is None:
            log.info("report missing for %s, fetching...", d)
            day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            day_end = day_start + timedelta(days=1)
            # Temporarily override window args for the single-day run
            saved = getattr(args, "window_start", None)
            args.window_start = day_start
            _run(cfg, store, day_start, day_end, args)
            args.window_start = saved
            report_path = _find_report_for_date(out_dir, d)
        if report_path is not None:
            all_reports.append(report_path)
            log.info("include report: %s", report_path.name)
        else:
            log.warning("no report found for %s after fetch attempt", d)

    if not all_reports:
        log.error("no reports to merge")
        return 1

    # Parse break tables from all reports
    all_rows: list[dict] = []
    for p in all_reports:
        rows = parse_break_table(p)
        log.info("parsed %d break rows from %s", len(rows), p.name)
        all_rows.extend(rows)

    deduped = dedup_merged_rows(all_rows)

    window_start = datetime(valid_dates[0].year, valid_dates[0].month,
                            valid_dates[0].day, tzinfo=timezone.utc)
    window_end = datetime(valid_dates[-1].year, valid_dates[-1].month,
                          valid_dates[-1].day, tzinfo=timezone.utc) + timedelta(days=1)

    report_date = date.today().isoformat()
    report = render_merged_report(report_date, deduped,
                                  raw_failed=len(all_rows),
                                  window=(window_start, window_end))
    window_key = window_slug(window_start, window_end)
    out_path = out_dir / f"merged-report-{window_key}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("merged report written: %s", out_path)
    print(f"done: merged {len(all_reports)} reports, "
          f"{len(all_rows)} raw break records -> {len(deduped)} deduped "
          f"-> {out_path}")
    return 0


def _dedup_by_sha(jobs: list[DashboardRun]) -> list[DashboardRun]:
    """Same commit executed multiple times -> keep only the newest run.

    Input must be newest-first. Runs without a commit sha are never merged.
    """
    best: dict[str, DashboardRun] = {}
    for run in jobs:
        key = run.commit_sha or f"nocommit:{run.job_id}"
        best.setdefault(key, run)
    return list(best.values())


def _dedup_same_pr(pairs: list[tuple[DashboardRun, PRInfo | None]]
                   ) -> tuple[list[tuple[DashboardRun, PRInfo | None]],
                              list[tuple[DashboardRun, PRInfo | None]]]:
    """Same PR executed multiple times -> keep only the newest run (first in
    the newest-first input list). PR-less records are never merged."""
    kept: list[tuple[DashboardRun, PRInfo | None]] = []
    superseded: list[tuple[DashboardRun, PRInfo | None]] = []
    seen_pr: set[int] = set()
    for run, pr in pairs:
        if pr is not None and pr.number in seen_pr:
            superseded.append((run, pr))
            continue
        if pr is not None:
            seen_pr.add(pr.number)
        kept.append((run, pr))
    return kept, superseded


def _run(cfg: AppConfig, store: Store, window_start: datetime,
         window_end: datetime, args) -> int:
    run_id = str(uuid.uuid4())
    log.info("window: %s -> %s", window_start.isoformat(), window_end.isoformat())
    if not args.dry_run:
        store.start_run(run_id, window_start.isoformat(), window_end.isoformat())

    jobs, index_source = _collect_failed_jobs(cfg, window_start, window_end)
    jobs = _cross_check(cfg, window_start, window_end, jobs)
    jobs.sort(key=lambda j: (j.build_number or 0, j.job_id), reverse=True)
    total = len(jobs)
    jobs = _dedup_by_sha(jobs)   # pass 1: same commit rerun -> newest only
    log.info("failed jobs in window: %d (%d after same-commit dedup; source: %s)",
             total, len(jobs), index_source)

    if args.dry_run:
        for j in jobs:
            print(f"build #{j.build_number} | {j.state:<12} | {j.job_id} | "
                  f"sha={j.commit_sha} | {j.web_url}")
        print(f"total: {len(jobs)} failed runs after same-commit dedup "
              f"({total} raw; dry-run, nothing processed)")
        return 0

    bk: BuildkiteClient | None = None
    try:
        bk = BuildkiteClient(cfg.buildkite, cfg.job)
    except BuildkiteError as exc:
        log.warning("log fetching will fail without a token: %s", exc)
    gh = GithubClient(cfg.github)
    http = httpx.Client(timeout=60.0)

    # resolve PRs first so that pass-2 dedup can compare across commits.
    # Already-processed jobs are NOT dropped: the report is rewritten every
    # run, so their cached log snapshots are re-analyzed to keep prior
    # findings in it (re-analysis is local-only, no extra API calls).
    snapshot_key = window_start.date().isoformat()
    pending: list[tuple[DashboardRun, PRInfo | None]] = []
    for run in jobs:
        if store.is_processed(run.job_id) and not args.refetch:
            text = store.load_log_snapshot(snapshot_key, run.build_number,
                                           run.job_id)
            if text is None:
                log.info("skip processed job %s (build %s; no cached snapshot)",
                         run.job_id, run.build_number)
                continue
            pr = None
            if run.commit_sha:
                cached = store.get_cached_pr(run.commit_sha)
                if cached:
                    pr = PRInfo(number=cached["number"], title=cached["title"],
                                status=cached["status"],
                                html_url=cached["html_url"],
                                author=cached["author"], resolved_by="cache")
            pending.append((run, pr))
            continue
        pending.append((run, _resolve_pr(gh, bk, run, store)))

    # pass 2: same PR executed multiple times -> keep only the newest
    pending, superseded = _dedup_same_pr(pending)
    for run, pr in superseded:
        log.info("build %s superseded by a newer run of PR #%s; not collected",
                 run.build_number, pr.number)

    records: list[ReportRecord] = []
    findings_total = 0
    for run, pr in pending:
        analysis: AnalysisResult | None = None
        if bk is None:
            log.error("cannot fetch log for build %s: no Buildkite token",
                      run.build_number)
        else:
            try:
                log_text = bk.get_job_log(run.build_number, run.job_id)
                analysis = analyze_log(log_text, cfg.analysis)
                store.save_log_snapshot(snapshot_key,
                                        run.build_number, run.job_id, log_text)
                if analysis.included_in_table:
                    findings_total += len(analysis.findings)
                store.mark_processed(run.job_id, run.build_number, run.state,
                                     index_source, date.today().isoformat())
            except BuildkiteError as exc:
                log.error("log fetch failed for build %s job %s: %s",
                          run.build_number, run.job_id, exc)
        records.append(ReportRecord(
            job_id=run.job_id, build_number=run.build_number, state=run.state,
            web_url=run.web_url, pr=pr, analysis=analysis, source=index_source))

    report_date = date.today().isoformat()
    report = render_report(report_date, records, raw_failed=total,
                           window=(window_start, window_end))
    out_dir = Path(cfg.report.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Window-keyed filename: same window overwrites, new window is a new file.
    window_key = window_slug(window_start, window_end)
    out_path = out_dir / cfg.report.filename.format(date=report_date,
                                                    window=window_key)
    if records or cfg.report.write_empty_report:
        out_path.write_text(report, encoding="utf-8")
        log.info("report written: %s", out_path)
    else:
        log.info("no failures in window; report skipped")

    included = sum(1 for r in records if r.analysis and r.analysis.included_in_table)
    store.finish_run(run_id, ok=True, runs_scanned=len(jobs),
                     failures=len(records), findings=findings_total)
    print(f"done: {len(jobs)} failed runs, {included} with breaks, "
          f"{findings_total} findings -> {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="ascend-ci-report",
        description="Daily Ascend NPU Test CI failure monitor with break reporting")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="scan the window and generate the report")
    p_run.add_argument("--date", help="process a single day (YYYY-MM-DD)")
    p_run.add_argument("--backfill", type=int, metavar="N",
                       help="scan the last N days instead of the default window")
    p_run.add_argument("--refetch", action="store_true",
                       help="re-process jobs already recorded in the store")
    p_run.add_argument("--merge", action="store_true",
                       help="merge existing daily reports (requires --backfill or --date)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="collect the failure index only; fetch no logs, write nothing")
    p_run.add_argument("--config", help="path to config.yaml")
    p_run.set_defaults(func=cmd_run, window_start=None)

    args = parser.parse_args(argv)
    if getattr(args, "merge", False):
        if getattr(args, "refetch", False):
            parser.error("--merge and --refetch are mutually exclusive")
        if not getattr(args, "backfill", None) and not getattr(args, "date", None):
            parser.error("--merge requires --backfill or --date")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
