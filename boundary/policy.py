"""Fixed limits owned by the trusted service, not worker configuration."""
ALLOWED_MODEL = "muse-spark-1.3-contributor"
TOKEN_LIMIT = 200_000_000
WINDOW_NS = 86_400 * 1_000_000_000
CONTEXT_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 32_768
MAX_BODY_BYTES = 1_000_000
PROVIDER_URL = "https://api.meta.ai/v1/chat/completions"

class BoundaryError(RuntimeError):
    code = "boundary_error"
    status = 503

class BudgetBlocked(BoundaryError):
    code = "token_budget_exhausted"
    status = 429

class PolicyBlocked(BoundaryError):
    code = "policy_blocked"
    status = 403

class StateBlocked(BoundaryError):
    code = "ledger_unavailable"


def validate_request(data):
    if not isinstance(data, dict) or set(data) != {"model", "system", "user", "max_tokens"}:
        raise PolicyBlocked("Only the four documented request fields are accepted")
    if data["model"] != ALLOWED_MODEL:
        raise PolicyBlocked("Only " + ALLOWED_MODEL + " is permitted")
    if type(data["max_tokens"]) is not int or not 1 <= data["max_tokens"] <= MAX_OUTPUT_TOKENS:
        raise PolicyBlocked("Invalid completion limit")
    for key in ("system", "user"):
        if not isinstance(data[key], str):
            raise PolicyBlocked("Only text requests are supported")
    if sum(len(data[k].encode("utf-8")) for k in ("system", "user")) > MAX_BODY_BYTES:
        raise PolicyBlocked("Request too large")
    # Reserve the entire documented input window, not an estimated tokenizer count.
    # The output limit includes reasoning. Deliberately conservative near the cap.
    return CONTEXT_TOKENS + data["max_tokens"]
