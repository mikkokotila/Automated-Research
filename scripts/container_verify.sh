#!/bin/bash
# Prove the container cannot reach the host. Fails loudly on any broken property.
set -euo pipefail
cd "$(dirname "$0")/.."

IMG="${IMG:-autoresearch:local}"
docker build -q -t "$IMG" .

run() {
  docker run --rm \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --pids-limit 256 -m 4g --cpus 2 \
    --read-only --tmpfs /tmp:rw,size=512m --tmpfs /work:rw,size=2g \
    "$IMG" bash -c "$1"
}

pass=0; fail=0
check() { # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS: $1"; pass=$((pass+1)); else echo "FAIL: $1 (want [$2] got [$3])"; fail=$((fail+1)); fi
}

# Host-side proof: inspect a live container for bind mounts (host paths).
CID=""
trap 'if [ -n "$CID" ]; then docker rm -f "$CID" >/dev/null 2>&1 || true; fi' EXIT
CID=$(docker run -d --cap-drop=ALL --security-opt=no-new-privileges:true \
  --pids-limit 256 -m 4g --cpus 2 \
  --read-only --tmpfs /tmp:rw,size=512m --tmpfs /work:rw,size=2g \
  "$IMG" sleep 30)
BINDS=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}BIND:{{.Source}} {{end}}{{end}}' "$CID")
check "no bind mounts (host side)" "" "$BINDS"

check "no linux capabilities" "0000000000000000" "$(run 'grep CapEff /proc/self/status | awk "{print \$2}"')"
check "no-new-privileges set" "1" "$(run 'grep NoNewPrivs /proc/self/status | awk "{print \$2}"')"
check "no docker socket" "absent" "$(run 'test -e /var/run/docker.sock && echo present || echo absent')"
check "rootfs read-only" "denied" "$(run 'touch /host-escape-test 2>/dev/null && echo writable || echo denied')"
check "workspace writable" "ok" "$(run 'touch /work/ok && rm /work/ok && echo ok')"
check "no host mounts" "" "$(run 'mount | grep -vE " /etc/(hostname|hosts|resolv.conf) " | grep -iE "host|/Users/|/home/|/host_mnt|docker.sock" || true')"
check "own pid namespace" "small" "$(run 'test $(ls -d /proc/[0-9]* 2>/dev/null | wc -l) -lt 30 && echo small || echo big')"
check "no secrets in env" "" "$(run 'env | grep -iE "TOKEN|API_KEY" || true')"
check "github egress" "200" "$(run 'curl -s -o /dev/null -w "%{http_code}" --max-time 20 https://api.github.com/zen || echo 000')"
check "muse egress (401=no key, yet reachable)" "401" "$(run 'curl -s -o /dev/null -w "%{http_code}" --max-time 20 https://api.meta.ai/v1/models || echo 000')"

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
