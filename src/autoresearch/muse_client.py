"""Muse API client (Meta Model API, OpenAI-compatible)."""

from __future__ import annotations

import os
import time
from typing import Any

BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3-contributor"
KEY_VARS = ("MUSE_API_KEY", "MODEL_API_KEY", "META_API_KEY")
TIMEOUT_S = 300
MAX_ATTEMPTS = 3
BACKOFF_S = (2.0, 5.0)


def _retryable(exc: Exception) -> bool:
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or 500 <= status <= 599)


def resolve_api_key(env: dict | None = None) -> str:
    env = env if env is not None else os.environ
    for var in KEY_VARS:
        if env.get(var):
            return env[var]
    raise RuntimeError(
        "no Muse API key found; set MUSE_API_KEY (also accepts MODEL_API_KEY or META_API_KEY)"
    )


class MuseClient:
    """Thin wrapper over the OpenAI SDK pointed at the Muse API."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, client: Any | None = None) -> None:
        from openai import OpenAI

        self.model = model
        self.calls = 0
        self._client = client or OpenAI(api_key=api_key or resolve_api_key(), base_url=BASE_URL, timeout=TIMEOUT_S)

    def _spend(self) -> None:
        cap = int(os.environ.get("MUSE_MAX_CALLS", "0") or 0)
        if cap > 0 and self.calls >= cap:
            raise RuntimeError(f"MUSE_MAX_CALLS budget exhausted ({cap} calls)")
        self.calls += 1

    def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        # Contributor models reason before answering; the budget must cover both.
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                self._spend()
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=max_tokens,
                )
            except Exception as e:
                last = e
                if _retryable(e) and attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                    continue
                raise
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
            finish = getattr(resp.choices[0], "finish_reason", "?")
            raise RuntimeError(f"Muse API returned empty content (finish={finish}); reasoning may have exhausted max_tokens={max_tokens}")
        raise RuntimeError(f"Muse API failed after {MAX_ATTEMPTS} attempts: {last}")
