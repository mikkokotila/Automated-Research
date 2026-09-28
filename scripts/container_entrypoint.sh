#!/bin/bash
# Copy the baked repo to a writable tmpfs workspace; the image stays read-only.
set -euo pipefail
export HOME=/work  # all tool caches/configs land on tmpfs, never the read-only rootfs
export PYTHONPATH=/work/src  # test and run the writable copy, not the baked image
rm -rf /work/* /work/.[!.]* 2>/dev/null || true  # clear tmpfs, never the mountpoint
cp -a /app/. /work/
cd /work
git config --global --add safe.directory /work
git config --global user.name "canary-bot"
git config --global user.email "canary-bot@users.noreply.github.com"
# Fresh guest repo: the image carries no .git, so revision machinery gets a
# local repo to diff against. Branch `guest` is an honest name: it claims no
# relation to any host branch (prod-on-main is enforced outside the guest).
git init -q -b guest
git add -A
git commit -qm "canary guest base ${CANARY_BASE_REV:-unknown}"
# Timestamped console capture into the export dir (dashboard raw log).
# The wrapper degrades to a plain exec when the log cannot be opened.
exec python3 scripts/console_tee.py /work/out/console.log "$@"
