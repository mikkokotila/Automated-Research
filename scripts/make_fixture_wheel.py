"""Build a tiny fixture wheel with stdlib only (no network, no build backend).

The wheel lets the disposable launcher demonstrate supported dependency
installation: the operator stages vetted wheels in a read-only wheelhouse
volume and the guest installs with `pip install --no-index
--find-links /wheels <name>`. No registry access is required or allowed.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

NAME = "fixture_pkg"
VERSION = "0.1"
WHEEL_NAME = f"{NAME}-{VERSION}-py3-none-any.whl"

INIT_SRC = '''"""Fixture package: proves guest dependency installation works."""


def answer() -> int:
    return 42
'''

METADATA = f"""Metadata-Version: 2.1
Name: {NAME}
Version: {VERSION}
Summary: Canary launcher fixture for offline dependency installation
"""

WHEEL_META = """Wheel-Version: 1.0
Generator: canary-make-fixture-wheel
Root-Is-Purelib: true
Tag: py3-none-any
"""


def _b64digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(dest_dir: str | Path) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dist_info = f"{NAME}-{VERSION}.dist-info"
    files = {
        f"{NAME}/__init__.py": INIT_SRC.encode(),
        f"{dist_info}/METADATA": METADATA.encode(),
        f"{dist_info}/WHEEL": WHEEL_META.encode(),
    }
    records = [(path, f"sha256={_b64digest(data)}", str(len(data))) for path, data in files.items()]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows([*records, (f"{dist_info}/RECORD", "", "")])
    files[f"{dist_info}/RECORD"] = buf.getvalue().encode()
    out = dest_dir / WHEEL_NAME
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the offline fixture wheel.")
    ap.add_argument("--out", required=True, help="directory to receive the .whl file")
    args = ap.parse_args()
    print(build_wheel(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
