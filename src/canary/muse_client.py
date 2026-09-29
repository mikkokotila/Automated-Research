"""Muse access through the external, persistent request boundary."""
from __future__ import annotations
import os
import time
from typing import Any
import httpx

from .spec import DEFAULT_MODEL, RunBudget  # re-exported; single locked value

BASE_URL = "https://api.meta.ai/v1"  # identity, not a worker destination
KEY_VARS = ("MUSE_API_KEY", "MODEL_API_KEY", "META_API_KEY")
TIMEOUT_S = 330
MAX_ATTEMPTS = 4
# Length-exhaustion escalation keeps its own cap: past the output ceiling a
# retry repeats the identical budget, so one same-budget retry stays enough.
LENGTH_ATTEMPTS = 3
BACKOFF_S = (2.0, 5.0, 15.0)
# Worker-owned ceiling for completion-budget escalation. The broker enforces
# its own per-call cap; this mirror only stops the worker asking for more.
MAX_OUTPUT_ESCALATION = 32768


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
                 client: Any | None = None, budget: RunBudget | None = None,
                 recorder: Any | None = None) -> None:
        if model != DEFAULT_MODEL:
            raise RequestBlocked("Only " + DEFAULT_MODEL + " is permitted")
        if api_key is not None:
            raise RequestBlocked("Provider credentials belong only in the trusted request service")
        self.calls = 0
        self.receipts: list[dict] = []
        self.budget = budget or RunBudget(25, 20_000_000, 1800)
        self.recorder = recorder
        self._url = os.environ.get("CANARY_GATE_URL", "http://canary-gate:8787").rstrip("/")
        self._token = os.environ.get("CANARY_GATE_TOKEN", "")
        if not self._token:
            raise RequestBlocked("CANARY_GATE_TOKEN required; direct provider access is disabled")
        self._client = client or httpx.Client(timeout=TIMEOUT_S, trust_env=False, follow_redirects=False)

    @property
    def model(self) -> str:
        return DEFAULT_MODEL

    def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        for attempt in range(MAX_ATTEMPTS):
            payload = {"model": DEFAULT_MODEL, "system": system, "user": user,
                       "max_tokens": max_tokens}
            self.budget.reserve_call()  # Cancelled/BudgetExhausted propagate distinctly
            self.calls += 1
            op = self.recorder.start("complete") if self.recorder else None
            try:
                response = self._client.post(self._url + "/v1/complete", json=payload,
                    headers={"Authorization": "Bearer " + self._token})
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS-1:
                    raise RequestBlocked("Request service unreachable; no direct fallback")
                time.sleep(BACKOFF_S[attempt])
                continue
            if self.recorder:  # a response arrived; its outcome is now knowable
                self.recorder.finish(op)
            try:
                body = response.json()
            except ValueError as exc:
                raise RequestBlocked("Invalid request-service response") from exc
            if response.status_code != 200:
                if body.get("error") == "provider_request_failed" and attempt < MAX_ATTEMPTS-1:
                    wait = BACKOFF_S[attempt]
                    hint = body.get("retry_after")
                    if type(hint) is int and 0 <= hint <= 60:
                        wait = hint  # server backoff, honored only for retryable failures
                    time.sleep(wait)
                    continue
                raise RequestBlocked(str(body.get("error", "request_blocked")) + ": " +
                                     str(body.get("message", "request denied")))
            if body.get("model") != DEFAULT_MODEL:
                raise RequestBlocked("Request-service model mismatch")
            usage = body.get("usage") or {}
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
                raise RequestBlocked("Request-service usage missing; cannot account")
            self.budget.note_usage(prompt, completion)
            self.receipts.append({**{k: body[k] for k in ("model", "usage", "receipt")},
                                  "finish_reason": body.get("finish_reason", "unknown")})
            text = body.get("text", "").strip()
            if text:
                return text
            if body.get("finish_reason") == "length" and attempt < LENGTH_ATTEMPTS - 1:
                # Reasoning burned the whole completion budget: escalate once
                # per attempt instead of dying (live Issue #49). No sleep:
                # this is under-provisioning, not rate pressure.
                max_tokens = min(max_tokens * 2, MAX_OUTPUT_ESCALATION)
                continue
            raise RuntimeError(f"Muse API returned empty content (finish={body.get('finish_reason', '?')}); "
                               f"completion limit={max_tokens}")
        raise RequestBlocked("Request attempts exhausted")

    def close(self) -> None:
        self._client.close()
