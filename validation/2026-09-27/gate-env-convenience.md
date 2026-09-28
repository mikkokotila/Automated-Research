# 2026-09-28 Broker start loads repo-root .env (owner UX)

**Revision under test:** branch `fix-gate-env-convenience`
**Owner contract compliance:** operator-script change; no revision paths
touched; no credentials printed, committed, or baked (verified by
refusal-message proofs, never by echoing values); recovery evidence
untouched.

## What changed

- `scripts/gate_service.sh start`: when `MUSE_API_KEY` is unset/empty
  and a repo-root `.env` exists, it is sourced into the process
  environment (`set -a`, then off) before the existing `:?` guard.
  Explicit environment always wins; absent both, the guard still fails
  closed. The broker still receives the key solely via process
  environment — never baked into images, never committed (`.env` is
  gitignored), never worker-visible.
- Truthfulness pass on the superseded "never from a file" wording:
  `.env.example` header, `docs/RUNBOOK.md` §10, `docs/TOKEN_BOUNDARY.md`
  rotation section. The load-bearing rule is now stated as: no committed
  file, no image, no worker env, no transcripts — with gitignored `.env`
  as the sole sanctioned file holder.

## Checks run

```
$ bash -n scripts/gate_service.sh
syntax-ok
$ env -u MUSE_API_KEY ./scripts/gate_service.sh start
Service already exists; inspect/restart it without deleting its ledger
exit=2   # past the :? guard => .env loaded; refusal proves zero state change
$ cp script to /tmp/nogate (no .env); env -u MUSE_API_KEY ./gate_service.sh start
MUSE_API_KEY: Supply a fresh authorized credential; do not use archived keys
exit=1   # guard still fails closed with neither env nor file
```

- `tests/test_gate_env.py` (new, hermetic): stages a copy of the script
  with a stub `docker` on PATH — never the repo checkout, so the real
  `.env` is unreadable. Three tests: explicit env wins, `.env` seeds an
  empty environment (values never echoed), and fail-closed with neither
  (docker never invoked). Against the pre-fix script, exactly the
  seeding test fails; the other two pass (preserved behavior).

## Disposition

Ready for PR. After merge, a fresh host needs only: key in `.env`,
then `./scripts/gate_service.sh start` — no manual export ritual.
