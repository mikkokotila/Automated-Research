#!/bin/bash
# Run autoresearch in a hardened container. No host mounts: outputs come out via `docker cp`.
# Usage: ./scripts/container_run.sh loop "question?" --self-improve --repo /work --out /work/out
set -euo pipefail
cd "$(dirname "$0")/.."

IMG="${IMG:-autoresearch:local}"
NAME="ar-run-$(date +%s)"
VOL="ar-out-$NAME"
OUT="${OUT:-./container-out}"

# Self-improvement demands a clean tree; a dirty image would abort every patch.
# Pass ALLOW_DIRTY=1 only for research runs that never self-patch.
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git status --porcelain -- src tests runs 2>/dev/null)" ]; then
  echo "refusing: uncommitted changes under src/ tests/ runs/ (set ALLOW_DIRTY=1 to override)" >&2
  exit 2
fi

docker build -q -t "$IMG" --label "gitsha=$(git rev-parse HEAD 2>/dev/null || echo unknown)" .
docker volume create "$VOL" >/dev/null

set +e
docker run --name "$NAME" \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --pids-limit 256 -m 4g --cpus 2 \
  --read-only --tmpfs /tmp:rw,size=512m --tmpfs /work:rw,size=2g \
  -v "$VOL:/work/out" \
  -e MUSE_API_KEY -e GITHUB_TOKEN -e GITHUB_REPO \
  -e SEMANTIC_SCHOLAR_API_KEY -e OPENALEX_MAILTO \
  "$IMG" autoresearch "$@"
RC=$?
set -e

rm -rf "$OUT"; mkdir -p "$OUT"
# Extract via tar pipe: outputs leave the volume without any host bind mount.
if ! docker run --rm -v "$VOL:/from" --entrypoint tar "$IMG" cf - -C /from . 2>/dev/null | tar xf - -C "$OUT"; then
  echo "(no /work/out produced)"
fi
docker rm "$NAME" >/dev/null
docker volume rm "$VOL" >/dev/null
echo "exit=$RC out=$OUT"
exit "$RC"
