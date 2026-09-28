#!/bin/bash
# Trusted worker launcher: disposable container, externally enforced limits.
#
# The worker gets no direct egress, provider credential, ledger mount, GitHub
# token, or host bind mounts. Everything the guest may use is staged by this
# host-side script into launcher-owned volumes; the guest cannot raise its
# own limits. See docs/TRUST_BOUNDARY.md.
#
# Knobs (all host-side, operator-controlled):
#   IMG               worker image (default canary:local)
#   OUT               host export dir (default ./container-out/<name>)
#   CANARY_NETWORK    must be an internal-only network (default canary-private)
#   CANARY_GATE_NAME  running broker container (default canary-gate)
#   CANARY_TIMEOUT_S  wall-time limit in seconds (default 1800)
#   CANARY_WHEELS     host dir of vetted wheels, mounted read-only at /wheels
#   CANARY_STAGE      comma list of src:dest staged read-only at /inputs;
#                     src is https://... (host curl, capped) or a host file (cp)
#   ALLOW_DIRTY=1     operator override for the clean-tree requirement (logged)
#   exec ...          run a raw guest command (operator fixture hook, contained)
#   RUN_KEY           dashboard registry key (default: OUT basename)
#   RUN_NAME/RUN_BRIEF name the run in the dashboard (default: key / argv echo)
set -euo pipefail
cd "$(dirname "$0")/.."
IMG="${IMG:-canary:local}"
NETWORK="${CANARY_NETWORK:-canary-private}"
GATE="${CANARY_GATE_NAME:-canary-gate}"
TIMEOUT_S="${CANARY_TIMEOUT_S:-1800}"
RUN=(canary "$@")
MODE="${1:-?}"
ARGV_STR="$*"
ARGV_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
  bash scripts/container_run.sh "$@" 2>/dev/null)" || ARGV_JSON=""
if [ "${1:-}" = exec ]; then
  shift
  RUN=("$@")
  if [ "${#RUN[@]}" -eq 0 ]; then echo "exec needs a command" >&2; exit 2; fi
elif [ "${1:-}" = profile ]; then
  shift
  RUN=(python scripts/profile_cycle.py --live "$@")
fi
NAME="canary-run-$(date +%s)-$$"
VOL="$NAME-out"
WVOL="$NAME-wheels"
IVOL="$NAME-inputs"
TOKEN_ID=""
OUT="${OUT:-./container-out/$NAME}"
STARTED=$(date +%s)
# Unsafe launch configurations fail closed before anything starts.
if [ "$NETWORK" = "host" ]; then
  echo "refusing: host network is never allowed for workers" >&2; exit 2
fi
test "$(docker network inspect -f '{{.Internal}}' "$NETWORK")" = true
test "$(docker inspect -f '{{.State.Running}}' "$GATE")" = true
case "${TIMEOUT_S}" in ''|*[!0-9]*) echo "refusing: CANARY_TIMEOUT_S must be an integer" >&2; exit 2;; esac
DIRTY="false"
if [ -n "$(git status --porcelain -- src tests runs)" ]; then
  if [ -z "${ALLOW_DIRTY:-}" ]; then
    echo "Refusing dirty source tree; commit or set ALLOW_DIRTY=1" >&2; exit 2
  fi
  DIRTY="operator-override"
  echo "WARNING: dirty source tree allowed by operator override" >&2
fi
# CANARY_SKIP_BUILD=1 reuses a prebuilt image (CI pre-build step); the inspect
# below still fails closed when the image is missing.
if [ "${CANARY_SKIP_BUILD:-0}" != "1" ]; then
  docker build -q -t "$IMG" .
fi
IMAGE_ID=$(docker inspect -f '{{.Id}}' "$IMG")
IMAGE_DIGEST=$(docker inspect -f '{{index .RepoDigests 0}}' "$IMG" 2>/dev/null || true)
docker volume create "$VOL" >/dev/null
HAVE_WHEELS="false"
HAVE_INPUTS="false"
STAGED=""
cleanup() {
  if [ -n "$TOKEN_ID" ]; then
    docker exec "$GATE" python -I -m boundary.server revoke-token --id "$TOKEN_ID" >/dev/null 2>&1 || true
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOL" "$WVOL" "$IVOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
helper_copy() { # $1 = host dir, $2 = volume: trusted copy, worker never sees the bind
  docker run --rm --network none --user 0 --entrypoint sh \
    --mount type=bind,src="$1",dst=/src,readonly \
    --mount type=volume,src="$2",dst=/dst "$IMG" \
    -c 'cp -r /src/. /dst/ && chmod -R a+rX /dst'
}
if [ -n "${CANARY_WHEELS:-}" ]; then
  test -d "$CANARY_WHEELS"
  docker volume create "$WVOL" >/dev/null
  helper_copy "$CANARY_WHEELS" "$WVOL"
  HAVE_WHEELS="true"
fi
if [ -n "${CANARY_STAGE:-}" ]; then
  STAGE_DIR=$(mktemp -d)
  trap 'rm -rf "$STAGE_DIR"; cleanup' EXIT
  IFS=',' read -ra PAIRS <<< "$CANARY_STAGE"
  for pair in "${PAIRS[@]}"; do
    src="${pair%:*}"; dest="${pair##*:}"  # split on last colon: URLs contain ://
    case "$dest" in ''|*/*|*..*|*:*) echo "refusing: bad stage name $dest" >&2; exit 2;; esac
    if [ "$src" != "${src#https://}" ]; then
      curl --proto '=https' --max-time 30 --max-filesize 10485760 -fsSL "$src" -o "$STAGE_DIR/$dest"
    else
      test -f "$src"
      test "$(wc -c < "$src")" -le 10485760
      cp "$src" "$STAGE_DIR/$dest"
    fi
    STAGED="$STAGED $dest"
  done
  docker volume create "$IVOL" >/dev/null
  helper_copy "$STAGE_DIR" "$IVOL"
  HAVE_INPUTS="true"
fi
# The worker gets a short-lived run token, never the operator access key or
# the upstream provider key. Revoked on exit; expiry backstops the revoke.
export CANARY_GATE_TOKEN
MINTED=$(docker exec "$GATE" python -I -m boundary.server mint-token --ttl $((TIMEOUT_S + 300)))
TOKEN_ID="${MINTED%% *}"
CANARY_GATE_TOKEN="${MINTED#* }"
docker run --rm --network none --user 0 --entrypoint sh \
  --mount type=volume,src="$VOL",dst=/export "$IMG" -c 'chown 10002:10002 /export'
MOUNTS=(--mount "type=volume,src=$VOL,dst=/work/out")
if [ "$HAVE_WHEELS" = "true" ]; then MOUNTS+=(--mount "type=volume,src=$WVOL,dst=/wheels,readonly"); fi
if [ "$HAVE_INPUTS" = "true" ]; then MOUNTS+=(--mount "type=volume,src=$IVOL,dst=/inputs,readonly"); fi
set +e
# CANARY_GUEST=1 is the interim worker role marker (Issue #52): injected only
# by this launcher, never baked into the image. CANARY_BASE_REV records which
# host revision the guest workspace was copied from (provenance, not proof).
# /tmp is exec (Docker tmpfs defaults to noexec): the revision eval gate runs
# the test suite in-guest, and hermetic fixtures stage executable stubs there.
BASE_REV="$(git rev-parse HEAD)"
# Dashboard registry hooks (best-effort: they must never fail a run).
RUN_KEY="${RUN_KEY:-$(basename "$OUT")}"
RUN_NAME="${RUN_NAME:-$RUN_KEY}"
RUN_BRIEF="${RUN_BRIEF:-canary $ARGV_STR}"
RUN_KIND="${RUN_KIND:-$MODE}"
python3 scripts/register_run.py start --key "$RUN_KEY" --name "$RUN_NAME" \
  --brief "$RUN_BRIEF" --kind "$RUN_KIND" --bundle "$OUT" --container "$NAME" \
  --launch "bash scripts/container_run.sh $ARGV_STR" \
  --launch-argv "$ARGV_JSON" >/dev/null 2>&1 || true
docker run --name "$NAME" --network "$NETWORK" --user 10002:10002 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2 \
  --stop-timeout 30 --read-only --tmpfs /tmp:rw,exec,size=512m --tmpfs /work:rw,size=2g,uid=10002,gid=10002 \
  "${MOUNTS[@]}" -e CANARY_GATE_TOKEN -e CANARY_GATE_URL=http://canary-gate:8787 \
  -e CANARY_GUEST=1 -e "CANARY_BASE_REV=$BASE_REV" \
  -e OPENALEX_MAILTO "$IMG" "${RUN[@]}" &
RUNPID=$!
TIMED_OUT="false"
elapsed=0
while kill -0 "$RUNPID" 2>/dev/null; do
  if [ "$elapsed" -ge "$TIMEOUT_S" ]; then
    docker stop -t 30 "$NAME" >/dev/null 2>&1 || true
    TIMED_OUT="true"
    break
  fi
  sleep 1
  elapsed=$((elapsed + 1))
done
wait "$RUNPID"
RC=$?
set -e
unset CANARY_GATE_TOKEN
docker run --rm --network none --read-only --entrypoint tar \
  --mount type=volume,src="$VOL",dst=/from,readonly "$IMG" cf - -C /from . \
  | python3 scripts/export_bundle.py "$OUT"
FINISHED=$(date +%s)
RECEIPT="$OUT.receipt.json"
RECEIPT="$RECEIPT" NAME="$NAME" IMG="$IMG" IMAGE_ID="$IMAGE_ID" IMAGE_DIGEST="$IMAGE_DIGEST" NETWORK="$NETWORK" \
GATE="$GATE" TIMEOUT_S="$TIMEOUT_S" TIMED_OUT="$TIMED_OUT" RC="$RC" DIRTY="$DIRTY" \
HAVE_WHEELS="$HAVE_WHEELS" HAVE_INPUTS="$HAVE_INPUTS" STAGED="$STAGED" TOKEN_ID="$TOKEN_ID" \
STARTED="$STARTED" FINISHED="$FINISHED" GIT_REV="$(git rev-parse HEAD)" \
python3 -c 'import json, os; json.dump({k: os.environ[k] for k in ("NAME","IMG","IMAGE_ID","IMAGE_DIGEST","NETWORK","GATE","TIMEOUT_S","TIMED_OUT","RC","DIRTY","HAVE_WHEELS","HAVE_INPUTS","STAGED","TOKEN_ID","STARTED","FINISHED","GIT_REV")}, open(os.environ["RECEIPT"], "w"), indent=2)'
if [ "$RC" = "0" ]; then FINAL_STATUS="done"; else FINAL_STATUS="failed"; fi
python3 scripts/register_run.py finish --key "${RUN_KEY:-$(basename "$OUT")}" \
  --status "$FINAL_STATUS" --bundle "$OUT" --container "$NAME" >/dev/null 2>&1 || true
echo "exit=$RC out=$OUT receipt=$RECEIPT"
exit "$RC"
