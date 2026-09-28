#!/usr/bin/env python3
"""Fail when the dashboard's inline script does not parse.

A one-backtick syntax error once shipped and left every browser with an
empty table (the script dies before first render, silently). node is
present on CI runners; absence here is a loud skip, never a pass-by-miss.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UI = Path(__file__).resolve().parent / "dashboard" / "index.html"


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("check_dashboard_js: node not found; refusing to pass blind", file=sys.stderr)
        return 2
    scripts = re.findall(r"<script>(.*?)</script>", UI.read_text(encoding="utf-8"), re.S)
    if not scripts:
        print("check_dashboard_js: no inline script found", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(scripts[0])
        probe = fh.name
    r = subprocess.run([node, "--check", probe], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:], file=sys.stderr)
        return 1
    print(f"check_dashboard_js: {UI.name} parses ({len(scripts[0])} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
