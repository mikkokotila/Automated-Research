# Build 05 validation record

Step: #6 — PR: #25
Code: `boundary/ledger.py` (run tokens + outcome log), `boundary/gateway.py`
(outcome recording + Retry-After), `boundary/server.py` (token auth +
`preflight`/`mint-token`/`revoke-token`), `src/canary/muse_client.py`
(finish preservation + backoff honoring), `scripts/container_run.sh`
(per-run mint/revoke), `tests/test_provider_boundary.py` (new, 15 tests),
`.env.example` (new), `docs/TOKEN_BOUNDARY.md` (rotation section).

## Evidence

- `pytest -q`: **160 passed, 2 deselected** — incl. token
  mint/use/revoke/expire/TTL/halt, outcome recording, 429/5xx/timeout/
  empty/truncation/cancel branches, redaction, backoff honoring, HTTP
  token auth, and preflight/mint/revoke CLI roundtrips.
- `scripts/container_verify.sh`: **43/43 true** — prior 37 plus
  per-run token scoping (distinct ids across runs), mint validity,
  revoke + expiry rejection, and honest `unknown` preflight.
- Live denial demo (fake key, disposable ledger, real endpoint): genuine
  provider HTTP 401 → `policy_blocked`, no retry, no fallback; preflight
  moved `unknown` → `blocked(provider_access_denied)`; status recorded
  `{kind: denied, status: 401}`. Demo fixtures fully removed.
- Fake-provider branch exercise: every broker error path also runs
  offline via `httpx.MockTransport` (no live spend anywhere in tests).

## Criteria status

- [x] Offline tests cover valid, 401/403, 429, 5xx, timeouts,
  empty/truncated, cancellation, and secret redaction.
- [x] Restriction yields persistent non-retryable halt (offline + live).
- [x] Worker holds only a short-lived run token: cannot read the account
  key, widen model/permissions (four-field validation), or proxy through
  the broker (endpoint + param allowlists).
- [x] Preflight records genuine success or explicit blocked/unknown
  prerequisite (exit 0 only when authorized). Live integration
  acceptance remains blocked: no legitimate credential exists yet.
- [x] Non-secret setup guide (`docs/TOKEN_BOUNDARY.md` incl. rotation)
  plus redacted `.env.example`.

## Notes

- No broker-side retries by design (each attempt is its own reservation);
  the worker honors provider `Retry-After` (capped 60s) only for
  retryable failures, with fixed backoff otherwise. Auth/contract
  failures never retry.
- Empty provider content stays an explicit worker-side error carrying
  the finish reason; truncation (`length`) is preserved end to end.
- Request identifiers (ledger receipt ids), usage, and finish status are
  preserved without logging prompts, credentials, or provider bodies
  (HTTP access logging is disabled; error strings carry no key material).
