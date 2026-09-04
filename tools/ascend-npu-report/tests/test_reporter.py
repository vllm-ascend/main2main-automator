from ascend_report.analyzer import AnalysisResult, BreakFinding
from ascend_report.config import AnalysisConfig, load_config
from ascend_report.github import PRInfo
from ascend_report.reporter import ReportRecord, render_report


def _rec(build: int, pr: int, status: str, result: str = "BREAKS FOUND",
         n_findings: int = 1, state: str = "failed") -> ReportRecord:
    findings = [BreakFinding(index=i + 1, priority="P0" if i == 0 else "P1",
                             relation="override", contract_kind="replacement_return",
                             vllm_api="vllm/x.py:Owner.method",
                             affected_code=f"vllm_ascend/x.py:{100 + i}",
                             impact=f"Impact {i + 1} | with pipe")
                for i in range(n_findings)]
    review = [BreakFinding(index=1, priority="P2", relation="override",
                           contract_kind="call_arguments",
                           vllm_api="vllm/v1/worker/utils.py:KVBlockZeroer.__init__",
                           affected_code="vllm_ascend/_310p/kv_block_zeroer.py:32",
                           review_reason="Contract difference needs manual review")]
    analysis = AnalysisResult(result=result,
                              section_found=True,
                              findings=findings if result == "BREAKS FOUND" else [],
                              review_findings=review)
    analysis.included_in_table = (result == "BREAKS FOUND" and bool(analysis.findings))
    return ReportRecord(
        job_id=f"job-{build}", build_number=build, state=state,
        web_url=f"https://buildkite.com/vllm/ci/builds/{build}",
        pr=PRInfo(number=pr, title=f"PR {pr} title", status=status,
                  html_url=f"https://github.com/vllm-project/vllm/pull/{pr}",
                  author="alice", resolved_by="commit_sha"),
        analysis=analysis)


def test_main_table_only_breaks_and_pr_status(breaks_log):
    records = [
        _rec(100, 22331, "Merged"),
        _rec(101, 22340, "Open", result="REVIEW"),   # no break -> appendix
        _rec(102, 22350, "Closed"),
    ]
    out = render_report("2026-09-04", records)
    assert "# Ascend NPU Test 失败报告 — 2026-09-04" in out
    assert "仅收录确认存在 break 的记录" in out
    assert "**Merged**" in out and "**Open**" in out and "**Closed**" in out
    # REVIEW record must not appear in the main table section
    main = out.split("## 附录 A")[0]
    assert "22340" not in main
    assert "附录 A" in out and "REVIEW" in out
    # merged PR sorts before closed PR
    assert out.index("**Merged**") < out.index("**Closed**")
    # pipe escaping
    assert "with pipe" in out and "Impact 1 \\| with pipe" in out
    # Review reason column in both main table and appendix A
    assert "| Review reason |" in out
    assert "Contract difference needs manual review" in out
    assert "待复核项 3 条" in out  # 3 records, each with 1 review finding


def test_same_pr_merged_into_one_row(breaks_log):
    records = [_rec(200, 22331, "Merged"), _rec(201, 22331, "Merged")]
    out = render_report("2026-09-04", records)
    main = out.split("## 附录 A")[0]
    assert main.count("[#22331]") == 1
    assert "#200" in main and "#201" in main


def test_empty_report_contains_stats_only():
    out = render_report("2026-09-04", [])
    assert "共发现 0 次" in out
    assert "Break 汇总表" not in out


def test_anomaly_record_in_appendix_b(no_section_log):
    from ascend_report.analyzer import analyze_log
    from ascend_report.config import AnalysisConfig
    analysis = analyze_log(no_section_log, AnalysisConfig())
    rec = ReportRecord(job_id="j1", build_number=300, state="failed",
                       web_url="https://buildkite.com/vllm/ci/builds/300",
                       pr=None, analysis=analysis)
    out = render_report("2026-09-04", [rec])
    assert "附录 B" in out
    assert "section_not_found" in out or "段落未找到" in out
    assert "````" in out  # 4-backtick fence used for raw blocks


def test_config_defaults_loadable(tmp_path):
    cfg = load_config(None)  # pure defaults
    assert cfg.buildkite.org == "vllm"
    assert cfg.buildkite.pipeline_slug == "ci"
    assert cfg.dashboard.pipeline == "CI"
    assert cfg.job.name == "Ascend NPU Test"


def test_dedup_same_pr_keeps_newest_only():
    from ascend_report.cli import _dedup_by_sha, _dedup_same_pr
    from ascend_report.dashboard import DashboardRun

    # pass 1: same commit sha rerun -> newest (input is newest-first)
    runs = [
        DashboardRun(job_id="j3", web_url="", state="failed", started_at=None,
                     finished_at=None, commit_sha="aaa", build_number=102),
        DashboardRun(job_id="j2a", web_url="", state="failed", started_at=None,
                     finished_at=None, commit_sha="bbb", build_number=101),
        DashboardRun(job_id="j2b", web_url="", state="failed", started_at=None,
                     finished_at=None, commit_sha="bbb", build_number=101),
        DashboardRun(job_id="j1", web_url="", state="failed", started_at=None,
                     finished_at=None, commit_sha=None, build_number=100),
    ]
    deduped = _dedup_by_sha(runs)
    assert [r.job_id for r in deduped] == ["j3", "j2a", "j1"]  # j2b dropped, no-sha kept

    # pass 2: two different commits resolve to the same PR -> keep newest
    pairs = [(deduped[0], _prinfo(500)), (deduped[1], _prinfo(500)),
             (deduped[2], None)]
    kept, superseded = _dedup_same_pr(pairs)
    assert [r.job_id for r, _ in kept] == ["j3", "j1"]
    assert [r.job_id for r, _ in superseded] == ["j2a"]


def _prinfo(n: int):
    return PRInfo(number=n, title=f"PR {n}", status="Open",
                  html_url=f"https://github.com/vllm-project/vllm/pull/{n}",
                  author="alice", resolved_by="commit_sha")


def test_report_stats_line_with_dedup():
    records = [_rec(300, 55123, "Open")]
    out = render_report("2026-09-04", records, raw_failed=71)
    assert "共发现 71 次" in out
    assert "仅保留最新一次" in out
    assert "去重后 1 次" in out


def test_report_shows_query_window():
    from datetime import datetime, timezone
    records = [_rec(300, 55123, "Open")]
    window = (datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
              datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc))
    out = render_report("2026-09-04", records, window=window)
    # window goes into the title line as well
    assert ("# Ascend NPU Test 失败报告 — 2026-09-03T00:00Z ~ "
            "2026-09-04T00:00Z") in out
    assert "查询时间窗：2026-09-03 00:00Z ~ 2026-09-04 00:00Z（UTC）" in out
    assert "2026-09-03 08:00 ~ 2026-09-04 08:00（北京时间，UTC+8）" in out


def test_window_slug_identity():
    from datetime import datetime, timezone
    from ascend_report.reporter import window_slug
    w = (datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc))
    assert window_slug(*w) == "20260903T0000Z-20260904T0000Z"
    # same window -> same slug (overwrite); new window -> different slug
    assert window_slug(*w) == window_slug(w[0], w[1])
    assert window_slug(*w) != window_slug(
        w[0], w[1].replace(hour=12))
