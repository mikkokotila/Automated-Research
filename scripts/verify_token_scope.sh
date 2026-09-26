#!/bin/bash
# Prove GITHUB_TOKEN is scoped to this repo and nothing else. Read-only checks.
set -euo pipefail

: "${GITHUB_TOKEN:?set GITHUB_TOKEN first}"
OWN="${GITHUB_REPO:-mikkokotila/Canary}"
OTHER="${OTHER_REPO:-mikkokotila/kanava}"  # private sibling: must be invisible

code() { curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $GITHUB_TOKEN" "$1"; }

fail=0
c1=$(code "https://api.github.com/repos/$OWN");          echo "own repo ($OWN): $c1"
c2=$(code "https://api.github.com/repos/$OTHER");        echo "other repo ($OTHER): $c2"
me=$(curl -s -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user | grep -o '"login": *"[^"]*"' | head -n 1)
echo "identity: $me"

[ "$c1" = "200" ] || { echo "FAIL: cannot read own repo"; fail=1; }
[ "$c2" = "404" ] || { echo "FAIL: token sees beyond its repo ($c2)"; fail=1; }
[ "$fail" -eq 0 ] && echo "SCOPE OK: own repo only"
exit "$fail"
