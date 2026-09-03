"""Shared retry policy for outbound search providers.

All providers hit the same class of failure (rate limits, upstream 5xx, network
blips). Previously only `google_search.py` had a retry policy; the other three
(Brave, Tavily, Google Maps) failed immediately on the first transient error.
This module gives every provider one identical, centrally-tuned policy.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRYABLE_WAIT = wait_exponential(multiplier=1, min=2, max=10)


def is_retryable_error(exc: BaseException) -> bool:
    """Classify whether a search-provider error is worth retrying.

    429 (rate limited) and >=500 (upstream fault) are retried; 4xx client
    errors are not, because the caller's request is not going to suddenly
    become valid. Transport-level errors (DNS, connection reset, timeout) are
    inherently transient and retried.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    if isinstance(exc, (httpx.RequestError, httpx.TimeoutException)):
        return True
    return False


def retry_search(func):
    """Tenacity retry decorator tuned for search providers."""
    return retry(
        retry=retry_if_exception(is_retryable_error),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=RETRYABLE_WAIT,
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)
