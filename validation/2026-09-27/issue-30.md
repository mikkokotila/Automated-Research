# 2026-09-27 Offline network guard (Issue #30)

**Revision under test:** branch `fix-30-offline-guard`
**Owner contract compliance:** test-only change; no revision paths
touched; synthetic fixtures only; no credentials; recovery evidence
untouched; no archived commands replayed.

## What changed

- `tests/conftest.py`: new autouse `offline_guard` fixture. Tests
  marked `live` opt out explicitly; every other test may only touch
  loopback (127/8, ::1, "localhost") or AF_UNIX paths. Non-loopback
  `connect`/`connect_ex` and external `getaddrinfo` raise
  `OfflineGuardError`. `connect` is the enforcement backstop: even a
  resolved name cannot reach the wire.
- `tests/test_offline_fixtures.py`: four contract tests — blocked
  connect/connect_ex (TEST-NET-1, unroutable), blocked external DNS,
  blocked real `httpx.Client`, loopback passthrough (closed-port
  `ConnectionRefusedError` proves the real socket ran; localhost DNS
  still resolves).

## Why this shape

The two legitimate real-socket uses in the suite
(`test_run_token_auth_over_http`, `test_http_auth_duplicates...`)
both hit 127.0.0.1 test servers, so a loopback exemption keeps them
green while any future live-provider leak fails closed. `MockTransport`
clients never touch sockets and are unaffected.

## Checks run

```
$ uv run --no-sync pytest tests/test_offline_fixtures.py -q
8 passed, 1 deselected
$ uv run --no-sync pytest -q -m "not live" --deselect tests/test_budget.py::test_processes_share_one_ledger
442 passed, 2 deselected in 63.48s
$ ./scripts/container_verify.sh
43 true, 0 false/null
```

## Findings fixed on the branch

- First run: guard fired correctly but `pytest.raises` did not catch —
  dual module identity (`conftest` vs `tests.conftest`). Test now
  imports `OfflineGuardError` from `conftest`, matching the fixture.

## Disposition

Ready for PR. Closes #30 on merge: the systemic gap (a future
offline test reaching the network and passing by luck) is now a loud
failure.
