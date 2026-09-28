# 2026-09-27 Ops testing tracker

**Revision under test:** `main` @ `7d213fe` (post #41) + uncommitted tracker only
**Method:** operator-driven black-box probes against the real CLI, plus a
throwaway loopback broker (`/tmp/ops/fake_broker.py`, since stopped) serving
canned completions and the recorded source payloads. All run artefacts in
`/tmp/ops` (outside the repo). Synthetic data only; no credentials present
beyond the fake `ops-fake-token` on loopback.
**Contract compliance:** no revision-enabled work outside the hermetic suite;
no prod credentials; recovery evidence untouched; zero `canary-*` Docker
leftovers and a clean tree at close (verified).

## End-state gates (all green)

- Full suite: `442 passed, 2 deselected` (offline guard active).
- Boundary suite: `43/43` (`./scripts/container_verify.sh`).
- Offline acceptance: `10/10` (`scripts/run_acceptance.py`, exit 0).

## Probe log

| # | Probe | Verdict |
|---|-------|---------|
| 1 | `review` without creds | PASS — one-line error, exit 1 |
| 2 | `revise` on uncontained host | PASS — refused (creds layer first); `require_revision_trust` also probed directly: `ContainmentBlocked` |
| 3 | `analyze` on synthetic CSV without creds | PASS — local stats computed and written, model step fails closed, exit 1 |
| 4 | Full `cycle` (review→analyze→review) vs fake broker | PASS — exit 0, converged, claim c1 anchored+supported, over-cited c2 rejected and downgraded, coverage warnings + dangling citations journaled, assessments written, bundle sealed |
| 5 | SIGKILL whole tree mid-cycle, then `resume` | PASS — killed at iter 2/4, no done/seal; resume completed with `continued` + `ambiguous-op` events, no duplicate iterations, single done/seal, usage accounted |
| 6 | `resume` on sealed bundle | PASS — exit 2 with `--fork` hint |
| 7 | `resume --fork` | PASS — fork completed, original journal untouched |
| 8 | `inspect` | PASS — exit 0, JSON status (`converged`, `interrupted` both observed) |
| 9 | Wrong gate token | PASS (fail closed) — exit 1; message verbose but leaks no secret (observation O1) |
| 10 | Broker down during `review` | PASS (fail closed) — exit 1, compact error; partial bundle has journal+manifest — but no failure event journaled (**F3**) |
| 11 | `inspect` on corrupt-checkpoint / tampered-journal bundles | OBSERVED — still reports `converged`; integrity is export-time, not inspect-time (by design; see probe 13) |
| 12 | Two concurrent cycles | PASS — both exit 0, distinct run_ids, no crosstalk |
| 13 | Export scanner: planted sub-cap secret | PASS — flagged `muse_key`, exit 1 |
| 14 | Export scanner: post-export byte tamper | PASS — checksum mismatch, `clean: false` |
| 15 | Export scanner: 5.2MB secret-bearing file | **FAIL — F2**: `{clean: true, skipped: 1}`, exit 0 |
| 16 | Export scanner: symlink to secret file | **FAIL — F2**: `{clean: true}`, exit 0, link silently ignored |
| 17 | `cycle --max-calls 2` | PASS — `budget_exhausted`, exactly 2 calls, exit 0 with synthesis |
| 18 | Bandit `learning` then `frozen` | PASS — policy + observations written; frozen byte-identical (sha256 before/after equal) |
| 19 | Bandit `frozen` with corrupt snapshot | PASS — exit 0, `degraded: true` + fallback reason in `bandit.jsonl`, `fallback` event in journal |
| 20 | `publish` with no recorded rounds | OBSERVED — exit 1 with raw `FileNotFoundError` + internal path (observation O2) |
| 21 | `export_bundle.py --help` | **FAIL — F1**: created `./--help/` (0700), then tarfile traceback; poisoned the Docker build context and turned the whole 43-check suite red until removed |
| 22 | Full offline acceptance after cleanup | PASS — 10/10, exit 0 |

## Filed findings

- **F1 — #42 (medium):** `export_bundle.py` side-effects on invalid invocation;
  stray dir poisoned the guest via `COPY . /app`. Red→green verified both ways.
- **F2 — #43 (low-medium):** `scan_export` reports `clean`/exit 0 with skipped
  oversize files; symlinks skipped silently. Controls (sub-cap secret, tamper)
  pass, so the gate is half-open, not absent.
- **F3 — #44 (low):** clean CLI failures journal no failure event; crash and
  clean refusal are indistinguishable post-hoc.

## Observations (tracker only, no issue)

- **O1:** wrong-token 403 error embeds two full URLs + MDN links. No secret
  leaks (token travels in the header). Cosmetic.
- **O2:** `publish` with no rounds surfaces a raw `FileNotFoundError` with a
  relative internal path. A one-line "no recorded rounds" refusal would match
  the CLI's error contract.
- **Positive:** partial `analyze` bundle on creds failure preserves real local
  results (analysis.jsonl valid); kill/resume honesty (`ambiguous-op`) and
  bandit loud-degradation trails are exactly as designed.

## Residual risks (unchanged, for the record)

- No live provider, broker, or gate credentials exist anywhere in this
  environment; all model/broker behavior above is fake-broker mechanics, not
  evidence of live performance. Live acceptance remains owner-blocked (#39).
- Redaction/scanning is best-effort prefix matching (see F2); no cost claims.
- Shared-kernel escapes, daemon compromise, broker bugs, operator error remain
  out of scope of this rig.

## Disposition

Tracker + #42/#43/#44 filed. Pre-existing opens: #1 (roadmap), #4 (Build 03
sign-off), #39 (owner-blocked live). Fixes for F1–F3 are queued, not started.
