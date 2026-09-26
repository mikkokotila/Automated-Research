#!/bin/bash
# Exercise a private network and persistent service with fixture credentials only.
set -euo pipefail
cd "$(dirname "$0")/.."
IMG="${IMG:-canary:local}"
docker build -q -t "$IMG" .
docker build -q -f boundary/Dockerfile -t canary-boundary:check .
python3 scripts/verify_boundary.py --image "$IMG" --gate-image canary-boundary:check
