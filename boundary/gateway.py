"""Only this service can send an authenticated provider request."""
import json
import httpx
from .policy import (ALLOWED_MODEL, PROVIDER_URL, CONTEXT_TOKENS,
                     BoundaryError, PolicyBlocked, StateBlocked, validate_request)


class ProviderFailure(BoundaryError):
    code = "provider_request_failed"
    status = 502


class Gateway:
    def __init__(self, ledger, api_key, transport=None):
        if not api_key:
            raise StateBlocked("A fresh provider credential is required")
        self.ledger = ledger
        self._key = api_key
        # No SDK retries, redirects, alternate URLs, or environment proxy settings.
        self.http = httpx.Client(timeout=300, trust_env=False, follow_redirects=False,
                                 transport=transport)

    def complete(self, data):
        reserved = validate_request(data)
        ident = self.ledger.reserve(reserved)  # committed BEFORE touching the network
        try:
            response = self.http.post(PROVIDER_URL,
                headers={"Authorization": "Bearer " + self._key},
                json={"model": ALLOWED_MODEL, "messages": [
                    {"role": "system", "content": data["system"]},
                    {"role": "user", "content": data["user"]}],
                    "max_completion_tokens": data["max_tokens"], "stream": False, "n": 1})
        except httpx.HTTPError as exc:
            # No known completion time or usage: retain the full reservation forever.
            raise ProviderFailure("Provider transport failed; reservation retained") from exc
        if response.status_code in (401, 403):
            self.ledger.halt("provider_access_denied")
            raise PolicyBlocked("Provider access denied; service halted, no fallback")
        if response.status_code != 200:
            raise ProviderFailure("Provider request failed; reservation retained")
        try:
            value = response.json()
            if value["model"] != ALLOWED_MODEL:
                raise ValueError("unexpected model")
            usage = value["usage"]
            p, c, total = [usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")]
            if any(type(n) is not int or n < 0 for n in (p, c, total)):
                raise ValueError("invalid usage")
            if p == 0 or p > CONTEXT_TOKENS or c > data["max_tokens"] or total != p+c or total > reserved:
                raise ValueError("invalid accounting")
            # Cached input and reasoning are subsets, not discounted token counts.
            for field, detail, maximum in (("prompt_tokens_details", "cached_tokens", p),
                                          ("completion_tokens_details", "reasoning_tokens", c)):
                details = usage.get(field) or {}
                n = details.get(detail, 0)
                if type(n) is not int or not 0 <= n <= maximum:
                    raise ValueError("invalid usage detail")
            choices = value["choices"]
            if len(choices) != 1:
                raise ValueError("unexpected choices")
            text = choices[0]["message"]["content"]
            if text is not None and not isinstance(text, str):
                raise ValueError("invalid content")
            if text and c == 0:
                raise ValueError("visible output with zero completion tokens")
            finish = choices[0].get("finish_reason", "unknown")
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            self.ledger.halt("invalid_provider_accounting_or_model")
            raise StateBlocked("Provider contract mismatch; full reservation retained") from exc
        self.ledger.settle(ident, total, json.dumps(usage, sort_keys=True))
        return {"model": ALLOWED_MODEL, "text": (text or "").strip(), "finish_reason": finish,
                "usage": usage, "receipt": ident}
