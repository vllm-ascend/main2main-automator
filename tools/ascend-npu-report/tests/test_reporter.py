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
    analysis = AnalysisResult(result=result,
                              section_found=True,
                              findings=findings if result == "BREAKS FOUND" else [])
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
