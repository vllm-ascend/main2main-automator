import json
import os
from datetime import date, datetime, timezone

import httpx

from ascend_report.config import load_config
from ascend_report.dashboard import DashboardClient, parse_build_number
from ascend_report.store import Store


def test_parse_build_number():
    url = "https://buildkite.com/vllm/ci/builds/85905?jid=01a045bf-c9e7-4c4f-bbb9-87930bb82b79"
    assert parse_build_number(url) == 85905
    assert parse_build_number("") is None
    assert parse_build_number(None) is None
    assert parse_build_number("https://example.com/other") is None


def test_dashboard_fetch_filters_failures():
    payload = {"runs": [
        {"job_id": "j1", "web_url": "https://buildkite.com/vllm/ci/builds/10?jid=j1",
         "state": "failed", "started_at": None, "finished_at": None,
         "duration_secs": 60, "commit_sha": "abc", "build_created_at": None},
        {"job_id": "j2", "web_url": "https://buildkite.com/vllm/ci/builds/11?jid=j2",
         "state": "passed", "started_at": None, "finished_at": None,
         "duration_secs": 60, "commit_sha": "abd", "build_created_at": None},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "jobName=Ascend+NPU+Test" in str(request.url) or \
               "jobName=Ascend%20NPU%20Test" in str(request.url)
        assert "pipeline=CI" in str(request.url)
        return httpx.Response(200, json=payload)

    client = DashboardClient(load_config(None).dashboard, load_config(None).job,
                             client=httpx.Client(
                                 base_url="https://ci.vllm.ai",
                                 transport=httpx.MockTransport(handler)))
    failed = client.fetch_failed_runs(date(2026, 9, 3), date(2026, 9, 4),
                                      ["failed", "soft_failed"])
    assert [r.job_id for r in failed] == ["j1"]
    assert failed[0].build_number == 10
    assert failed[0].commit_sha == "abc"


def test_store_idempotency_and_audit(tmp_path):
    store = Store(tmp_path / "store")
    assert not store.is_processed("job-1")
    store.mark_processed("job-1", 100, "failed", "dashboard", "2026-09-04")
    assert store.is_processed("job-1")
    store.mark_processed("job-1", 100, "failed", "both", "2026-09-04")  # idempotent

    store.start_run("run-1", "2026-09-03T00:00:00+00:00", "2026-09-04T00:00:00+00:00")
    assert store.last_successful_run_end_parsed() is None  # not finished yet
    store.finish_run("run-1", ok=True, runs_scanned=5, failures=2, findings=3)
    last = store.last_successful_run_end_parsed()
    assert last is not None

    snap = store.save_log_snapshot("2026-09-04", 100, "job-1", "log body")
    assert snap.exists() and snap.read_text(encoding="utf-8") == "log body"
    store.close()


def test_cross_check_url_and_match(tmp_path):
    """Buildkite builds list -> match job by step_key (design 5.1)."""
    from ascend_report.buildkite import BuildkiteClient
    cfg = load_config(None)
    build_payload = [{
        "number": 87028, "branch": "main", "message": "[Bugfix] something (#22331)",
        "web_url": "https://buildkite.com/vllm/ci/builds/87028",
        "jobs": [{"id": "01a065ff-efc4-425c-b9c6-cb2e6f92fafe",
                  "name": "Ascend NPU Test", "step_key": "ascend-npu-test",
                  "state": "failed",
                  "web_url": "https://buildkite.com/vllm/ci/builds/87028#j"}],
    }]
    seen: dict = {}
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        seen["auth"] = request.headers.get("Authorization")
        # only the state=failed request carries the fixture build
        payload = build_payload if "state=failed" in str(request.url) else []
        return httpx.Response(200, json=payload)

    import os
    os.environ["BUILDKITE_API_TOKEN"] = "test-token"
    client = BuildkiteClient(cfg.buildkite, cfg.job,
                             client=httpx.Client(base_url="https://api.buildkite.com",
                                                 transport=httpx.MockTransport(handler)))
    jobs = client.scan_failed_jobs(
        datetime(2026, 9, 3, tzinfo=timezone.utc), ["failed", "failing"])
    # state is a single value per request (API rejects comma lists with 422)
    assert any("state=failed" in u for u in urls)
    assert any("state=failing" in u for u in urls)
    assert not any("%2C" in u or "%2c" in u for u in urls)
    assert seen["auth"] == "Bearer test-token"
    assert len(jobs) == 1
    assert jobs[0].job_id == "01a065ff-efc4-425c-b9c6-cb2e6f92fafe"
    assert jobs[0].build_number == 87028
