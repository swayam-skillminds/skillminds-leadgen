"""JSON over HTTP for the providers that have no SDK here (OpenAI, Gemini, compatible).

One POST helper maps status codes to the provider errors of ``base``:

* 401 / 403                       -> ``AuthFailed``
* 402, or a quota/credit message  -> ``QuotaExhausted``
* 429 (after retries)             -> ``QuotaExhausted``
* 5xx, timeouts, connection       -> ``ProviderError`` (retried first)
* other 4xx                       -> ``ProviderError`` (not retried: the request is wrong)

Tests mount a fake transport adapter on the session (``tests/ai_fakes.py``), so
the real request building and response parsing run with no network.
"""

from __future__ import annotations

from typing import Any

import requests

from .base import AuthFailed, Provider, ProviderError, QuotaExhausted, describe, retry_after

#: Words in an error body that mean "out of money", whatever the status.
_QUOTA_WORDS = ("insufficient_quota", "credit balance", "billing", "exceeded your current quota")


class _Retryable(ProviderError):
    def __init__(self, message: str, delay: float, final: ProviderError):
        super().__init__(message)
        self.delay = delay
        self.final = final


def session() -> requests.Session:
    return requests.Session()


def post_json(provider: Provider, http: requests.Session, url: str, *,
              headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    """POST `body`, return the JSON answer, or raise a provider error."""

    def send() -> dict[str, Any]:
        try:
            response = http.post(url, json=body, headers=headers, timeout=provider.spec.timeout)
        except requests.Timeout as exc:
            raise _Retryable("timed out", 2.0, ProviderError(f"timed out: {exc!r}"[:200])) from exc
        except requests.RequestException as exc:
            raise _Retryable("connection failed", 2.0,
                             ProviderError(f"connection failed: {exc!r}"[:200])) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:300]
        status = response.status_code
        if status < 400:
            if not isinstance(payload, dict):
                raise ProviderError(f"HTTP {status}: not a JSON object")
            return payload
        text = describe(status, payload)
        lowered = str(payload).lower()
        if status in (401, 403):
            raise AuthFailed(text)
        out_of_money = any(word in lowered for word in _QUOTA_WORDS)
        if status == 402 or (status == 400 and out_of_money) or (
                status == 429 and ("insufficient_quota" in lowered or "credit" in lowered
                                   or "billing" in lowered)):
            raise QuotaExhausted(text)
        if status == 429:                      # a rate limit: wait and retry first
            raise _Retryable(text, retry_after(response.headers), QuotaExhausted(text))
        if status >= 500:
            raise _Retryable(text, retry_after(response.headers), ProviderError(text))
        raise ProviderError(text)

    try:
        return provider._with_retries(
            send, lambda exc: exc.delay if isinstance(exc, _Retryable) else None)
    except _Retryable as exc:
        raise exc.final from None
