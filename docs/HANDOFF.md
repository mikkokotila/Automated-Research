# Automated Research — continuation handoff

Snapshot: **26 September 2026**, Europe/Helsinki (EEST, UTC+03:00).

## Start here

The original Muse Code build completed milestones 1–3 and stopped partway through milestone 4. The previously uncommitted M4 implementation is preserved in `src/autoresearch/` and `tests/test_milestone4.py`. This is a preservation checkpoint, **not a declaration that self-improvement or host isolation is ready**. No autonomous research or self-modification was launched during recovery.

Read the [session transcript](../recovery/2026-09-26/session/transcript.md) and [recovery inventory](../recovery/2026-09-26/README.md). The main saved loop output is [synthesis.md](../recovery/2026-09-26/artifacts/m3-loop/synthesis.md), with its [run manifest](../recovery/2026-09-26/artifacts/m3-loop/run.json).

The source state before recovery was commit `77501f654534f880682389fdf525405ebf53f8ae` plus seven changed/new files. Original source and test contents are unchanged; recovery adds documentation, archives, and credential-file ignore rules rather than changing implementation.

## Purpose and decisions

Mikko initiated this work from his article, [Automated Researcher and Beyond: The Evolution of Artificial Intelligence](https://mikkokotila.medium.com/automated-researcher-and-beyond-the-evolution-of-artificial-intelligence-5db4fdde6f1c). The goal was to connect existing capabilities into a researcher: a plain-language problem becomes literature retrieval, synthesis, optional data analysis, follow-up questions, and eventually self-reflection and self-improvement.

The user explicitly chose **Muse API, not Claude API**, and **`muse-spark-1.3-contributor`**. The source client uses `https://api.meta.ai/v1` through the OpenAI-compatible Python SDK; this does not mean the application uses an OpenAI model. These are recovered settings, not a fresh verification of provider availability.

The actual implementation is a Python CLI with `httpx`, `pandas`, `numpy`, and `scikit-learn`, not the larger FastAPI/LangGraph/Postgres/AutoGluon stack floated in the initial discussion.

## What was built

| Stage | Implementation | Recorded evidence |
|---|---|---|
| M1 | `review`: OpenAlex + Semantic Scholar retrieval, DOI deduplication, local reranking, cited Muse synthesis, Markdown and provenance | Commit `a971a81`; session reported 14 passing tests; saved live review |
| M2 | `analyze`: CSV preparation, cross-validation model selection, held-out evaluation, warnings, narration and provenance | Commit `5e38d8c`; session reported 25 passing tests; saved live analysis and synthetic input |
| M3 | `loop`: queued review/analyze jobs, proposed follow-ups, question deduplication, stopping and final synthesis | Commit `77501f6`; session reported 36 passing tests; saved two-iteration live loop |
| M4 | Journal, reflection proposals, test-gated patches, CLI integration and tests | Previously uncommitted work in progress; last recorded run: 53 tests passed. Container isolation and live self-improvement were not completed/verified |

The final successful test output is preserved in [original-session-last-tests.txt](../recovery/2026-09-26/validation/original-session-last-tests.txt), with structured evidence beside it. It records `53 passed in 11.84s` and `docker UP`. This is historical evidence, **not a new test execution during recovery**. A running Docker daemon does not establish application containerization or isolation.

The saved M3 demo used “does treatment delay raise cancer mortality?”, two iterations, and three papers per review. It stopped at `max_iterations`; its own report says the retrieved material did not resolve the seed question. These are software demonstration artefacts, not validated scientific or clinical conclusions.

## Exact stopping point

Session: `calm-fireball`, ID `01a0ddd3-a8a8-70c1-ad3c-63f81dcabea5`. It started at 16:07 EEST and ended with a recorded failure at approximately 16:50 EEST on 26 September 2026.

The saved terminal event reports Muse API HTTP 403 and an account-level restriction. It says retrying will not help and refers the user to Muse Code support. This describes the recorded API response, not an independent assessment of the account. Do not repeatedly retry or attempt to bypass that restriction. No later successful M4 completion is recorded.

The last code operation fixed `test_journal_text_tail_caps`; all 53 tests then passed. The next model request failed before a final M4 response or commit. Preserved changes:

- New: `src/autoresearch/journal.py`, `reflect.py`, `improve.py`, and `tests/test_milestone4.py`.
- Modified: `src/autoresearch/cli.py`, `loop.py`, and `report.py`.

The [complete original M4 patch](../recovery/2026-09-26/working-tree/milestone4.patch) includes all seven files relative to M3. It is for recovery/provenance: **do not reapply it to this checkpoint**, where those changes already exist.

## M4 requirements and current behavior

The final user request required procedural notes throughout each workflow, review of those notes, documentation of improvements, and application of those improvements. Autonomous improvement should occur iteratively, not only after a whole workflow. The user made **isolation from the host, with internet access inside the container**, a key safety requirement. Exact wording: [transcript](../recovery/2026-09-26/session/transcript.md), source line 3895.

Current code:

- `Journal` collects timestamped phase/event/detail notes; report writers can save `journal.jsonl`.
- `reflect.py` parses reflection Markdown and up to three proposed changes from Muse JSON.
- `improve.py` requests unified diffs, restricts target paths, requires an initially clean source/test tree and passing checks, and keeps patches when checks pass. These are application checks, not an OS security boundary.
- `loop --self-improve` attempts a pass after an iteration and configured rounds after synthesis. `improve --run-dir ...` separately processes an existing journal.
- **No Dockerfile, container launcher, verified host-isolation policy, or enforced container-only execution check exists in the recovered source. Do not run self-modifying modes on the host.**

## Continuation priorities

### 1. Establish the execution boundary first

Define the threat model and implement a disposable execution environment before enabling self-modification. Address host mounts, container-engine sockets, privileges, host/LAN/metadata-service access, credential exposure, resource limits, and controlled artefact export. Keep isolation controls outside the agent-modifiable tree. Verify the chosen boundary with tests; Docker presence and passing application tests are not an absolute containment guarantee. This is remaining engineering work, not an implemented launch recipe.

### 2. Review M4 before enabling it

Static observations from the recovered source, **not a comprehensive audit**:

- Notes accumulate in memory and are saved by report writers, not durably appended throughout execution. `Journal.text()` defaults to the last 6,000 characters, so reflection may not review every note.
- Automatic loop improvement discards the returned reflection document; standalone `improve` saves `reflection.md`.
- `request_diff()` provides the proposal but not actual target-file contents. Review whether patch generation has enough context.
- Accepted patches remain uncommitted, so later rounds can fail the clean-tree check. Failed-patch rollback restores tracked targets from HEAD and can discard an earlier accepted change to the same file. Review transactional snapshots and rollback.
- Path filtering and tests do not contain arbitrary Python execution. Audit complete diff semantics, subprocess behavior, and runtime capabilities.
- Source changes on disk do not reload already imported modules. Define how improved code becomes active.

Other limits: `MAX_ITERATIONS` is 5; follow-up proposal errors can appear as convergence; iteration bounds are not monetary cost budgets. Standalone `review` and `analyze` write journals but do not automatically self-improve afterward.

### 3. Revalidate and demonstrate an isolated M4 run

Inside the chosen disposable development environment, install the project and test tool and run tests without piping away the exit status. These are continuation instructions, not commands executed during recovery:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
python -m pytest -q
```

`pyproject.toml` declares Python >=3.10; the original working environment used Python 3.12. `uv.lock` is preserved; pytest is not a declared project dependency, so installing the test tool separately is intentional.

Resolve legitimate provider access and inject a fresh credential through the environment, not tracked files or logged command arguments. The client checks `MUSE_API_KEY`, then `MODEL_API_KEY`, then `META_API_KEY`. Semantic Scholar has an optional separate API key. The source currently uses an 8,000-token completion budget. Recheck provider/model availability when actually resuming.

First repeat bounded non-self-modifying demos inside the isolated environment, then demonstrate one bounded reflection/patch cycle with new output paths. Require durable notes, saved reflection, patch and test evidence, rollback evidence, and isolation validation before declaring M4 complete. Archived M1–M3 runs predate journaling and have **no `journal.jsonl`**; do not pass them directly to `improve --run-dir` without a new compatible run or documented migration.

## Artefacts and historical paths

| Original location | Preserved under `recovery/2026-09-26/` |
|---|---|
| `/tmp/ar-live/` | `artifacts/m1-review/` |
| `/tmp/ar-m2/` | `artifacts/m2-analysis/` |
| `/tmp/ar-m3/` | `artifacts/m3-loop/` |
| `/tmp/ar-patients.csv` | `inputs/ar-patients.csv` |
| Original Muse session directory | `session/` (redacted text history and tool outputs) |

Report contents and historical absolute paths were retained for provenance. Supply the relocated input path when rerunning. The 300-row patient dataset is synthetic, generated by the recorded command at source line 2618 using NumPy `RandomState(7)`; it is not real patient data. Its exact generation command is in `session/tool-calls.jsonl`.

## Credentials, privacy, and archive boundaries

The GitHub repository was public at the start of recovery and changed to **private before recovery material was uploaded**. The API key and other detected credential-like tokens were removed from copied session files and generated extractions. Existing reachable Git history was scanned before upload; no matches for those credentials or checked token formats were found. Final scan scope is in the validation record. Pattern-based scanning is not proof that arbitrary unknown secrets cannot exist.

Original Muse files outside this repository were left untouched and may still contain the old key. Redaction is not revocation; rotate/revoke the previously shared key through the provider. No credentials were tested, revoked, or replaced during recovery.

Only this research-build session was archived, not unrelated concurrent sessions. Runtime locks, SQLite session/cache databases, the virtual environment, and generated caches are excluded. The archive is evidence for continuation, not a supported Muse session-import format: redaction changes bytes and may invalidate internal sizes/digests/signatures. Do not automatically execute archived commands.
