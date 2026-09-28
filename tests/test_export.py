"""Build 04: export only bounded inert files, with checksums, no escapes."""
import hashlib
import io
import json
import tarfile

import pytest

from scripts.export_bundle import MANIFEST_NAME, export


def _archive(entries):
    """entries: list of (name, bytes|type-marker)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            if isinstance(payload, bytes):
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:  # (type, linkname?)
                info.type, info.linkname = payload
                tar.addfile(info)
    buf.seek(0)
    return buf


def test_export_rejects_devices(tmp_path):
    for index, dtype in enumerate((tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE)):
        with pytest.raises(ValueError):
            export(_archive([("dev", (dtype, ""))]), tmp_path / f"dev{index}")


def test_export_rejects_reserved_manifest_name(tmp_path):
    with pytest.raises(ValueError):
        export(_archive([(MANIFEST_NAME, b"forged")]), tmp_path / "m")


def test_export_rejects_collisions_as_value_error(tmp_path):
    with pytest.raises(ValueError):  # never a bare FileExistsError
        export(_archive([("a.txt", b"1"), ("a.txt", b"2")]), tmp_path / "c")
    with pytest.raises(ValueError):
        export(_archive([("sub", (tarfile.DIRTYPE, "")), ("sub", b"file")]),
               tmp_path / "c2")


def test_export_writes_benign_tree_with_manifest(tmp_path):
    out = tmp_path / "out"
    count = export(_archive([("r/report.md", b"# hi"), ("run.json", b"{}")]), out)
    assert count == 2
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["count"] == 2 and manifest["total_bytes"] == 6
    by_path = {e["path"]: e for e in manifest["files"]}
    assert by_path["r/report.md"]["sha256"] == hashlib.sha256(b"# hi").hexdigest()
    assert by_path["run.json"]["size"] == 2
    assert (out / "r" / "report.md").read_text() == "# hi"


def test_export_still_rejects_traversal_absolute_and_links(tmp_path):
    for index, entry in enumerate((
            ("../outside", b"x"), ("/absolute", b"x"),
            ("link", (tarfile.SYMTYPE, "/tmp")),
            ("hard", (tarfile.LNKTYPE, "run.json")))):
        with pytest.raises(ValueError):
            export(_archive([entry]), tmp_path / str(index))


def test_scanner_flags_planted_credentials_and_passes_clean(tmp_path):
    from scripts.scan_export import scan_export

    dirty = tmp_path / "dirty"
    export(_archive([("notes.md", b"key: MUSE_API_KEY = fixture-planted"),
                     ("id_rsa", b"-----BEGIN OPENSSH PRIVATE KEY-----\nx")]), dirty)
    report = scan_export(dirty)
    assert not report["clean"]
    assert {f["pattern"] for f in report["findings"]} == {"muse_key", "private_key"}

    clean = tmp_path / "clean"
    export(_archive([("notes.md", b"nothing secret here")]), clean)
    report = scan_export(clean)
    assert report["clean"] and report["scanned"] == 2  # file + manifest


def test_scanner_detects_manifest_tampering(tmp_path):
    from scripts.scan_export import scan_export

    out = tmp_path / "out"
    export(_archive([("run.json", b"{}")]), out)
    (out / "run.json").write_text('{"tampered": true}')
    report = scan_export(out)
    assert not report["clean"]
    assert report["manifest_problems"] == ["checksum mismatch: run.json"]


def test_scanner_fails_closed_on_skipped_oversize_file(tmp_path):
    from scripts.scan_export import MAX_SCAN_BYTES, scan_export

    out = tmp_path / "out"
    blob = b"x" * (MAX_SCAN_BYTES + 1) + b"MUSE_API_KEY = hidden-in-big-file"
    export(_archive([("big.bin", blob)]), out)
    report = scan_export(out)
    assert report["skipped"] == 1
    assert not report["clean"]  # unscanned content is not a clean bill


def test_scanner_fails_closed_on_symlink(tmp_path):
    import json

    from scripts.scan_export import scan_export

    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "outside.txt").write_bytes(b"MUSE_API_KEY = hidden-via-link")
    (out / "notes.txt").symlink_to(tmp_path / "outside.txt")
    (out / MANIFEST_NAME).write_text(json.dumps({"files": []}))
    report = scan_export(out)
    assert report["symlinked"] == ["notes.txt"]
    assert not report["clean"]  # unscanned content is not a clean bill


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


def test_export_cli_help_prints_usage_and_creates_nothing(tmp_path, capsys):
    from scripts.export_bundle import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_export_cli_rejects_bad_argv_without_side_effects(tmp_path, capsys):
    from scripts.export_bundle import main

    for argv in ([], ["a", "b"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == []


def test_export_cli_nontar_stdin_fails_clean_with_one_line_error(tmp_path, monkeypatch, capsys):
    from scripts import export_bundle

    monkeypatch.setattr("sys.stdin", _FakeStdin(b"this is not a tar stream"))
    rc = export_bundle.main([str(tmp_path / "dest")])
    assert rc == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("export_bundle: error:") and "\n" not in err
    assert not (tmp_path / "dest").exists()  # failed export leaves nothing


def test_export_cli_refuses_existing_destination(tmp_path, monkeypatch, capsys):
    from scripts import export_bundle

    (tmp_path / "dest").mkdir()
    monkeypatch.setattr("sys.stdin", _FakeStdin(b""))
    rc = export_bundle.main([str(tmp_path / "dest")])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_export_cli_roundtrip_from_stdin(tmp_path, monkeypatch, capsys):
    from scripts import export_bundle

    monkeypatch.setattr("sys.stdin", _FakeStdin(_archive([("r.md", b"# hi")]).read()))
    rc = export_bundle.main([str(tmp_path / "dest")])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "Exported 1 files"
    assert (tmp_path / "dest" / MANIFEST_NAME).is_file()
