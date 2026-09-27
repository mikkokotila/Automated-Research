#!/bin/bash
# Exercise a private network and persistent service with fixture credentials only.
set -euo pipefail
cd "$(dirname "$0")/.."
IMG="${IMG:-canary:local}"
GATE_IMG="canary-boundary:check"
# CANARY_PREBUILT=1 skips rebuilds only when the caller built both images from
# this same checkout (CI pre-build step). Default always rebuilds: never verify stale.
if [ "${CANARY_PREBUILT:-0}" != "1" ]; then
  docker build -q -t "$IMG" .
  docker build -q -f boundary/Dockerfile -t "$GATE_IMG" .
fi
python3 scripts/verify_boundary.py --image "$IMG" --gate-image "$GATE_IMG"
