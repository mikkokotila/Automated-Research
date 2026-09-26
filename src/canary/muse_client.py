"""Muse access through the external, persistent request boundary."""
from __future__ import annotations
import os
import time
from typing import Any
import httpx

BASE_URL = "https://api.meta.ai/v1"  # identity, not a worker destination
DEFAULT_MODEL = "muse-spark-1.3-contributor"
KEY_VARS = ("MUSE_API_KEY", "MODEL_API_KEY", "META_API_KEY")
TIMEOUT_S = 330
MAX_ATTEMPTS = 3
BACKOFF_S = (2.0, 5.0)


class RequestBlocked(RuntimeError):
    """A hard boundary refusal, not evidence of research convergence."""


def resolve_api_key(env: dict | None = None) -> str:
    """Operator-tool credential lookup; not used by the worker client."""
    env = env if env is not None else os.environ
    for var in KEY_VARS:
        if env.get(var):
            return env[var]
    raise RuntimeError("no Muse API key found; set MUSE_API_KEY")


class MuseClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 client: Any | None = None) -> None:
        if model != DEFAULT_MODEL:
            raise RequestBlocked("Only " + DEFAULT_MODEL + " is permitted")
        if api_key is not None:
            raise RequestBlocked("Provider credentials belong only in the trusted request service")
        self.calls = 0
        self.receipts: list[dict] = []
        self._url = os.environ.get("CANARY_GATE_URL", "http://canary-gate:8787").rstrip("/")
        self._token = os.environ.get("CANARY_GATE_TOKEN", "")
        if not self._token:
            raise RequestBlocked("CANARY_GATE_TOKEN required; direct provider access is disabled")
        self._client = client or httpx.Client(timeout=TIMEOUT_S, trust_env=False, follow_redirects=False)

    @property
    def model(self) -> str:
        return DEFAULT_MODEL

    def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        payload = {"model": DEFAULT_MODEL, "system": system, "user": user, "max_tokens": max_tokens}
        for attempt in range(MAX_ATTEMPTS):
            cap = int(os.environ.get("MUSE_MAX_CALLS", "0") or 0)
            if cap > 0 and self.calls >= cap:
                raise RequestBlocked(f"MUSE_MAX_CALLS budget exhausted ({cap} calls)")
            self.calls += 1
            try:
                response = self._client.post(self._url + "/v1/complete", json=payload,
                    headers={"Authorization": "Bearer " + self._token})
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS-1:
                    raise RequestBlocked("Request service unreachable; no direct fallback")
                time.sleep(BACKOFF_S[attempt])
                continue
            try:
                body = response.json()
            except ValueError as exc:
                raise RequestBlocked("Invalid request-service response") from exc
            if response.status_code != 200:
                if body.get("error") == "provider_request_failed" and attempt < MAX_ATTEMPTS-1:
                    time.sleep(BACKOFF_S[attempt])
                    continue
                raise RequestBlocked(str(body.get("error", "request_blocked")) + ": " +
                                     str(body.get("message", "request denied")))
            if body.get("model") != DEFAULT_MODEL:
                raise RequestBlocked("Request-service model mismatch")
            self.receipts.append({k:body[k] for k in ("model", "usage", "receipt")})
            text = body.get("text", "").strip()
            if text:
                return text
            raise RuntimeError(f"Muse API returned empty content (finish={body.get('finish_reason', '?')}); "
                               f"completion limit={max_tokens}")
        raise RequestBlocked("Request attempts exhausted")

    def close(self) -> None:
        self._client.close()
