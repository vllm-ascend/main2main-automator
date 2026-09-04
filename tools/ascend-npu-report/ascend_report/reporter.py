"""Markdown report rendering (structure per design doc 5.4)."""
from __future__ import annotations

from dataclasses import dataclass

from .analyzer import AnalysisResult
from .github import PRInfo

_PR_ORDER = {"Merged": 0, "Open": 1, "Closed": 2}
_REASON_ZH = {
    "section_not_found": "段落未找到（section_not_found）",
    "analyzer_error": "分析器错误（interface analysis failed）",
    "Result: REVIEW": "Result: REVIEW（待复核）",
    "Result: PASS": "Result: PASS（无 break）",
    "no_break": "无 break",
    "parse_failed": "结构化解析失败",
}


@dataclass
class ReportRecord:
    """One failed job with its analysis + PR info, ready for rendering."""
    job_id: str
    build_number: int | None
    state: str
    web_url: str
    pr: PRInfo | None
    analysis: AnalysisResult | None
    source: str = "dashboard"


def _esc(text: object) -> str:
    s = str(text or "")
    return s.replace("|", "\\|").replace("\n", "<br>")


def _code(text: object) -> str:
    return "`" + _esc(text) + "`" if text else "—"


def _pr_display(pr: PRInfo | None) -> str:
    if pr is None:
        return "—"
    label = f"#{pr.number}"
    return f"[{label}]({pr.html_url})" if pr.html_url else label


def _pr_status(pr: PRInfo | None) -> str:
    return f"**{pr.status}**" if pr else "—"


def _status_key(pr: PRInfo | None) -> int:
    if pr is None:
        return 3
    return _PR_ORDER.get(pr.status, 3)


def _group_by_pr(records: list[ReportRecord]) -> dict[object, list[ReportRecord]]:
    grouped: dict[object, list[ReportRecord]] = {}
    for r in records:
        # Unknown-PR records stay separate (one row per job), never merged.
        key: object = r.pr.number if r.pr else f"job-{r.job_id}"
        grouped.setdefault(key, []).append(r)
    return grouped


def _main_table(records: list[ReportRecord]) -> str:
    grouped = _group_by_pr(records)
    rows = []
    for group in grouped.values():
        group.sort(key=lambda r: (r.build_number or 0), reverse=True)
        head = group[0]
        analysis = head.analysis
        findings = analysis.findings if analysis else []
        builds = ", ".join(f"#{r.build_number}" for r in group if r.build_number)
        priorities = " / ".join(dict.fromkeys(
            f.priority for f in findings if f.priority))
        apis = "<br>".join(dict.fromkeys(
            _esc(f.vllm_api) for f in findings if f.vllm_api))
        affected = "<br>".join(dict.fromkeys(
            _esc(f.affected_code) for f in findings if f.affected_code))
        impacts = "<br>".join(dict.fromkeys(
            _esc(f.impact) for f in findings if f.impact))
        job_links = "<br>".join(
            f"[build#{r.build_number}]({r.web_url})" for r in group if r.web_url)
        rows.append((
            _status_key(head.pr),
            head.build_number or 0,
            f"| {builds or '—'} | {_pr_display(head.pr)} | {_pr_status(head.pr)} "
            f"| {_esc(head.pr.title) if head.pr else '—'} | {len(findings)} "
            f"| {priorities or '—'} | {apis or '—'} | {affected or '—'} "
            f"| {impacts or '—'} | {job_links or '—'} |",
        ))
    rows.sort(key=lambda t: (t[0], -t[1]))
    header = ("| Build | PR | PR 状态 | PR 标题 | Breaks | Priority "
              "| vLLM API 变更 | vllm-ascend 受影响代码 | 影响说明 | 日志链接 |\n"
              "| ----- | -- | ------- | ------- | ------ | -------- "
              "| ------------- | ---------------------- | -------- | -------- |")
    body = "\n".join(r[2] for r in rows)
    return f"{header}\n{body}\n"


def _appendix_a(records: list[ReportRecord]) -> str:
    header = ("| Build | PR | PR 状态 | Job 状态 | 原因 | 链接 |\n"
              "| ----- | -- | ------- | -------- | ---- | ---- |")
    lines = [header]
    for r in sorted(records, key=lambda r: (r.build_number or 0), reverse=True):
        reason = _REASON_ZH.get(
            r.analysis.anomaly_reason if r.analysis else "unknown",
            (r.analysis.anomaly_reason if r.analysis else "未分析"))
        link = f"[job]({r.web_url})" if r.web_url else "—"
        lines.append(
            f"| #{r.build_number or '?'} | {_pr_display(r.pr)} "
            f"| {_pr_status(r.pr)} | {_esc(r.state)} | {_esc(reason)} | {link} |")
    return "\n".join(lines) + "\n"


def _appendix_b(records: list[ReportRecord]) -> str:
    parts: list[str] = []
    for r in records:
        build_label = f"Build #{r.build_number or '?'}"
        build_ref = (f"[{build_label}]({r.web_url})" if r.web_url
                     else build_label)
        title = f"### {build_ref} / PR {r.pr.number if r.pr else '?'}"
        parts.append(title)
        a = r.analysis
        reason = _REASON_ZH.get(a.anomaly_reason if a else "unknown",
                                (a.anomaly_reason if a else "未分析"))
        parts.append(f"原因：{reason}")
        blocks: list[str] = []
        if a and a.fallback_hits:
            blocks.extend(a.fallback_hits)
        if not blocks:
            blocks.append("（无可提取片段，需人工核查日志）")
        for b in blocks:
            parts.append(f"````\n{b}\n````")
    return "\n\n".join(parts) + "\n"


def render_report(report_date: str, records: list[ReportRecord],
                  raw_failed: int | None = None) -> str:
    """records = failed jobs kept after dedup (same PR -> newest only)."""
    included = [r for r in records if r.analysis and r.analysis.included_in_table]
    no_break = [r for r in records
                if r.analysis and not r.analysis.included_in_table
                and r.analysis.section_found and not r.analysis.analyzer_error
                and r.analysis.anomaly_reason != "parse_failed"]
    anomalies = [r for r in records
                 if r.analysis and not r.analysis.included_in_table
                 and (not r.analysis.section_found
                      or r.analysis.anomaly_reason == "parse_failed"
                      or r.analysis.analyzer_error)]

    findings_total = sum(len(r.analysis.findings) for r in included)
    raw = raw_failed if raw_failed is not None else len(records)
    out = [f"# Ascend NPU Test 失败报告 — {report_date}", ""]
    if raw != len(records):
        out.append(f"统计：本时间窗内共发现 {raw} 次 Ascend NPU Test 失败运行，"
                   f"同 PR 多次执行仅保留最新一次（去重后 {len(records)} 次），"
                   f"其中 {len(included)} 次确认存在 break（已收录表格），"
                   f"提取 break 条目 {findings_total} 条。")
    else:
        out.append(f"统计：本时间窗内共发现 {len(records)} 次 Ascend NPU Test 失败运行，"
                   f"其中 {len(included)} 次确认存在 break（已收录表格），"
                   f"提取 break 条目 {findings_total} 条。")
    out.append("")

    if included:
        out.append("## Break 汇总表（仅收录确认存在 break 的记录）")
        out.append("")
        out.append(_main_table(included).rstrip("\n"))
        out.append("")
    if no_break:
        out.append("## 附录 A：失败但无 break 的记录（不进主表，仅备查）")
        out.append("")
        out.append(_appendix_a(no_break).rstrip("\n"))
        out.append("")
    if anomalies:
        out.append("## 附录 B：解析异常（需人工核查）")
        out.append("")
        out.append(_appendix_b(anomalies).rstrip("\n"))
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"
