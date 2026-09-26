#!/bin/bash
# Run a worker with no direct egress, provider credential, or ledger mount.
set -euo pipefail
cd "$(dirname "$0")/.."
IMG="${IMG:-canary:local}"
RUN=(canary "$@")
if [ "${1:-}" = profile ]; then
  shift
  RUN=(python scripts/profile_cycle.py --live "$@")
fi
NAME="canary-run-$(date +%s)-$$"
VOL="$NAME-out"
OUT="${OUT:-./container-out/$NAME}"
test "$(docker network inspect -f '{{.Internal}}' canary-private)" = true
test "$(docker inspect -f '{{.State.Running}}' canary-gate)" = true
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git status --porcelain -- src tests runs)" ]; then
  echo "Refusing dirty source tree; commit before maintenance" >&2; exit 2
fi
docker build -q -t "$IMG" .
docker volume create "$VOL" >/dev/null
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Only this narrow service credential reaches the worker, never the upstream key.
export CANARY_GATE_TOKEN
CANARY_GATE_TOKEN=$(docker exec canary-gate cat /state/access.key)
docker run --rm --network none --user 0 --entrypoint sh   --mount type=volume,src="$VOL",dst=/export "$IMG" -c 'chown 10002:10002 /export'
set +e
docker run --name "$NAME" --network canary-private --user 10002:10002   --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2   --read-only --tmpfs /tmp:rw,size=512m --tmpfs /work:rw,size=2g,uid=10002,gid=10002   --mount type=volume,src="$VOL",dst=/work/out   -e CANARY_GATE_TOKEN -e CANARY_GATE_URL=http://canary-gate:8787   -e OPENALEX_MAILTO "$IMG" "${RUN[@]}"
RC=$?
set -e
unset CANARY_GATE_TOKEN
docker run --rm --network none --read-only --entrypoint tar   --mount type=volume,src="$VOL",dst=/from,readonly "$IMG" cf - -C /from .   | python3 scripts/export_bundle.py "$OUT"
echo "exit=$RC out=$OUT"
exit "$RC"
