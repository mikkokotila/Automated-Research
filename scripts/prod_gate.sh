#!/bin/bash
# Host-side prod gate (Issue #64): prod-profile launches run on origin/main only.
#
# Usage: prod_gate.sh <repo> [launcher args...]
#
# Parses --profile from the launcher args (dev default). Dev passes through.
# Prod requires, all checked on the HOST checkout (never guest claims):
#   1. branch name is exactly main (detached HEAD refuses),
#   2. origin/main resolves (unverifiable remote refuses),
#   3. local HEAD equals origin/main (stale or ahead refuses).
# Exit 0 passes, 2 refuses. Unknown --profile values refuse.
set -euo pipefail
repo="${1:-}"
if [ -z "$repo" ] || [ ! -d "$repo" ]; then
  echo "prod_gate: refusing: bad repo path" >&2; exit 2
fi
shift || true
profile="dev"
prev=""
for arg in "$@"; do
  if [ "$prev" = "--profile" ]; then profile="$arg"; prev=""; continue; fi
  case "$arg" in
    --profile=*) profile="${arg#--profile=}" ;;
    --profile) prev="--profile" ;;
  esac
done
if [ -n "$prev" ]; then
  echo "prod_gate: refusing: --profile without a value" >&2; exit 2
fi
case "$profile" in
  dev) exit 0 ;;
  prod) ;;
  *) echo "prod_gate: refusing: unknown run profile '$profile'" >&2; exit 2 ;;
esac
branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ "$branch" != "main" ]; then
  echo "prod_gate: refusing: prod profile requires the main branch (on '${branch:-unknown}')" >&2
  exit 2
fi
local_sha="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
remote_sha="$(git -C "$repo" ls-remote origin refs/heads/main 2>/dev/null | awk '{print $1}' || true)"
if [ -z "${remote_sha:-}" ]; then
  echo "prod_gate: refusing: origin/main is unverifiable (no remote?)" >&2; exit 2
fi
if [ -z "${local_sha:-}" ] || [ "$local_sha" != "$remote_sha" ]; then
  echo "prod_gate: refusing: HEAD ${local_sha:-unknown} != origin/main $remote_sha" >&2
  exit 2
fi
