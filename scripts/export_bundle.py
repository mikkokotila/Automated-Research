"""Copy only bounded regular files from a worker tar stream into a NEW directory."""
from pathlib import Path, PurePosixPath
import sys
import tarfile


def export(stream, destination):
    root = Path(destination)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    total = count = 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for item in archive:
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or item.issym() or item.islnk():
                raise ValueError("Unsafe archive path or link")
            if item.isdir():
                (root / path).mkdir(parents=True, exist_ok=True)
                continue
            if not item.isfile() or item.size < 0:
                raise ValueError("Only regular files may be exported")
            total += item.size
            count += 1
            if total > 200_000_000 or count > 5000:
                raise ValueError("Export limit exceeded")
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(item)
            if source is None:
                raise ValueError("Missing file data")
            with dest.open("xb") as out:
                while data := source.read(65536):
                    out.write(data)
    return count


if __name__ == "__main__":
    print("Exported", export(sys.stdin.buffer, sys.argv[1]), "files")
