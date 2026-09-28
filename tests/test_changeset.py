"""Build 13: structural diff validation, base binding, promotion policy."""
import json
import subprocess

import pytest

from canary import assess as assessmod
from canary import revise as revmod
from canary.changeset import (ChangesetError, check_policy, normalize_unified_diff,
                              parse_unified_diff, verify_in_disposable)
from canary.journal import Journal


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Explicit in-process trust seam; git repo for patch application."""
    monkeypatch.setattr(revmod, "require_revision_trust", lambda: None)
    r = tmp_path / "repo"
    (r / "src" / "canary").mkdir(parents=True)
    (r / "src" / "canary" / "foo.py").write_text("X = 1\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True, check=True)
    return r


def prop(target="src/canary/foo.py"):
    return assessmod.Proposal("p1", target, "bump", "r")


DIFF_OK = """diff --git a/src/canary/foo.py b/src/canary/foo.py
--- a/src/canary/foo.py
+++ b/src/canary/foo.py
@@ -1 +1 @@
-X = 1
+X = 2
"""


# --- valid path ---


def test_valid_fix_binds_base_and_hashes(repo, tmp_path):
    oc = revmod.apply_one(repo, prop(), DIFF_OK, ["true"], Journal(),
                          assessment_id="a1", assess_dir=tmp_path)
    assert oc.kept and (repo / "src/canary/foo.py").read_text() == "X = 2\n"
    assert oc.base_rev and oc.manifest["base_rev"] == oc.base_rev
    assert oc.manifest["files"][0]["path"] == "src/canary/foo.py"
    assert oc.assessment_id == "a1"


def test_verify_sees_uncommitted_kept_adds(repo):
    """Follow-up diffs verify against kept adds, which land uncommitted."""
    (repo / "src" / "canary" / "newmod.py").write_text("Y = 1\n", encoding="utf-8")
    diff = ("--- a/src/canary/newmod.py\n+++ b/src/canary/newmod.py\n"
            "@@ -1 +1 @@\n-Y = 1\n+Y = 2\n")
    manifest = verify_in_disposable(repo, diff, parse_unified_diff(diff))
    assert manifest.files[0]["path"] == "src/canary/newmod.py"
    assert manifest.files[0]["old_sha"] != manifest.files[0]["new_sha"]


def test_request_diff_records_context_and_omissions(repo):
    record: dict = {}
    seen = {}

    class Muse:
        model = "m"

        def complete(self, system, user, max_tokens=8000):
            seen["user"] = user
            return DIFF_OK

    revmod.request_diff(prop(), Muse(), repo, record)
    assert record["base_rev"] and record["file_sha"] not in ("", "absent")
    assert "Base revision:" in seen["user"] and "File sha256:" in seen["user"]
    assert "X = 1" in seen["user"]  # real current context, not a guess


# --- adversarial table ---


def _diff(body: str) -> str:
    return body if body.startswith("diff --git") else DIFF_OK.split("---")[0] + body


ADVERSARIAL = [
    ("deletion via /dev/null",
     "diff --git a/src/canary/foo.py b/src/canary/foo.py\n"
     "deleted file mode 100644\n--- a/src/canary/foo.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-X = 1\n",
     "deletion"),
    ("quoted path",
     'diff --git "a/src/canary/foo.py" "b/src/canary/foo.py"\n--- "a/src/canary/foo.py"\n'
     '+++ "b/src/canary/foo.py"\n@@ -1 +1 @@\n-X = 1\n+X = 2\n',
     "quoted"),
    ("rename headers",
     "diff --git a/src/canary/foo.py b/src/canary/bar.py\nsimilarity index 90%\n"
     "rename from src/canary/foo.py\nrename to src/canary/bar.py\n",
     "rename"),
    ("a/b path alias",
     "diff --git a/src/canary/foo.py b/src/canary/foo.py\n--- a/src/canary/foo.py\n"
     "+++ b/src/canary/other.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n",
     "alias"),
    ("mode change",
     "diff --git a/src/canary/foo.py b/src/canary/foo.py\nold mode 100644\nnew mode 100755\n",
     "mode"),
    ("symlink mode",
     "diff --git a/src/canary/link b/src/canary/link\nnew file mode 120000\n"
     "--- /dev/null\n+++ b/src/canary/link\n@@ -0,0 +1 @@\n+target\n",
     "120000"),
    ("submodule gitlink",
     "diff --git a/src/canary/sub b/src/canary/sub\nnew file mode 160000\n"
     "--- /dev/null\n+++ b/src/canary/sub\n@@ -0,0 +1 @@\n+Subproject commit abc\n",
     "160000"),
    ("binary marker",
     "diff --git a/src/canary/foo.py b/src/canary/foo.py\n"
     "Binary files a/src/canary/foo.py and b/src/canary/foo.py differ\n",
     "binary"),
    ("traversal",
     "diff --git a/src/canary/foo.py b/src/canary/foo.py\n--- a/src/canary/foo.py\n"
     "+++ b/src/canary/../../evil.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n",
     "escaping|alias"),
    ("tests target",
     DIFF_OK.replace("src/canary/foo.py", "tests/test_x.py"),
     "forbidden"),
    ("broker target",
     DIFF_OK.replace("src/canary/foo.py", "boundary/server.py"),
     "forbidden"),
    ("launcher target",
     DIFF_OK.replace("src/canary/foo.py", "scripts/container_run.sh"),
     "forbidden"),
    ("gate file target",
     DIFF_OK.replace("src/canary/foo.py", "src/canary/changeset.py"),
     "forbidden"),
    ("trust doc target",
     DIFF_OK.replace("src/canary/foo.py", "docs/TRUST_BOUNDARY.md"),
     "forbidden"),
    ("malformed hunk",
     DIFF_OK.replace("@@ -1 +1 @@", "@@ nonsense @@"),
     "hunk"),
    ("empty diff", "", "empty"),
    ("no sections", "just prose\n", "no file sections|no path"),
]


@pytest.mark.parametrize("name,diff,reason", ADVERSARIAL)
def test_adversarial_diffs_rejected_without_touching_tree(repo, name, diff, reason):
    import re as _re

    before = (repo / "src/canary/foo.py").read_text()
    oc = revmod.apply_one(repo, prop(), diff, ["true"], Journal())
    assert not oc.applied and not oc.kept, name
    assert _re.search(reason, oc.reason), f"{name}: {oc.reason}"
    assert (repo / "src/canary/foo.py").read_text() == before
    assert revmod.git(repo, "status", "--porcelain", "--", "src", "tests").strip() == ""


def test_multi_file_all_allowed_applies(repo, tmp_path):
    (repo / "src" / "canary" / "bar.py").write_text("Y = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "bar"], cwd=repo, capture_output=True, check=True)
    two = DIFF_OK + DIFF_OK.replace("foo.py", "bar.py").replace("X = 1", "Y = 1").replace("X = 2", "Y = 2")
    oc = revmod.apply_one(repo, prop(), two, ["true"], Journal(), assess_dir=tmp_path)
    assert oc.kept and len(oc.manifest["files"]) == 2


def test_multi_file_one_forbidden_rejects_all(repo):
    two = DIFF_OK + DIFF_OK.replace("src/canary/foo.py", "tests/test_x.py")
    oc = revmod.apply_one(repo, prop(), two, ["true"], Journal())
    assert not oc.applied and "forbidden" in oc.reason


def test_oversize_rejected(repo):
    big = DIFF_OK + "\n".join(f"# pad {i}" for i in range(400))
    oc = revmod.apply_one(repo, prop(), big, ["true"], Journal())
    assert not oc.applied and "too large" in oc.reason


# --- staleness and provenance ---


def test_source_mutation_between_validation_and_apply_rejects(repo):
    from canary import promote as promotemod

    store = promotemod.Store(repo / "runs" / "promotions")
    store.init_from_worktree(repo)
    cand = promotemod.evaluate(store, prop(), DIFF_OK, ["true"], "a1", Journal())
    assert cand.state == "testing" and cand.test["exit"] == 0
    # Tamper the base snapshot between evaluation and promotion.
    (store.revs / cand.base_rev / "tree" / "src" / "canary" / "foo.py").write_text(
        "X = 999\n", encoding="utf-8")
    with pytest.raises(promotemod.PromotionError, match="drifted"):
        promotemod.promote(store, cand, Journal())
    assert store.latest_rev() == cand.base_rev  # accepted untouched
    assert store.load_candidate(cand.id).state == "rejected"


def test_stale_context_diff_rejected(repo):
    stale = DIFF_OK.replace("-X = 1", "-X = 999")
    oc = revmod.apply_one(repo, prop(), stale, ["true"], Journal())
    assert not oc.applied
    assert (repo / "src/canary/foo.py").read_text() == "X = 1\n"


def test_candidate_provenance_redacts_secrets(repo, tmp_path):
    leaky = DIFF_OK.replace("+X = 2", '+X = 2  # token ghp_abcdef1234567890abcdef1234567890ab')
    oc = revmod.apply_one(repo, prop(), leaky, ["true"], Journal(), assessment_id="a9",
                          assess_dir=tmp_path, context_record={"base_rev": "abc123"})
    assert oc.kept
    saved = json.loads((tmp_path / "candidates" / "p1.json").read_text())
    assert saved["assessment_id"] == "a9" and saved["manifest"]["files"]
    assert "ghp_abcdef" not in saved["raw_diff"]
    assert "change" in saved and saved["context"]["base_rev"] == "abc123"


def test_policy_matrix():
    allowed = revmod.target_allowed("src/canary/rank.py")
    assert allowed is None
    for bad in ("tests/t.py", ".github/w.yml", "boundary/gateway.py", "scripts/x.sh",
                "recovery/r.md", "validation/v.md", "Dockerfile", "uv.lock", "k.pem",
                ".env", "src/canary/revise.py", "src/canary/changeset.py",
                "src/canary/muse_client.py", "pyproject.toml", "docs/TRUST_BOUNDARY.md",
                "docs/TOKEN_BOUNDARY.md", "README.md", "/abs", "a/../b"):
        assert revmod.target_allowed(bad), bad


def test_parser_and_policy_agree_on_ops():
    ops = parse_unified_diff(DIFF_OK)
    assert [(op.new_path, op.op) for op in ops] == [("src/canary/foo.py", "modified")]
    check_policy(ops, revmod.target_allowed)  # raises nothing
    with pytest.raises(ChangesetError, match="empty"):
        parse_unified_diff("   ")


# --- hunk-count normalization (keep1c yield) ---


class QueuedCompleter:
    model = "queued"

    def __init__(self, *texts):
        self.texts = list(texts)

    def complete(self, system, user, max_tokens=8000):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]


DIFF_MISCOUNTED = ("diff --git a/src/canary/foo.py b/src/canary/foo.py\n"
                   "--- a/src/canary/foo.py\n"
                   "+++ b/src/canary/foo.py\n"
                   "@@ -1,5 +1,9 @@\n"
                   "-X = 1\n"
                   "+X = 2\n")


def test_normalize_repairs_counts_and_git_accepts(repo):
    with pytest.raises(ChangesetError, match="corrupt patch"):
        verify_in_disposable(repo, DIFF_MISCOUNTED,
                             parse_unified_diff(DIFF_MISCOUNTED))
    fixed = normalize_unified_diff(DIFF_MISCOUNTED)
    assert "@@ -1,1 +1,1 @@" in fixed
    manifest = verify_in_disposable(repo, fixed, parse_unified_diff(fixed))
    assert manifest.files[0]["new_sha"] != manifest.files[0]["old_sha"]
    # content lines pass through byte-identical; only the header changes
    assert fixed.replace("@@ -1,1 +1,1 @@", "@@ -1,5 +1,9 @@") == DIFF_MISCOUNTED


def test_normalize_counts_empty_lines_as_context(repo):
    target = repo / "src" / "canary" / "foo.py"
    target.write_text("a\n\nb\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "empty"], cwd=repo,
                   capture_output=True, check=True)
    diff = ("--- a/src/canary/foo.py\n+++ b/src/canary/foo.py\n"
            "@@ -1,7 +1,7 @@\n a\n\n-b\n+c\n")
    fixed = normalize_unified_diff(diff)
    assert "@@ -1,3 +1,3 @@" in fixed
    verify_in_disposable(repo, fixed, parse_unified_diff(fixed))


def test_normalize_rejects_structural_garbage():
    bad_body = ("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n"
                "? not a hunk line\n")
    with pytest.raises(ChangesetError, match="unexpected line"):
        normalize_unified_diff(bad_body)
    with pytest.raises(ChangesetError, match="empty hunk"):
        normalize_unified_diff("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n")
    with pytest.raises(ChangesetError, match="malformed hunk header"):
        normalize_unified_diff("@@ -x +y @@\n foo\n")


def test_applicable_diff_normalizes_first_attempt(repo, tmp_path):
    client = QueuedCompleter(DIFF_MISCOUNTED)
    attempts: list = []
    diff = revmod.request_applicable_diff(prop(), client, repo, {},
                                          Journal(), attempts_out=attempts)
    assert "@@ -1,1 +1,1 @@" in diff  # repaired, applied, no retry burned
    assert len(attempts) == 1
    assert attempts[0]["error"] == ""
    assert attempts[0]["raw"] == DIFF_MISCOUNTED
    assert attempts[0]["normalized"] == diff


def test_failed_attempts_persist_redacted(repo, tmp_path):
    garbage = ("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n"
               "? boom MUSE_API_KEY = sk-live\n")
    client = QueuedCompleter(garbage)
    attempts: list = []
    with pytest.raises(ChangesetError, match="no applicable diff"):
        revmod.request_applicable_diff(prop(), client, repo, {}, Journal(),
                                       max_attempts=2, attempts_out=attempts)
    assert len(attempts) == 2
    assert all(a["error"] for a in attempts)
    path = revmod.save_candidate(tmp_path, prop(), "", None, {}, "a1",
                                 "diff-failed", "nope", attempts)
    saved = json.loads(path.read_text())
    assert len(saved["attempts"]) == 2
    assert saved["attempts"][0]["normalized"] is False
    assert "sk-live" not in saved["attempts"][0]["raw_diff"]
    assert "[REDACTED:muse_key]" in saved["attempts"][0]["raw_diff"]
