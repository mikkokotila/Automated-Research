#!/usr/bin/env python3
"""Compare two local revisions in separate, offline Docker containers."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET


def translate(text: str, changes: list[list[str]]) -> str:
    text = text.replace("User-Agent", "HTTP_HEADER_PLACEHOLDER")
    for old, new in changes:
        def substitute(match, new=new):
            value = match.group(0)
            if value.isupper():
                return new.upper()
            return new[:1].upper() + new[1:] if value[:1].isupper() else new
        text = re.sub(re.escape(old), substitute, text, flags=re.IGNORECASE)
    return text.replace("HTTP_HEADER_PLACEHOLDER", "User-Agent")


def archive(root: Path, names: list[str], probe: Path) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name in sorted(names):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Not a regular input file: {path}")
            output.add(path, arcname=name, recursive=False)
        output.add(probe, arcname="contract_probe.py", recursive=False)
    return stream.getvalue()


def execute(label: str, payload: bytes, image: str, destination: Path) -> dict:
    command = (
        "mkdir -p /work; tar -xf - -C /work; cd /work; "
        "python -m pytest -q --junitxml=/tmp/results.xml; rc=$?; "
        "echo REPORT_BEGIN; cat /tmp/results.xml; echo REPORT_END; "
        "[ $rc -eq 0 ] || exit $rc; python contract_probe.py"
    )
    arguments = [
        "docker", "run", "--rm", "-i", "--network", "none",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--read-only",
        "--user", "1000:1000", "--pids-limit", "256", "--memory", "3g", "--cpus", "2",
        "--tmpfs", "/work:rw,mode=1777,size=512m", "--tmpfs", "/tmp:rw,mode=1777,size=512m",
        "-e", "HOME=/work", "-e", "PYTHONPATH=/work/src",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "OMP_NUM_THREADS=1",
        "-e", "OPENBLAS_NUM_THREADS=1", "--entrypoint", "/bin/sh", image, "-c", command,
    ]
    completed = subprocess.run(arguments, input=payload, capture_output=True, timeout=300)
    text = completed.stdout.decode("utf-8", errors="replace")
    (destination / f"{label}.log").write_bytes(completed.stdout + completed.stderr)
    if completed.returncode:
        raise RuntimeError(f"{label} failed: see {destination / (label + '.log')}")
    xml = text.split("REPORT_BEGIN\n", 1)[1].split("REPORT_END", 1)[0].strip()
    (destination / f"{label}-junit.xml").write_text(xml, encoding="utf-8")
    tests = sorted((element.get("classname"), element.get("name"), [child.tag for child in element])
                   for element in ET.fromstring(xml).iter("testcase"))
    result = json.loads(text.split("CONTRACT_RESULT=", 1)[1])
    return {"tests": tests, "contracts": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--image", required=True, help="One image with Python, Git and project/test dependencies")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    mapping = json.loads(args.mapping.read_text())
    paths = mapping["paths"]
    old_probe = args.baseline / ".migration" / "contract_probe.py"
    if not old_probe.exists():
        old_probe = args.baseline / "contract_probe.py"
    new_probe = args.candidate / "scripts" / "contract_probe.py"
    left = archive(args.baseline, list(paths), old_probe)
    right = archive(args.candidate, list(paths.values()), new_probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(execute, "baseline", left, args.image, args.out)
        b = pool.submit(execute, "candidate", right, args.image, args.out)
        baseline, candidate = a.result(), b.result()
    expected = json.loads(translate(json.dumps(baseline), mapping["replacements"]))
    actual = json.loads(json.dumps(candidate))
    test_match = sorted(expected["tests"]) == sorted(actual["tests"])
    mismatches = []
    for key in sorted(set(expected["contracts"]) | set(actual["contracts"])):
        x, y = expected["contracts"].get(key), actual["contracts"].get(key)
        if key.startswith("cli-") and isinstance(x, dict) and "stdout" in x:
            x = {k: re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v for k, v in x.items()}
            y = {k: re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v for k, v in y.items()}
        if x != y:
            mismatches.append(key)
    report = {
        "image": args.image, "tests_per_version": len(actual["tests"]),
        "test_outcomes_match": test_match, "contract_groups": len(actual["contracts"]),
        "contract_mismatches": mismatches, "contract_names": sorted(actual["contracts"]),
        "normalization": "Declared naming substitutions and CLI whitespace only; the probe fixes its clock.",
        "limitations": "Mocked external services; no live generation, real publishing, or universal equivalence claim.",
    }
    (args.out / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if test_match and not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
