#!/bin/bash
# Copy the baked repo to a writable tmpfs workspace; the image stays read-only.
set -euo pipefail
export HOME=/work  # all tool caches/configs land on tmpfs, never the read-only rootfs
export PYTHONPATH=/work/src  # test and run the writable copy, not the baked image
rm -rf /work/* /work/.[!.]* 2>/dev/null || true  # clear tmpfs, never the mountpoint
cp -a /app/. /work/
cd /work
git config --global --add safe.directory /work
git config --global user.name "autoresearch-bot"
git config --global user.email "autoresearch-bot@users.noreply.github.com"
exec "$@"
