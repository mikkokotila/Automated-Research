# Build 04 validation record

Step: #5 — PR: #24
Code: `scripts/export_bundle.py` (manifest + reserved name + collision
contract), `scripts/scan_export.py` (new), `tests/test_export.py` (new),
`scripts/verify_boundary.py` (containment section + `--report`),
`scripts/container_verify.sh` (`--report` wiring), `ci.yml` (weekly
schedule + scheduled-run artifact).

## Evidence (local, fixture credentials only)

- `scripts/container_verify.sh`: **37/37 true** — 27 prior checks plus
  PID-namespace smallness, cgroup lockdown, no docker CLI, private/
  link-local IPv4 + IPv6 denials (10/8, 192.168/16, fe80::/10),
  pids-limit enforcement (fork bomb stopped), egress-probe negative
  control (unsafe worker correctly detected open), scanner end-to-end
  on a real export, and source-checkout-untouched sentinel.
- `pytest -q`: **145 passed, 2 deselected** (7 new export/scanner tests).
- Machine-readable report: `verify_boundary.py --report` writes the same
  JSON CI prints; the weekly scheduled CI run uploads it as an artifact.
- Malicious export cases (offline + CI): traversal, absolute, symlink,
  hardlink, char/block/fifo devices, oversized, collisions (normalized
  to `ValueError`), reserved manifest name — all rejected. Benign nested
  trees export with sha256 manifests.
- Maintainer scan: planted `ghp_fixture_*` in a real guest export is
  flagged by `scan_export.py`; manifest tampering detected; clean
  bundles pass.
- Legitimate access demo: contained worker fetched **live OpenAlex data**
  through the broker (`perovskite solar cells`, 2 works incl. "The
  emergence of perovskite solar cells"), exported and scanned clean.
  Demo gate/networks/volumes fully removed afterwards (zero `canary-*`
  resources left).
- No unrelated host/LAN probing: every network assertion targets owned
  disposable fixtures or well-known unroutable/test destinations from
  inside the disposable guest.

## Criteria status

- [x] Suite runs on the supported runtime; per-capability pass/fail plus
  image/config evidence recorded outside the worker (report artifact).
- [x] Unsafe configs rejected (host/bridge); negative control proves the
  egress probe detects a genuinely open worker.
- [x] Sentinels unchanged (checkout diff sentinel); probes confined to
  owned fixtures.
- [x] Malicious exports rejected; benign exports carry checksums; scanner
  gates maintainer import.
- [x] Missing runtime/failed containment blocks revision: revision stays
  unconditionally refused (Build 02 gate); the future attestation hook
  will consume this suite's report. Verify exits 1 on any false check.

## Deliberately not done here

- No live-external calls in CI (flakiness); positive brokered paths are
  proven in the operator-run demo above and by recorded offline fixtures.
- No memory/disk exhaustion fills (mean to runners); bounded by
  construction via cgroup/tmpfs caps plus the enforced pids proof.
- No extension allowlist on exports: type restrictions would be theater
  (any inert type can carry text); the controls are bounded copy +
  never-execute + scan-before-import.
