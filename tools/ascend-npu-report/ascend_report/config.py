"""Configuration loading: config.yaml overlaid on pydantic model defaults."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class DashboardConfig(BaseModel):
    base_url: str = "https://ci.vllm.ai"
    runs_endpoint: str = "/api/jobs/runs"
    pipeline: str = "CI"
    branch: str = "main"


class BuildkiteConfig(BaseModel):
    api_token_env: str = "BUILDKITE_API_TOKEN"
    org: str = "vllm"
    pipeline_slug: str = "ci"
    branch: str = "main"
    cross_check: bool = True
    cross_check_states: list[str] = Field(default_factory=lambda: ["failed", "failing"])


class JobConfig(BaseModel):
    name: str = "Ascend NPU Test"
    step_key: str = "ascend-npu-test"
    failure_states: list[str] = Field(
        default_factory=lambda: ["failed", "soft_failed", "timed_out", "broken", "failing"]
    )


class AnalysisConfig(BaseModel):
    section_start_pattern: str = (
        r"^\+\+\+ vLLM PR compatibility "
        r"(?P<section>inputs|timings|result for vllm-ascend)\s*$"
    )
    result_line_pattern: str = r"^\*\*Result: (?P<result>BREAKS FOUND|REVIEW|PASS)\*\*\s*$"
    finding_header_pattern: str = (
        r"^### (?P<index>\d+)\. (?P<priority>\S+) "
        r"(?P<relation>[^/\s]+)\s*/\s*(?P<contract>\S+)\s*$"
    )
    field_patterns: dict[str, str] = Field(default_factory=lambda: {
        "vllm_api": r"^\s*- vLLM API changed by this PR: `(?P<value>.+)`\s*$",
        "affected_code": r"^\s*- Affected vllm-ascend code: `(?P<value>.+)`\s*$",
        "impact": r"^\s*- Compatibility impact: (?P<value>.+)$",
        "override_path": r"^\s*- vllm-ascend override path: `(?P<value>.+)`\s*$",
        "review_reason": r"^\s*- Review reason: (?P<value>.+)$",
    })
    pytest_fail_patterns: dict[str, str] = Field(default_factory=lambda: {
        "real_break": "this vLLM PR introduces an interface break in vllm-ascend",
        "analyzer_error": "interface analysis failed with exit code",
    })
    break_patterns: list[str] = Field(default_factory=lambda: [r"\bbreak\w*\b"])
    context_lines: int = 2


class GithubConfig(BaseModel):
    # 专用名，避免与 Actions runner 自身的 GITHUB_TOKEN 混淆/覆盖
    token_env: str = "GITHUB_PR_TOKEN"
    repo: str = "vllm-project/vllm"


class ReportConfig(BaseModel):
    output_dir: str = "reports"
    # {window} = UTC window identity, e.g. 20260903T0000Z-20260904T0000Z
    filename: str = "report-{window}.md"
    table_inclusion: str = "breaks_only"
    write_empty_report: bool = True


class StoreConfig(BaseModel):
    dir: str = "store"


class ScheduleConfig(BaseModel):
    lookback_default_hours: int = 24


class AppConfig(BaseModel):
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    buildkite: BuildkiteConfig = Field(default_factory=BuildkiteConfig)
    job: JobConfig = Field(default_factory=JobConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load YAML config over model defaults. Missing file -> pure defaults."""
    data: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = AppConfig(**data)
    # Anchor relative output paths to the tool directory so results land in
    # tools/ascend-npu-report/ regardless of the process working directory.
    tool_root = Path(__file__).resolve().parent.parent
    if not Path(cfg.report.output_dir).is_absolute():
        cfg.report.output_dir = str(tool_root / cfg.report.output_dir)
    if not Path(cfg.store.dir).is_absolute():
        cfg.store.dir = str(tool_root / cfg.store.dir)
    return cfg
