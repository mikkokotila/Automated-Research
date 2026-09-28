# Worker operating notes

What worker code (anything under `src/canary/` that runs inside a research
run) may do, must do, and must never do. The container boundary enforces the
hard limits externally; these rules keep worker logic honest inside them.

## Authority

- Workers act only within the operator-authorized scope frozen in the run
  spec: goal question, datasets, budgets, maintenance flag, repo root, and
  check command. Nothing the model writes can widen that envelope.
- No per-action approval is needed inside the envelope; anything outside it
  (new credentials, new hosts, new write targets) is refused, not asked.
- Revision entry points refuse on every host today. Workers must treat
  `ContainmentBlocked` as a final answer, never retry around it.

## Budgets

- One shared `RunBudget` per run, threaded through research, assessment,
  and revision. Attempts spend before dispatch and are never refunded.
- Workers never mint budget: no new trackers mid-run, no max-raising, no
  wall-clock games. Spend carries across resume and restart legs.
- Finalization reserves (`finalize_calls`) are held back before the last
  report; exhaustion stops the run with an honest partial bundle.

## State and restarts

- Journal every phase transition and every external outcome; notes are
  durable from the first event and distinguish worker observations from
  trusted supervisor facts (workers never emit trusted notes).
- Checkpoint after every iteration and every propose round: iterations,
  pending work, seen questions, budgets, code/input hashes, scheduler
  counters, scope, and any pending restart.
- After an accepted revision the running process must halt
  (`restart_required`): already-imported modules are stale, and continuing
  on them silently is forbidden. A fresh worker continues research-only on
  the verified revision; further revision needs a new maintained run.
- Scheduler decisions take runner-observed counts only (iterations,
  grounded claims, proposal outcomes, call spend). Model text never feeds
  stop/veto/restart decisions.

## Research quality

- Reviews cite retrieved papers; claims anchor to verbatim spans or are
  downgraded to unsupported. Citation markers without a validated claims
  block are ungrounded and trip the no-progress stop after two iterations.
- Analyses run per-fold pipelines on per-question splits; the holdout is
  scored once; causal questions are refused, not answered weakly.
- Follow-ups bind lineage runner-side (`parent` is never model-set) and
  prefer stated evidence gaps over adjacent curiosities.
- Assessments cover the whole journal in chunks with explicit coverage;
  partial coverage says `partial` and names the unreviewed ranges.

## Memory and promotion

- Lessons are untrusted observations, never policy: they cannot change
  targets, budgets, or the trust gate. Contradicted lessons are superseded
  with provenance, never silently edited.
- Rejected patches leave research on the previous accepted version with
  the rejection recorded (candidate file + memory + journal).
- Promotion is transactional and hash-bound: only the exact validated
  candidate promotes, the accepted pointer swaps atomically, and the live
  tree must match accepted afterwards or halt.
- Publishing to GitHub is a separate maintainer action (`canary publish`)
  in a separate process; workers refuse to hold the token.
- Container yields cross via `canary publish --bundle <export>`: the
  bundle is scan-gated, each kept diff is verified against its
  candidate manifest (`diff_sha`), policy-checked, and applied to
  main before the auto-PR. Tampered or policy-breaking exports
  refuse without side effects.

## Failure posture

- Stop reasons name the truth: converged, budget, evidence, novelty,
  provider, cancellation, or failure — never a guessed success.
- Malformed model output retries once, then stops honestly; it never
  crashes the run and never fabricates content.
- Partial bundles stay complete and inspectable: manifest, journal,
  checkpoints, iterations, and exact usage survive every stop.
