"""Log analysis: extract `+++ vLLM PR compatibility` sections, classify the
Result line, parse structured break findings, apply the hard table-inclusion
rule (BREAKS FOUND + >=1 finding).

Log format verified from vllm-ascend source:
  test_vllm_pr_interface_compatibility.py + range_analysis.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import AnalysisConfig

# ANSI: CSI sequences (\x1b[...letter), OSC sequences (\x1b...BEL), stray BEL.
# Buildkite log lines carry `_bk;t=<epoch_ms>` wrapped as \x1b...\x07.
ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|[^\[]*?\x07)|\x07")


@dataclass
class BreakFinding:
    index: int | None = None
    priority: str | None = None
    relation: str | None = None
    contract_kind: str | None = None
    vllm_api: str | None = None
    affected_code: str | None = None
    impact: str | None = None
    override_path: str | None = None
    review_reason: str | None = None       # only on "Items for review" entries
    raw_text: str = ""


@dataclass
class AnalysisResult:
    result: str | None = None               # BREAKS FOUND / REVIEW / PASS / None
    section_found: bool = False
    findings: list[BreakFinding] = field(default_factory=list)  # breaks only
    review_findings: list[BreakFinding] = field(default_factory=list)
    fallback_hits: list[str] = field(default_factory=list)  # (context blocks)
    pytest_real_break: bool = False
    analyzer_error: bool = False
    included_in_table: bool = False         # hard rule (5.3.2 #5)

    @property
    def anomaly_reason(self) -> str | None:
        """Why this record is not in the main table (None if included)."""
        if self.included_in_table:
            return None
        if not self.section_found:
            return "section_not_found"
        if self.analyzer_error:
            return "analyzer_error"
        if self.result == "REVIEW":
            return "Result: REVIEW"
        if self.result == "PASS":
            return "Result: PASS"
        if self.result == "BREAKS FOUND" and not self.findings:
            return "parse_failed"
        return "no_break"


def _clean(text: str) -> str:
    text = ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def analyze_log(text: str, cfg: AnalysisConfig) -> AnalysisResult:
    text = _clean(text)
    out = AnalysisResult()

    out.pytest_real_break = cfg.pytest_fail_patterns["real_break"] in text
    out.analyzer_error = cfg.pytest_fail_patterns["analyzer_error"] in text

    section_re = re.compile(cfg.section_start_pattern, re.M)
    matches = list(section_re.finditer(text))
    sections: dict[str, str] = {}
    if matches:
        out.section_found = True
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections[m.group("section")] = text[m.end():end]

    result_section = sections.get("result for vllm-ascend", "")
    if result_section:
        m = re.search(cfg.result_line_pattern, result_section, re.M)
        if m:
            out.result = m.group("result")
        out.findings, out.review_findings = _parse_findings(result_section, cfg)

    # Fallback break scan when the structured route yielded nothing useful.
    if not out.section_found or (out.result in (None, "BREAKS FOUND") and not out.findings):
        out.fallback_hits = _fallback_scan(text, cfg)

    out.included_in_table = (out.result == "BREAKS FOUND" and bool(out.findings))
    return out


def _parse_findings(result_section: str, cfg: AnalysisConfig
                    ) -> tuple[list[BreakFinding], list[BreakFinding]]:
    """Parse `### N.` entries, split by their `##` parent section:
    `## Breaks introduced by this PR` -> breaks; `## Items for review` -> review.
    Entries before any `##` section (preamble) are ignored."""
    header_re = re.compile(cfg.finding_header_pattern)
    field_res = {k: re.compile(v) for k, v in cfg.field_patterns.items()}
    lines = result_section.split("\n")

    findings: list[BreakFinding] = []
    review_findings: list[BreakFinding] = []
    current: BreakFinding | None = None
    raw_buf: list[str] = []
    current_section: str | None = None      # 'breaks' | 'review' | None

    def flush() -> None:
        nonlocal current, raw_buf
        if current is not None:
            current.raw_text = "\n".join(raw_buf).strip("\n")
            if current_section == "review":
                review_findings.append(current)
            else:
                findings.append(current)
        current = None
        raw_buf = []

    for line in lines:
        if line.startswith("## "):
            flush()
            if "Breaks introduced" in line:
                current_section = "breaks"
            elif "Items for review" in line:
                current_section = "review"
            else:
                current_section = None
            continue
        hm = header_re.match(line)
        if hm:
            flush()
            current = BreakFinding(
                index=int(hm.group("index")),
                priority=hm.group("priority"),
                relation=hm.group("relation"),
                contract_kind=hm.group("contract"),
            )
            raw_buf.append(line)
            continue
        if current is not None:
            matched = False
            for key, fre in field_res.items():
                fm = fre.match(line)
                if fm:
                    setattr(current, key, fm.group("value").strip())
                    matched = True
                    break
            raw_buf.append(line)
        # lines before the first header belong to the report preamble: ignored
    flush()
    return findings, review_findings


def _fallback_scan(text: str, cfg: AnalysisConfig) -> list[str]:
    """Last-resort regex scan for break-patterns with context lines."""
    lines = text.split("\n")
    pattern = re.compile("|".join(f"(?:{p})" for p in cfg.break_patterns), re.I)
    hits: list[str] = []
    seen: set[int] = set()
    n = cfg.context_lines
    for i, line in enumerate(lines):
        if pattern.search(line):
            lo, hi = max(0, i - n), min(len(lines), i + n + 1)
            if lo in seen:
                hi_prev = max(seen)
                if hi <= hi_prev:
                    continue
                lo = hi_prev + 1
            block = "\n".join(f"{j + 1}: {lines[j]}" for j in range(lo, hi))
            hits.append(block)
            seen.update(range(lo, hi))
    return hits
