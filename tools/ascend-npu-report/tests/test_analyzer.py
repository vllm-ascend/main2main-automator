from ascend_report.analyzer import analyze_log
from ascend_report.config import AnalysisConfig


def test_breaks_found_parsed_and_included(breaks_log):
    cfg = AnalysisConfig()
    r = analyze_log(breaks_log, cfg)
    assert r.section_found
    assert r.result == "BREAKS FOUND"
    assert len(r.findings) == 2
    assert r.included_in_table
    f1 = r.findings[0]
    assert f1.index == 1
    assert f1.priority == "P0"
    assert f1.relation == "override"
    assert f1.contract_kind == "replacement_return"
    assert f1.vllm_api == "vllm/worker/worker_base.py:Worker.execute_model"
    assert f1.affected_code == "vllm_ascend/worker/worker_v1.py:123"
    assert f1.override_path.startswith("vllm_ascend/worker")
    assert "break" in f1.impact
    assert r.pytest_real_break is False  # no pytest fail line in fixture


def test_header_with_spaces_around_slash(breaks_log):
    """Real-world format (verified 2026-09-04): `### 1. P1 direct_call / call_target_presence`."""
    spaced = breaks_log.replace(
        "### 1. P0 override/replacement_return",
        "### 1. P1 direct_call / call_target_presence",
    ).replace(
        "### 2. P1 direct_call/positional_args",
        "### 2. P1 direct_import / symbol_presence",
    )
    r = analyze_log(spaced, AnalysisConfig())
    assert r.included_in_table and len(r.findings) == 2
    assert r.findings[0].priority == "P1"
    assert r.findings[0].relation == "direct_call"
    assert r.findings[0].contract_kind == "call_target_presence"
    assert r.findings[1].relation == "direct_import"


def test_review_not_included(review_log):
    r = analyze_log(review_log, AnalysisConfig())
    assert r.result == "REVIEW"
    assert r.findings  # entries still parsed
    assert not r.included_in_table
    assert r.anomaly_reason == "Result: REVIEW"


def test_pass_not_included(pass_log):
    r = analyze_log(pass_log, AnalysisConfig())
    assert r.result == "PASS"
    assert not r.included_in_table


def test_section_not_found_falls_back(no_section_log):
    r = analyze_log(no_section_log, AnalysisConfig())
    assert not r.section_found
    assert not r.included_in_table
    assert r.anomaly_reason == "section_not_found"
    assert r.pytest_real_break
    assert r.fallback_hits  # break regex still catches the pytest line


def test_analyzer_error_detected(analyzer_error_log):
    r = analyze_log(analyzer_error_log, AnalysisConfig())
    assert r.analyzer_error
    assert not r.included_in_table


def test_ansi_codes_stripped(ansi_log):
    r = analyze_log(ansi_log, AnalysisConfig())
    assert r.section_found
    assert r.result == "BREAKS FOUND"
    assert r.included_in_table


def test_breaks_found_but_unparseable_findings(breaks_log):
    # Corrupt the finding headers: BREAKS FOUND but 0 structured findings
    # -> parse_failed, NOT in main table (hard rule 5.3.2 #5).
    broken = breaks_log.replace("### 1. P0 override/replacement_return", "### 1. ???")
    broken = broken.replace("### 2. P1 direct_call/positional_args", "### 2. ???")
    r = analyze_log(broken, AnalysisConfig())
    assert r.result == "BREAKS FOUND"
    assert not r.findings
    assert not r.included_in_table
    assert r.anomaly_reason == "parse_failed"
