"""Shared fixtures: synthetic log texts matching the verified format (design 5.3.1)."""
import pytest

BREAKS_LOG = """
+++ vLLM PR compatibility inputs
vllm_pr_base_sha=aaa111
vllm_pr_head_sha=bbb222
vllm_ascend_sha=ccc333
+++ vLLM PR compatibility timings
[vllm-interface] load_contracts: 0.42s
[vllm-interface] range_analysis: 1.10s
+++ vLLM PR compatibility result for vllm-ascend
# vLLM PR Compatibility with vllm-ascend

**Result: BREAKS FOUND**

- This PR: `aaa111` -> `bbb222`
- vllm-ascend revision: `ccc333`
- Breaks introduced by this PR: 2
- Distinct vLLM API changes causing breaks: 2
- Items for review: 0
- Distinct vLLM API changes for review: 0

## Breaks introduced by this PR

### 1. P0 override/replacement_return
- vLLM API changed by this PR: `vllm/worker/worker_base.py:Worker.execute_model`
- Affected vllm-ascend code: `vllm_ascend/worker/worker_v1.py:123`
- vllm-ascend override path: `vllm_ascend/worker/worker_v1.py -> Worker.execute_model`
- Compatibility impact: This PR changes the return contract; the override now breaks.

### 2. P1 direct_call/positional_args
- vLLM API changed by this PR: `vllm/distributed/parallel_state.py:init_model_parallel`
- Affected vllm-ascend code: `vllm_ascend/distributed/parallel_state_v1.py:45`
- Compatibility impact: A new required parameter was added; positional call breaks.

## Items for review
(none)
"""

REVIEW_LOG = BREAKS_LOG.replace("**Result: BREAKS FOUND**", "**Result: REVIEW**")

PASS_LOG = BREAKS_LOG.replace("**Result: BREAKS FOUND**", "**Result: PASS**")

NO_SECTION_LOG = """
some docker build output
Successfully built abcdef
ERROR: pytest failed
Failed: this vLLM PR introduces an interface break in vllm-ascend
"""

ANALYZER_ERROR_LOG = BREAKS_LOG.replace(
    "Failed: this vLLM PR introduces an interface break in vllm-ascend",
    "").replace("**Result: BREAKS FOUND**", "**Result: PASS**") + """
interface analysis failed with exit code 2
"""

ANSI_LOG = BREAKS_LOG.replace("+++ vLLM PR compatibility result for vllm-ascend",
                              "\x1b[1m+++ vLLM PR compatibility result for vllm-ascend\x1b[0m")


@pytest.fixture
def breaks_log() -> str:
    return BREAKS_LOG


@pytest.fixture
def review_log() -> str:
    return REVIEW_LOG


@pytest.fixture
def pass_log() -> str:
    return PASS_LOG


@pytest.fixture
def no_section_log() -> str:
    return NO_SECTION_LOG


@pytest.fixture
def analyzer_error_log() -> str:
    return ANALYZER_ERROR_LOG


@pytest.fixture
def ansi_log() -> str:
    return ANSI_LOG
