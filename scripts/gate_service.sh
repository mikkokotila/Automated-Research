#!/bin/bash
# Operator-managed service: one persistent ledger for every Canary worker.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=canary-boundary:1
VOLUME=canary-token-ledger-v1
case "${1:-}" in
  init)
    if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
      echo "Refusing: ledger volume already exists; never reset it to regain allowance" >&2; exit 2
    fi
    docker build -f boundary/Dockerfile -t "$IMAGE" .
    docker volume create "$VOLUME" >/dev/null
    docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges       --mount type=volume,src="$VOLUME",dst=/state "$IMAGE" init
    ;;
  start)
    # Convenience, not a new trust path: a gitignored repo-root .env seeds
    # the process environment only when the operator has not already set
    # the key. Explicit environment always wins; the broker still receives
    # the key solely via process environment (never baked, committed, or
    # worker-visible). Absent both, the guard below still fails closed.
    if [ -f ./.env ] && { [ -z "${MUSE_API_KEY:-}" ] || [ -z "${OPENALEX_API_KEY:-}" ]; }; then
      _explicit_muse="${MUSE_API_KEY:-}"
      set -a
      # shellcheck disable=SC1091
      . ./.env
      set +a
      if [ -n "$_explicit_muse" ]; then MUSE_API_KEY="$_explicit_muse"; fi
      unset _explicit_muse
    fi
    : "${MUSE_API_KEY:?Supply a fresh authorized credential; do not use archived keys}"
    docker volume inspect "$VOLUME" >/dev/null
    if docker container inspect canary-gate >/dev/null 2>&1; then
      echo "Service already exists; inspect/restart it without deleting its ledger" >&2; exit 2
    fi
    docker network inspect canary-private >/dev/null 2>&1 || docker network create --internal canary-private >/dev/null
    test "$(docker network inspect -f '{{.Internal}}' canary-private)" = true
    docker network inspect canary-egress >/dev/null 2>&1 || docker network create canary-egress >/dev/null
    docker create --name canary-gate --network canary-private --network-alias canary-gate       --restart unless-stopped --read-only --tmpfs /tmp:rw,size=64m       --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m --cpus 1       --mount type=volume,src="$VOLUME",dst=/state       -e MUSE_API_KEY -e OPENALEX_API_KEY "$IMAGE" serve >/dev/null
    docker network connect canary-egress canary-gate
    docker start canary-gate >/dev/null
    echo "Service started. No host ports are published; ledger survives worker restarts."
    ;;
  status)
    docker exec canary-gate python -I -m boundary.server status
    ;;
  *) echo "Usage: $0 {init|start|status}" >&2; exit 2 ;;
esac
