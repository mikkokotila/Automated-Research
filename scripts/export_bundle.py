"""Copy only bounded regular files from a worker tar stream into a NEW directory.

Host-side boundary control: rejects traversal, absolute paths, links,
devices, oversized payloads, collisions, and the reserved manifest name.
Never executes, renders, or unpickles guest content. Writes a manifest
with per-file checksums so a maintainer can verify before importing.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

MANIFEST_NAME = "manifest.canary.json"  # reserved: guests must not ship it
MAX_TOTAL_BYTES = 200_000_000
MAX_FILES = 5000


def export(stream, destination):
    root = Path(destination)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    total = count = 0
    files = []
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for item in archive:
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or item.issym() or item.islnk():
                raise ValueError("Unsafe archive path or link")
            if path.name == MANIFEST_NAME or MANIFEST_NAME in path.parts:
                raise ValueError("Reserved manifest name")
            if item.isdir():
                (root / path).mkdir(parents=True, exist_ok=True)
                continue
            if not item.isfile() or item.size < 0:
                raise ValueError("Only regular files may be exported")
            total += item.size
            count += 1
            if total > MAX_TOTAL_BYTES or count > MAX_FILES:
                raise ValueError("Export limit exceeded")
            dest = root / path
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(item)
                if source is None:
                    raise ValueError("Missing file data")
                digest = hashlib.sha256()
                try:
                    out = dest.open("xb")
                except FileExistsError:
                    raise ValueError(f"Export collision: {item.name}")
                except IsADirectoryError:
                    raise ValueError(f"Export collision: {item.name}")
                with out:
                    while data := source.read(65536):
                        digest.update(data)
                        out.write(data)
            except (FileExistsError, IsADirectoryError):
                raise ValueError(f"Export collision: {item.name}")
            files.append({"path": item.name, "sha256": digest.hexdigest(), "size": item.size})
    manifest = {"count": count, "total_bytes": total, "files": files}
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return count


def main(argv: list[str] | None = None) -> int:
    """CLI: stream a worker tar from stdin into a NEW directory.

    Argument errors exit 2 with no side effects. A failed export cleans
    up the directory it created, so a bad invocation never litters the
    tree (a stray dir once poisoned the worker-image build context).
    """
    ap = argparse.ArgumentParser(
        prog="export_bundle.py",
        description="Copy bounded regular files from a worker tar stream "
                    "(stdin) into a NEW directory.")
    ap.add_argument("destination", help="new directory to create for the export")
    args = ap.parse_args(argv)
    dest = Path(args.destination)
    if dest.exists():
        print(f"export_bundle: error: destination already exists: {dest}",
              file=sys.stderr)
        return 1
    try:
        count = export(sys.stdin.buffer, dest)
    except FileExistsError:
        # Another writer won the race: the dir is not ours; keep it.
        print(f"export_bundle: error: destination already exists: {dest}",
              file=sys.stderr)
        return 1
    except (ValueError, tarfile.TarError, OSError) as exc:
        shutil.rmtree(dest, ignore_errors=True)  # only we could have made it
        print(f"export_bundle: error: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {count} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
