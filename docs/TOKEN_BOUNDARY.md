# Fixed token and model controls

## Enforced contract

Canary permits only `muse-spark-1.3-contributor`. A shared ceiling of **200,000,000 input-plus-output tokens in every rolling 86,400-second window** is enforced before requests reach the provider. This is neither a midnight reset nor a spending target. Prompts do not implement the control.

The trusted service is in `boundary/`, outside the worker package and its writable checkout. It owns the provider credential and a persistent SQLite ledger. Workers receive only a credential for its narrow request interface. They receive no provider key, ledger mount, Docker socket, or direct outbound route. The launcher places them on an internal network; only the service has an outbound network connection.

## Accounting and failure behavior

Each admitted attempt commits a reservation under `BEGIN IMMEDIATE` before sending anything upstream. There are no hidden SDK retries. Every retry needs its own reservation, shared with all other processes and workers.

Reservations use the entire documented input context allowance (1,048,576 tokens) plus the requested completion limit, capped at 32,768. This deliberately avoids relying on an approximate tokenizer or character ratio. Near the ceiling, a short request can be rejected despite some remaining headroom. This conservative under-use is intentional.

On a successful, structurally valid response, the reservation is reconciled to `prompt_tokens + completion_tokens`; `total_tokens` must agree. Cached input is still input. Reasoning is included in completion tokens, not added twice. Negative, noninteger, inconsistent, over-limit, missing, or unexpected-model responses retain the full reservation and halt further admissions.

A successful charge remains active for 24 hours **after response completion**, not admission. An unresolved request has no expiry: a timeout or crash does not prove the provider stopped processing. Such reservations remain until an operator can reconcile trustworthy provider evidence; there is no worker-facing refund/reset API. This may overcount failures and is preferable to silently overspending.

The clock uses Linux kernel monotonic time, not calendar dates or the mutable wall clock. A new kernel boot receives no downtime credit, so outages can delay expiry but do not create an allowance. Backwards monotonic time fails closed. Restarting processes or workers preserves charges. Missing, corrupt, or mismatched state blocks requests instead of creating a new ledger.

Provider 401/403 responses persistently halt the service. No alternative model, provider, credential recovery, or restriction workaround is attempted.

## First deployment

The service runs in Linux containers. A trusted operator performs one-time initialization:

```bash
./scripts/gate_service.sh init
```

Then supply a fresh, legitimately authorized credential without placing it in files or command arguments:

```bash
read -rs MUSE_API_KEY
export MUSE_API_KEY
./scripts/gate_service.sh start
unset MUSE_API_KEY
./scripts/gate_service.sh status
```

Initialization refuses an existing `canary-token-ledger-v1` volume. Normal startup refuses missing state. Never delete the volume, initialize a second independent ledger for the same workload, or restore stale state to regain allowance. Updating/restarting the service must reuse the same volume. After stopping and removing an old service container, recreate it with the retained volume and reviewed image; do not remove the volume.

## Preflight, rotation, and run-scoped worker credentials

Before trusting the service after any (re)start, the operator checks
preflight. It reports `authorized` only on a genuine recorded provider
success, `blocked` with the halt reason after a denial, or `unknown`
when no provider request has completed yet (live acceptance stays
blocked until the first success):

```bash
docker exec canary-gate python -I -m boundary.server preflight
./scripts/gate_service.sh status   # ledger, budget, and last provider outcome
```

Workers never receive the operator access key. The launcher mints a
short-lived run token per launch (time-to-live covers the run plus a
grace period), passes only that token into the guest, and revokes it on
exit; expiry backstops the revoke. The run receipt records the token id,
never the secret.

To rotate or revoke the provider credential, use the provider's own key
management: revoke the old key there first, then restart the service
with the fresh key (same ledger volume):

```bash
docker stop canary-gate && docker rm canary-gate
read -rs MUSE_API_KEY
export MUSE_API_KEY
./scripts/gate_service.sh start
unset MUSE_API_KEY
```

Never copy a credential from archived logs, issues, or shell history,
and never place one in chat transcripts, GitHub, or any committed file.
The sole sanctioned file holder is a gitignored repo-root `.env`
(template: `.env.example`), which `gate_service.sh start` loads into the
process environment only when the variable is unset.

Run a bounded job through the worker launcher:

```bash
./scripts/container_run.sh cycle "your research question" --max-iterations 3 --out /work/out
```

Reports are exported as bounded regular files into a new local directory. Links, traversal paths, devices, and overwrite attempts are rejected. The former raw tar extraction is no longer used.

If the previously reported provider-account restriction persists, it remains a live-run blocker. No production service or fresh credential was provisioned by the offline verification task.

## Network and publishing changes

To make model selection enforceable for a writable worker, direct internet access is intentionally removed. The service mediates only the exact OpenAlex and Semantic Scholar search endpoints plus the single approved completion route. It is not a generic HTTP proxy. Additional public-web tools require reviewed, mediated routes. This narrows the earlier unrestricted-internet design.

Raw GitHub credentials are no longer passed to workers, so their optional publishing helper stays inactive. Publishing must be performed by a separately authorized controller, not by handing repository credentials to generated code. Existing publishing unit tests remain mocked/local.

## Scope and assumptions

The guarantee covers requests through this Canary deployment, not unrelated Muse CLI sessions or other applications sharing an account. Use a dedicated credential, route all Canary workers through the one service, and do not reuse the upstream key elsewhere. Multiple independent ledgers cannot enforce a combined global allowance.

The provider must honor its documented context/output bounds, selected model, and usage reporting. Client software cannot independently prove which weights a remote provider used or recover undisclosed token consumption. The external service, its image, its clock/kernel, Docker administration, state storage, and the operator are trusted. Worker code is not. The included probes are evidence for tested controls, not a proof against every container/kernel vulnerability.

## Verification and profiling

`tests/test_budget.py` covers exact limits, rolling expiry, retries, cached/reasoning accounting, thread/process contention, restarts, clock changes, missing/corrupt state, model/endpoint injection, missing usage, and boundary refusals. `scripts/container_verify.sh` additionally creates a disposable service and worker network with a fake key and no provider egress; it proves tested refusal paths survive an actual service restart.

The saved profile in `validation/token-boundary/profile.json` is explicitly an **offline synthetic fixture**, not a live Muse benchmark or scientific result. Seed: “How do urban tree canopy and reflective roofs affect daytime heat during heat waves?” It executes three research iterations and five completion requests through the same reservation/reconciliation logic. Token values are simulated. Cycle wall time excludes Python import/startup time; peak RSS includes the scientific Python runtime.

For a legitimate live profile after deployment:

```bash
./scripts/container_run.sh profile --out /work/out/profile
```

The profile uses a bounded three-iteration job with maintenance and publishing disabled. There is no requirement to approach the ceiling. Real latency, token usage, and research quality remain unmeasured until legitimate provider access is available.

## Provider contract references

Verified against Meta's documentation on 26 September 2026:

- [Models](https://dev.meta.ai/docs/models): exact model ID and 1,048,576-token context window.
- [Chat completions](https://dev.meta.ai/docs/protocols/chat-completions): request fields, `max_completion_tokens`, single completion, and usage structure.
- [Reasoning](https://dev.meta.ai/docs/reasoning): reasoning counts toward the completion limit and billed completion tokens.

Changing a provider contract requires a reviewed boundary update, never an automatic fallback or a worker-controlled override.
