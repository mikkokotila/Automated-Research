# Release decision — offline acceptance 2026-09-28

**Verdict: accept for offline and contained research use; live acceptance
extended with stated gaps.** The ten deterministic scenarios in this
bundle all pass from a clean checkout with no credentials (repo_rev
`fdabef6` plus the two `run_acceptance.py` edits in the landing PR:
container-check count 43→47, leftover check made differential).
Anything listed under gaps stays open — no success is inferred for it.

## Demonstrated (evidence linked)

| Claim | Evidence |
|---|---|
| Cited review from problem to findings | `scenarios/review-only/` (cited=1, grounded=1, manifest `completed`) |
| Tabular analysis on synthetic data | `scenarios/synthetic-csv/` (computed metrics, recorded split seed) |
| Answers become next questions, then stop | `scenarios/cycle-research/` (2 iters, converged, 5 calls, checkpoints) |
| Revision-enabled work refuses on an uncontained host | `scenarios/maintenance-refusal/` (`ContainmentBlocked` before any work, tree untouched) |
| Revision logic: validated changesets, eval gate, promotion, restart | `scenarios/maintenance-logic/pytest.log` (110 passed, hermetic) |
| Crash/resume preserves research | `scenarios/crash-resume/` (cancelled → resumed converged, spend carried) |
| Budgets stop honestly with carry-forward | `scenarios/budget-exhaustion/` (`budget_exhausted`, `unanswered` kept, exact usage) |
| Provider restriction fails closed | `scenarios/provider-restriction/` (run blocked, keyless client refuses) |
| Container/network/credential/ledger boundaries | `scenarios/containment/container_verify.log` (47/47, no leftovers) |
| Export is bounded, hashed, and secret-scanned | `scenarios/export-scan/` (15 files, manifest + clean scan log) |

## New since 2026-09-27 (observed, not all in this bundle)

- First genuine kept patch from the unmanned loop: keep1m proposed,
  validated in-guest, and kept p2 (coverage warning in synthesis),
  landed via maintainer PR #81 after review + completion (#67 closed).
  Sibling redundant proposals were rejected by the loop's own gate.
- Live retrieval now runs on arXiv (new broker source, #79/#80) plus
  OpenAlex; Semantic Scholar dropped (keyless pool saturated, no key).
  keep1m reviewed 25 live papers, 3/3 cited.
- Container→host publish bridge landed (#66, code + tests, fake-GitHub
  proven); a live guest-to-PR run has not been exercised yet.
- Prod launches pin HEAD to origin/main host-side (#64); auto-merge
  waits for non-author approval (#62 code gate, owner ruleset pending);
  safety CI runs from worker-unreachable canary-guards (#63).
- Flaky timeout test made deterministic (#76); dashboard launch env (#82).

## Untested assumptions (do not infer)

- Live guest-to-PR publishing end to end (bridge code is tested
  against fakes only).
- Broker behavior under load; keyless OpenAlex/arXiv quota stability
  (both flaked within the last 24h).
- keep1o (2026-09-28) failed after 33 minutes with no bundle and no
  log; cause unknown, infra healthy. Under watch; not smoothed over.
- Soundness of any scientific conclusion; macOS containment (Linux/Docker
  only); monetary cost claims (none made).

## Reproduce and verify

```bash
git clone https://github.com/mikkokotila/Canary.git && cd Canary
git checkout <landing-PR merge>
uv sync --locked
uv run --no-sync python scripts/run_acceptance.py --out /tmp/accept-repro
```

Compare scenario verdicts with `verdicts.jsonl` (run ids and timestamps
vary; statuses, counts, and schemas must match).

---

- Tested revision: `fdabef6` + landing-PR script edits (see manifest.json)
- Date: 2026-09-28
- Maintainer sign-off: ______________________ (runbook reproduction on a fresh environment)
