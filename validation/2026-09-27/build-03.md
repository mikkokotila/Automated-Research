# Build 03 validation record

Step: #4 — PR: #23
CI: https://github.com/mikkokotila/Canary/actions/runs/36329266458
(smoke 16s, matrix 20-23s, docker 1m59s with the extended 27-check verify)
Code: hardened `scripts/container_run.sh` + `scripts/verify_boundary.py`
launcher checks + `scripts/make_fixture_wheel.py` + `tests/test_launcher_support.py`.

## What was built

- Trusted launcher with externally enforced limits: internal-only network
  (override-guarded, `host` refused, `Internal=true` asserted), non-root
  user, dropped caps, read-only rootfs, CPU/memory/pids/tmpfs caps,
  portable wall-time watchdog (`CANARY_TIMEOUT_S`, stop + trap cleanup),
  and a per-run receipt (`<out>.receipt.json`: image digest, config,
  outcome, git rev).
- Approved-source snapshot: image built from the checkout; dirty trees
  refused unless the host-side operator override `ALLOW_DIRTY=1` is set
  (logged in the receipt as `operator-override`).
- Supported installation without guest egress: operator-staged read-only
  wheelhouse (`CANARY_WHEELS`) + `pip install --no-index --find-links`.
- Explicitly selected inputs staged read-only at `/inputs` (`CANARY_STAGE`,
  `https://` via capped host curl or host-file copy; last-colon split,
  bare-filename validation).
- `container-out/` added to `.gitignore`; CI smoke gains a `bash -n` +
  `py_compile` script gate.

## Evidence (local, fixture credentials only)

- `scripts/container_verify.sh`: **27/27 true** — 12 pre-existing boundary
  checks plus 15 launcher checks (receipt, wheel install, staged input,
  image immutability, workspace independence, metadata + public-DNS
  denial, timeout kill + resource release, host/bridge refusal, colon
  split, unreachable-stage refusal, no-import tripwire).
- `pytest -q`: **138 passed, 2 deselected** (incl. offline wheel test and
  the updated launcher tripwire in `test_budget.py`).
- Direct demo: staged live `https://example.com` (559 bytes, host-side
  curl), guest installed the fixture wheel (`answer=42`) and wrote
  `result.json`; receipt recorded digest + `operator-override`; demo
  gate/network/volumes removed afterwards with zero `canary-*` resources
  left (`docker ps/volume/network` filters empty).

## Explicitly open (kept, per completion contract)

- Acceptance "Public HTTP(S), DNS ... work" from the guest is **denied by
  design**, not implemented: direct worker egress stays off so model
  selection remains enforceable (see `docs/TRUST_BOUNDARY.md` network
  decision). Public content reaches guests via staged inputs and the two
  brokered literature routes. A brokered fetch/PyPI proxy is named future
  work if the owner wants guest-originated retrieval; it needs its own
  SSRF review and is not smuggled in here.
- "Host CLI entries hand work to the launcher": host `canary` runs only
  first-party code (revision refuses per Build 02); contained execution
  goes through `container_run.sh`. No CLI re-plumbing was needed.
