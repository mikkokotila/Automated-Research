#!/bin/bash
# Owner service: runs dashboard daemon (UI + run control plane).
#
# Localhost-only HTTP on 127.0.0.1:8789. The daemon itself spends nothing;
# runs are launched only by explicit UI/CLI action. Runs survive daemon
# restarts (detached); the registry reconciles on every listing.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
LABEL="com.canary.runsd"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${CANARY_RUNS_PORT:-8789}"
CANARY="$ROOT/.venv/bin/canary"

case "${1:-}" in
  start)
    test -x "$CANARY" || { echo "Missing $CANARY; run: uv sync" >&2; exit 2; }
    command -v docker >/dev/null || { echo "docker not on PATH" >&2; exit 2; }
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      echo "runsd already loaded; use '$0 stop' first to reinstall" >&2; exit 2
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/runs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$CANARY</string><string>runs</string><string>serve</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$ROOT/runs/runsd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/runs/runsd.err.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
    launchctl bootstrap "gui/$UID" "$PLIST"
    for _ in $(seq 1 30); do
      curl -fsS "http://127.0.0.1:$PORT/api/runs" >/dev/null 2>&1 && break
      sleep 1
    done
    curl -fsS "http://127.0.0.1:$PORT/api/runs" >/dev/null
    echo "runsd serving at http://127.0.0.1:$PORT/ (persists across reboots; stop: $0 stop)"
    ;;
  stop)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "runsd stopped and uninstalled from LaunchAgents"
    ;;
  status)
    launchctl print "gui/$UID/$LABEL" 2>/dev/null | head -8 || echo "not loaded"
    curl -fsS "http://127.0.0.1:$PORT/api/runs" >/dev/null 2>&1 \
      && echo "API reachable at http://127.0.0.1:$PORT/" \
      || echo "API unreachable at http://127.0.0.1:$PORT/"
    ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
