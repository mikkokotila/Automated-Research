# 2026-09-27 Build 17: Acceptance and Handoff (Issue #18)

**Revision under test:** branch `build-17-acceptance`
**Owner contract compliance:** no revision-enabled work outside the hermetic
suite (acceptance asserts the refusal on uncontained hosts); synthetic data
only; no prod credentials (none present in the environment); recovery
evidence untouched; no archived commands replayed.

## What changed

- New `scripts/run_acceptance.py`: ten deterministic offline scenarios run
  from a clean checkout with no keys and no network — review-only research,
  synthetic-CSV analysis, a full cycle, maintenance refusal, hermetic
  revision-logic tests (91 passed), crash/resume, budget exhaustion,
  provider restriction, container boundaries (43/43 or honest skip without
  Docker), and export + secret scan. Seals `verdicts.jsonl`,
  `manifest.json`, and `ACCEPTANCE.md`; exit 0 only when all pass.
- New `docs/RUNBOOK.md`: setup, offline acceptance, trusted launch,
  preflight, finite run spec, status, stop/resume, export, credential
  setup/rotation, data-exposure policy, troubleshooting, limitations,
  backlog. CLI-first, no machine-specific paths.
- New `docs/WORKERS.md`: worker contract (scope, budgets, restarts,
  journaling, research quality, memory, promotion, failure posture).
- `acceptance/2026-09-27/`: dated release bundle (10/10 pass) with
  per-scenario artefacts, scan logs, and `DECISION.md` — the release
  decision distinguishing demonstrated, untested, and deferred work.
- README refreshed (current commands incl. analyze/inspect/assess/publish,
  new stop reasons, restart legs, acceptance entry); HANDOFF gained a dated
  Build 17 addendum (history above it untouched); GUIDANCE refreshed.
- Live demonstration: BLOCKED, tracked in its own issue (no `MUSE_API_KEY`,
  no broker deployment, no provider keys anywhere in the environment —
  verified by name check). Nothing live is claimed or inferred.

## Checks run

```
$ uv run --no-sync python scripts/run_acceptance.py --out acceptance/2026-09-27
verdicts: {'pass': 10, 'fail': 0, 'skip': 0}

clean clone (/tmp/canary-clean @ 5d46016, no credentials, no /tmp artefacts):
$ uv sync --locked && uv run --no-sync pytest -q -m "not live" --deselect ...
397 passed, 2 deselected in 93.44s
$ uv run --no-sync python scripts/run_acceptance.py --out /tmp/clean-accept
verdicts: {'pass': 10, 'fail': 0, 'skip': 0}   # statuses/counts match the bundle
$ git status --short   # tree untouched by the run
(empty)
```

## Findings fixed on the branch

- First bundle commit captured a mid-debug state (2 scenario fails): the
  script was not idempotent across re-runs into one `--out`, macOS tar
  archived AppleDouble members, and the exporter's 0700 modes broke the
  worker image build (non-root entrypoint `cp -a /app/. /work` cannot
  traverse root-owned 0700 dirs from `COPY . /app`). Fixed in the script
  (wipe scenario dirs, `COPYFILE_DISABLE=1`, relax the committed copy to
  standard modes with rationale comment) and the bundle was regenerated
  clean; junk files removed.
- A single-character `detail`/`Detail` inconsistency in the export verdict
  survived several read-throughs; fixed and machine-verified
  (`ord() == 68`), then proven by a green rerun.

## Disposition

Ready for PR. Live acceptance filed separately as an owner-blocked issue;
#18 closes with the offline portion complete and the blocker linked.
