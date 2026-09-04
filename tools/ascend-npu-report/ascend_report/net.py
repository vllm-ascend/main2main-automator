"""HTTP helpers shared by all API clients (retry / backoff / rate-limit)."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    max_attempts: int = 5,
    backoff_base: float = 2.0,
    **kwargs,
) -> httpx.Response:
    """GET with exponential backoff. 401/403 raise immediately (token problems)."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.get(url, **kwargs)
        except httpx.HTTPError as exc:  # network errors are retryable
            last_exc = exc
            resp = None
        if resp is not None and resp.status_code not in RETRYABLE_STATUS:
            return resp
        if attempt == max_attempts:
            break
        delay = backoff_base**attempt
        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        log.warning("GET %s attempt %d/%d failed (%s), retrying in %.1fs",
                    url, attempt, max_attempts,
                    resp.status_code if resp is not None else type(last_exc).__name__, delay)
        time.sleep(delay)
    if resp is not None:
        resp.raise_for_status()
    raise last_exc  # type: ignore[misc]
