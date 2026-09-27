"""Scan an exported bundle for credentials before a maintainer imports it.

Best-effort tripwire for known-prefix secrets, plus manifest checksum
verification (detects post-export tampering). Exit 0 when clean, 1 on
findings or manifest mismatch. Never executes guest content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from canary.redact import find_secrets

MAX_SCAN_BYTES = 5_000_000
MANIFEST_NAME = "manifest.canary.json"


def scan_file(path: Path) -> tuple[list[str], bool]:
    """Returns (matched pattern names, skipped_for_size)."""
    if path.stat().st_size > MAX_SCAN_BYTES:
        return [], True
    return find_secrets(path.read_bytes()), False


def verify_manifest(root: Path) -> list[str]:
    """Returns a list of mismatch descriptions (empty when valid)."""
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return ["missing manifest"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ["unreadable manifest"]
    problems = []
    for entry in manifest.get("files", []):
        target = root / entry["path"]
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            problems.append(f"missing: {entry['path']}")
            continue
        if digest != entry.get("sha256"):
            problems.append(f"checksum mismatch: {entry['path']}")
    return problems


def scan_export(root: str | Path) -> dict:
    root = Path(root)
    findings: list[dict] = []
    scanned = skipped = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        matched, too_big = scan_file(path)
        if too_big:
            skipped += 1
            continue
        scanned += 1
        findings.extend({"file": str(path.relative_to(root)), "pattern": name} for name in matched)
    manifest_problems = verify_manifest(root)
    return {"clean": not findings and not manifest_problems,
            "findings": findings, "manifest_problems": manifest_problems,
            "scanned": scanned, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan an export bundle for credentials.")
    ap.add_argument("directory", help="exported bundle directory")
    args = ap.parse_args()
    report = scan_export(args.directory)
    print(json.dumps(report, indent=2))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
