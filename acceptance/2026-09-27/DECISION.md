# Release decision — offline acceptance 2026-09-27

**Verdict: accept for offline and contained research use; live acceptance
blocked.** The ten deterministic scenarios in this bundle all pass from a
clean checkout with no credentials. Anything requiring a live provider or
live promotion is not demonstrated and stays open as a tracked blocker —
no success is inferred for it.

## Demonstrated (evidence linked)

| Claim | Evidence |
|---|---|
| Cited review from problem to findings | `scenarios/review-only/` (cited=1, grounded=1, manifest `completed`) |
| Tabular analysis on synthetic data | `scenarios/synthetic-csv/` (computed metrics, recorded split seed) |
| Answers become next questions, then stop | `scenarios/cycle-research/` (2 iters, converged, 5 calls, checkpoints) |
| Revision-enabled work refuses on an uncontained host | `scenarios/maintenance-refusal/` (`ContainmentBlocked` before any work, tree untouched) |
| Revision logic: validated changesets, eval gate, promotion, restart | `scenarios/maintenance-logic/pytest.log` (91 passed, hermetic) |
| Crash/resume preserves research | `scenarios/crash-resume/` (cancelled → resumed converged, spend carried) |
| Budgets stop honestly with carry-forward | `scenarios/budget-exhaustion/` (`budget_exhausted`, `unanswered` kept, exact usage) |
| Provider restriction fails closed | `scenarios/provider-restriction/` (run blocked, keyless client refuses) |
| Container/network/credential/ledger boundaries | `scenarios/containment/container_verify.log` (43/43, no leftovers) |
| Export is bounded, hashed, and secret-scanned | `scenarios/export-scan/` (manifest + clean scan log) |
| Per-build gates behind all of the above | `validation/2026-09-27/build-*.md` on the tested revision |

A known useful change affecting a later iteration, and a harmful change
being rejected, were demonstrated by the Build 16 two-phase run (accepted
record-tagging + split-seed patches executed by a fresh worker; breaking
patch rejected with checks failing). That is an observation about
hermetic, scripted behavior — not a promise of spontaneous live revision,
which has never run.

## Untested assumptions (do not infer)

- Live provider latency, cost, output quality, and broker behavior under
  load. No live model call has been made from this tree.
- Real GitHub publishing, real promotion, and any in-worker auto-merge.
  Revision entry points refuse on every host by design.
- Soundness of any scientific conclusion. Green tests prove control flow:
  budgets enforced, citations resolved, claims downgraded when unanchored,
  analyses leakage-free by construction, stops truthful.
- macOS containment (the offline suite only; containment is Linux/Docker).

## Deferred work (tracked, not dropped)

- Guest HTTP/DNS posture (#4), offline suite network guard (#30), the live
  acceptance blocker filed with this release, and the bandit extension (#19).
- Original recovery evidence is preserved untouched in git history
  (`pre-canary-20260926`); archived commands were not replayed.

## Limitations kept visible

- Claim support is deterministic anchoring, not semantic truth;
  `overlap_hint` is a fallible hint and proves nothing.
- The rare-class cross-validation fixture warning is a known modeling
  limitation (recorded in earlier validation notes).
- Redaction is best-effort pattern matching; the clean scan in this bundle
  covers known credential shapes only.
- Token accounting is exact only when the broker reports receipts; this
  tree makes no monetary cost claims at all.

## Reproduce and verify

```bash
git clone https://github.com/mikkokotila/Canary.git && cd Canary
git checkout <revision below>
uv sync --locked
uv run --no-sync python scripts/run_acceptance.py --out /tmp/accept-repro
```

Compare scenario verdicts with `verdicts.jsonl` (run ids and timestamps
vary; statuses, counts, and schemas must match). The tested revision is
recorded in `manifest.json` (`repo_rev`).

---

- Tested revision: see `manifest.json`
- Date: 2026-09-27
- Maintainer sign-off: ______________________ (runbook reproduction on a fresh environment)

## Live addendum 2026-09-28 (Issue #39)

History above untouched. Broker deployed per runbook §4–5 on the owner
host with a fresh owner-provisioned key (ledger `canary-token-ledger-v1`,
ceiling 200M tokens/24h). Tested revision `cc9feea` (main incl. #49).
All runs below went through `./scripts/container_run.sh` (disposable
worker, `canary-private`, clean tree, per-run minted token, receipt per
run). Bundles live outside the repo under `/tmp/ops-live/`; nothing live
is committed here — observed results only.

| Demo | Result |
|---|---|
| Preflight before any request | `access: unknown, no provider request yet` |
| Live `review` (spaced repetition, 3 papers) | exit 0, `completed`, 3/3 cited, real papers, honest S2-429 degradation to OpenAlex-only, 7,860 tokens, 48.4s wall, export scan clean |
| Live `cycle` pre-fix (test anxiety, 2 iters), 2 runs | identical `failed` at iter 0: empty text, `finish=length`, 9,173 tokens — reasoning burned the 8,000-token completion budget (filed #49, fixed, merged) |
| Live `cycle` post-fix, same question | 2/2 iters, `budget_exhausted` (iteration budget with pending follow-ups — the correct stop), 5 calls, 33,317 tokens, export scan clean |
| Preflight after requests | `access: authorized`, model `muse-spark-1.3-contributor` |
| Token accounting | ledger charged 59,523 == 7860+9173+9173+33317 worker-reported. Exact. |

Still untested: Semantic Scholar with a key (429s without one), broker
behavior under load, real GitHub publishing, spontaneous live revision
(entry points still refuse off-path by design). No cost claims; no
scientific soundness claims. Live failure observed once (#49) was filed,
fixed, and re-proven — not smoothed over.

**Verdict update: the live model path is demonstrated.** The offline
verdict stands as written; the live acceptance blocker (#39) is cleared
by this addendum.

