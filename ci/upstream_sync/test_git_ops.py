"""Consumer-level regression tests; every transition uses real temporary Git repos."""

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from ci.upstream_sync.git_ops import SyncError, materialize, prepare, write_patch


_FIXTURE_PATHS = tuple(
    f"tests/{side}/configs/{name}/sample.rs"
    for side in ("source", "target")
    for name in (
        "blank_lines_before_control_flow_statements",
        "blank_lines_after_control_flow_statements",
        "blank_lines_between_items",
    )
)


@dataclass(frozen=True)
class _Link:
    target: str


@dataclass(frozen=True)
class _Case:
    source: Path
    base: str
    upstream: str


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-upstream-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "home").mkdir()
        self.env = {
            "PATH": os.defpath,
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": "1700000000 +0000",
            "GIT_COMMITTER_DATE": "1700000000 +0000",
        }
        self.serial = 0

    def git(self, repo, *args, data=None, check=True):
        result = subprocess.run(
            [
                "git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
                "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false", *args,
            ],
            env=self.env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result

    def oid(self, repo, revision="HEAD"):
        return self.git(repo, "rev-parse", revision).stdout.decode("ascii").strip()

    def files(self, repo, updates):
        for name, value in updates.items():
            path = repo / name
            if path.is_symlink():
                path.unlink()
            if value is None:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                continue
            if path.is_dir():
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, _Link):
                path.unlink(missing_ok=True)
                path.symlink_to(value.target)
            else:
                path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)

    def commit(self, repo, message):
        self.git(repo, "add", "--all")
        self.git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
        return self.oid(repo)

    def case(self, *, common=None, fork=None, upstream=None, unrelated=False, unchanged=False):
        self.serial += 1
        source = self.root / f"source-{self.serial}"
        source.mkdir()
        self.git(source, "init", "--quiet", "--initial-branch=main")
        initial = {
            "shared.txt": "base\n", "stable.txt": "untouched\n", "FORK.md": "fork policy\n",
            **{path: "fn fixture() {}\n" for path in _FIXTURE_PATHS},
        }
        initial.update(common or {})
        self.files(source, initial)
        self.commit(source, "common ancestor")
        self.git(source, "branch", "upstream")
        self.files(source, {"shared.txt": "fork\n"} if fork is None else fork)
        base = self.commit(source, "fork changes")
        if unrelated:
            self.git(source, "checkout", "--quiet", "--orphan", "unrelated")
            self.git(source, "rm", "-r", "-f", "--ignore-unmatch", ".")
            self.files(source, {"unrelated.txt": "different history\n"})
        else:
            self.git(source, "checkout", "--quiet", "upstream")
            self.files(source, {"shared.txt": "upstream\n", "upstream.txt": "automatic\n"} if upstream is None else upstream)
        upstream_id = self.oid(source) if unchanged else self.commit(source, "upstream changes")
        return _Case(source, base, upstream_id)

    def clone(self, case):
        self.serial += 1
        destination = self.root / f"clone-{self.serial}"
        self.git(self.root, "clone", "--quiet", "--no-local", str(case.source), str(destination))
        return destination

    def failure(self, expected, operation, *args):
        with self.assertRaises(SyncError) as caught:
            operation(*args)
        self.assertIn(caught.exception.code, (expected,) if isinstance(expected, str) else expected)
        return caught.exception

    def unchecked_patch(self, repo, base, destination, *paths):
        self.git(repo, "add", "--all")
        destination.write_bytes(self.git(
            repo, "diff", "--cached", "--binary", "--full-index", "--no-renames", base, "--", *paths
        ).stdout)
        return destination

    def test_clean_upstream_marker_fixture_is_not_mistaken_for_an_unresolved_merge(self):
        content = "<<<<<<< parser fixture\n=======\n>>>>>>> literal content\n"
        case = self.case(fork={"fork.txt": "fork\n"}, upstream={"marker-fixture.rs": content})
        repo = self.clone(case)
        self.assertEqual(prepare(repo, case.base, case.upstream)["kind"], "clean")
        patch = self.root / "marker-fixture.patch"
        write_patch(repo, case.base, case.upstream, patch)
        destination = self.clone(case)
        materialize(destination, case.base, case.upstream, patch)
        self.assertEqual((destination / "marker-fixture.rs").read_text(), content)

    def test_ancestry_only_merge_records_upstream_without_changing_the_tree(self):
        case = self.case(fork={}, upstream={})
        repo = self.clone(case)
        self.assertEqual(prepare(repo, case.base, case.upstream)["kind"], "clean")
        patch = self.root / "ancestry.patch"
        write_patch(repo, case.base, case.upstream, patch)
        destination = self.clone(case)
        result = materialize(destination, case.base, case.upstream, patch)
        parents = self.git(destination, "show", "--no-patch", "--format=%P", result["commit"]).stdout.split()
        self.assertEqual(parents, [case.base.encode(), case.upstream.encode()])
        self.assertEqual(self.oid(destination, "HEAD^{tree}"), self.oid(destination, case.base + "^{tree}"))

    def assert_parents(self, repo, case, result):
        self.assertEqual(self.oid(repo), result["commit"])
        self.assertEqual(self.oid(repo, "HEAD^{tree}"), result["tree"])
        parents = self.git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.decode("ascii").split()[1:]
        self.assertEqual(parents, [case.base, case.upstream])
        for parent in parents:
            self.assertEqual(self.git(repo, "merge-base", "--is-ancestor", parent, "HEAD").returncode, 0)
        self.assertEqual(self.git(repo, "status", "--porcelain").stdout, b"")

    def test_clean_merge_complete_binary_patch_review_paths_and_two_parents(self):
        review = [".github/workflows/upstream.yml", "ci/check.sh", "release-plz.toml", "rust-toolchain.toml"]
        case = self.case(
            common={"image.bin": b"\x00old\xff"}, fork={"fork.txt": "fork addition\n"},
            upstream={"upstream.txt": "upstream addition\n", "image.bin": b"\x00new\xfe", **{path: "review required\n" for path in review}},
        )
        repo = self.clone(case)
        prepared = prepare(repo, case.base, case.upstream)
        self.assertEqual(prepared["kind"], "clean")
        self.assertEqual(prepared["conflicts"], [])
        self.assertEqual(prepared["review_paths"], sorted(review))
        self.assertEqual(self.oid(repo), case.base)
        patch = self.root / "candidate.patch"
        exported = write_patch(repo, case.base, case.upstream, patch)
        candidate = self.clone(case)
        result = materialize(candidate, case.base, case.upstream, patch)
        self.assertEqual(result["tree"], exported["tree"])
        self.assertEqual(result["kind"], "clean")
        self.assertEqual(result["review_paths"], sorted(review))
        self.assertEqual((candidate / "image.bin").read_bytes(), b"\x00new\xfe")
        self.assertEqual((candidate / "fork.txt").read_text(), "fork addition\n")
        self.assertEqual((candidate / "FORK.md").read_text(), "fork policy\n")
        for path in _FIXTURE_PATHS:
            self.assertEqual((candidate / path).read_text(), "fn fixture() {}\n")
        self.assert_parents(candidate, case, result)
        again = materialize(self.clone(case), case.base, case.upstream, patch)
        self.assertEqual(again["commit"], result["commit"])

    def test_content_conflict_resolution_round_trip(self):
        case = self.case()
        repo = self.clone(case)
        prepared = prepare(repo, case.base, case.upstream)
        self.assertEqual(prepared["kind"], "conflict")
        self.assertEqual(prepared["conflicts"], ["shared.txt"])
        self.assertTrue(self.git(repo, "ls-files", "--unmerged").stdout)
        (repo / "shared.txt").write_text("combined fork and upstream\n")
        patch = self.root / "resolved.patch"
        exported = write_patch(repo, case.base, case.upstream, patch)
        self.assertFalse(self.git(repo, "ls-files", "--unmerged").stdout)
        candidate = self.clone(case)
        result = materialize(candidate, case.base, case.upstream, patch)
        self.assertEqual(result["conflicts"], ["shared.txt"])
        self.assertEqual(result["tree"], exported["tree"])
        self.assertEqual((candidate / "shared.txt").read_text(), "combined fork and upstream\n")
        self.assertEqual((candidate / "upstream.txt").read_text(), "automatic\n")
        self.assert_parents(candidate, case, result)

    def test_add_add_text_conflict_is_resolvable(self):
        case = self.case(common={"shared.txt": None}, fork={"shared.txt": "fork\n"}, upstream={"shared.txt": "upstream\n"})
        repo = self.clone(case)
        self.assertEqual(prepare(repo, case.base, case.upstream)["conflicts"], ["shared.txt"])
        (repo / "shared.txt").write_text("combined\n")
        patch = self.root / "add-add.patch"
        write_patch(repo, case.base, case.upstream, patch)
        candidate = self.clone(case)
        result = materialize(candidate, case.base, case.upstream, patch)
        self.assertEqual((candidate / "shared.txt").read_text(), "combined\n")
        self.assert_parents(candidate, case, result)

    def test_review_paths_include_unresolved_workflow_conflict(self):
        path = ".github/workflows/check.yml"
        case = self.case(common={path: "base\n"}, fork={path: "fork\n"}, upstream={path: "upstream\n"})
        result = prepare(self.clone(case), case.base, case.upstream)
        self.assertEqual(result["conflicts"], [path])
        self.assertEqual(result["review_paths"], [path])

    def test_prepare_discards_previous_resolution_and_untracked_files_without_following_symlinks(self):
        case = self.case(common={".gitignore": "scratch/\n"})
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        (repo / "shared.txt").write_text("discard this resolution\n")
        (repo / "scratch").mkdir()
        (repo / "scratch" / "model-output").write_text("discard\n")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("preserve\n")
        (repo / "escape").symlink_to(outside, target_is_directory=True)
        prepare(repo, case.base, case.upstream)
        self.assertIn("<<<<<<<", (repo / "shared.txt").read_text())
        self.assertFalse((repo / "scratch").exists())
        self.assertFalse((repo / "escape").exists())
        self.assertEqual((outside / "keep").read_text(), "preserve\n")
        self.assertTrue((repo / ".git" / "config").is_file())

    def test_unresolved_markers_rejected_even_after_staging(self):
        case = self.case()
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        self.git(repo, "add", "shared.txt")
        patch = self.root / "markers.patch"
        self.failure("unresolved-conflicts", write_patch, repo, case.base, case.upstream, patch)
        self.assertFalse(patch.exists())

    def test_repository_short_markers_cannot_hide_unresolved_conflicts(self):
        case = self.case(common={".gitattributes": "shared.txt conflict-marker-size=3\n"})
        repo = self.clone(case)
        self.assertEqual(prepare(repo, case.base, case.upstream)["conflicts"], ["shared.txt"])
        self.assertIn("<<<<<<<", (repo / "shared.txt").read_text())
        self.failure("unresolved-conflicts", write_patch, repo, case.base, case.upstream, self.root / "short.patch")

    def test_out_of_scope_resolution_rejected(self):
        case = self.case()
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        self.files(repo, {"shared.txt": "resolved\n", "stable.txt": "unauthorized edit\n"})
        patch = self.root / "unauthorized.patch"
        self.failure("out-of-scope", write_patch, repo, case.base, case.upstream, patch)
        self.assertFalse(patch.exists())

    def test_clean_merge_must_match_automatic_tree_even_on_upstream_changed_path(self):
        case = self.case(fork={"fork.txt": "fork\n"}, upstream={"upstream.txt": "automatic\n"})
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        (repo / "upstream.txt").write_text("not the automatic result\n")
        self.failure("out-of-scope", write_patch, repo, case.base, case.upstream, self.root / "clean.patch")

    def test_materialize_rejects_partial_resolution_patch_omitting_automatic_changes(self):
        case = self.case()
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        (repo / "shared.txt").write_text("resolved\n")
        patch = self.unchecked_patch(repo, case.base, self.root / "partial.patch", "shared.txt")
        self.failure("out-of-scope", materialize, self.clone(case), case.base, case.upstream, patch)

    def test_materialize_rejects_forged_extra_changes(self):
        case = self.case()
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        self.files(repo, {"shared.txt": "resolved\n", "stable.txt": "forged\n"})
        patch = self.unchecked_patch(repo, case.base, self.root / "forged.patch")
        self.failure("out-of-scope", materialize, self.clone(case), case.base, case.upstream, patch)

    def test_upstream_fork_contract_changes_require_manual_integration(self):
        for path in ("FORK.md", *_FIXTURE_PATHS):
            with self.subTest(path=path):
                case = self.case(fork={"fork.txt": "fork\n"}, upstream={path: "upstream changed contract\n"})
                self.failure("protected-path", prepare, self.clone(case), case.base, case.upstream)

    def test_protected_conflict_is_not_sent_for_resolution(self):
        case = self.case(fork={"FORK.md": "fork policy update\n"}, upstream={"FORK.md": "upstream policy update\n"})
        self.failure("protected-path", prepare, self.clone(case), case.base, case.upstream)

    def test_resolver_cannot_change_or_delete_preserved_fixtures(self):
        case = self.case()
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        (repo / "shared.txt").write_text("resolved\n")
        for path in ("FORK.md", *_FIXTURE_PATHS):
            with self.subTest(path=path):
                original = (repo / path).read_bytes()
                (repo / path).write_text("changed to get green\n")
                self.failure("protected-path", write_patch, repo, case.base, case.upstream, self.root / "protected.patch")
                (repo / path).write_bytes(original)
        shutil.rmtree(repo / "tests/source/configs/blank_lines_between_items")
        self.failure("protected-path", write_patch, repo, case.base, case.upstream, self.root / "deleted-fixture.patch")

    def test_materialize_independently_rejects_protected_fixture_patch(self):
        case = self.case()
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        self.files(repo, {"shared.txt": "resolved\n", _FIXTURE_PATHS[0]: "forged fixture\n"})
        patch = self.unchecked_patch(repo, case.base, self.root / "fixture.patch")
        self.failure("protected-path", materialize, self.clone(case), case.base, case.upstream, patch)

    def test_unrelated_histories_are_infrastructure_not_model_conflicts(self):
        case = self.case(unrelated=True)
        self.failure("merge-infrastructure", prepare, self.clone(case), case.base, case.upstream)

    def test_binary_non_utf8_delete_modify_and_symlink_conflicts_are_manual(self):
        cases = (
            ({"shared.txt": b"\x00base"}, {"shared.txt": b"\x00fork"}, {"shared.txt": b"\x00upstream"}),
            ({"shared.txt": b"\xffbase\n"}, {"shared.txt": b"\xfffork\n"}, {"shared.txt": b"\xffupstream\n"}),
            ({}, {"shared.txt": "fork\n"}, {"shared.txt": None}),
            ({"shared.txt": _Link("base-target")}, {"shared.txt": _Link("fork-target")}, {"shared.txt": _Link("upstream-target")}),
        )
        for common, fork, upstream in cases:
            with self.subTest(common=common, upstream=upstream):
                case = self.case(common=common, fork=fork, upstream=upstream)
                self.failure("manual-conflict-type", prepare, self.clone(case), case.base, case.upstream)

    def test_file_directory_conflict_is_manual(self):
        case = self.case(
            common={"node": "common file\n"}, fork={"node": "fork file\n"},
            upstream={"node": None, "node/child": "upstream directory\n"},
        )
        self.failure("manual-conflict-type", prepare, self.clone(case), case.base, case.upstream)

    def test_changed_symlinks_and_submodules_are_manual_even_for_clean_merge(self):
        case = self.case(fork={"fork.txt": "fork\n"}, upstream={"link": _Link("/etc/passwd")})
        self.failure("manual-conflict-type", prepare, self.clone(case), case.base, case.upstream)
        case = self.case(fork={"fork.txt": "fork\n"}, upstream={})
        self.git(case.source, "update-index", "--add", "--cacheinfo", f"160000,{case.base},module")
        self.git(case.source, "commit", "--quiet", "-m", "add gitlink")
        upstream = self.oid(case.source)
        changed = _Case(case.source, case.base, upstream)
        self.failure("manual-conflict-type", prepare, self.clone(changed), changed.base, changed.upstream)

    def test_unsafe_tracked_paths_rejected_before_checkout(self):
        for path in ("line\nbreak.txt", "back\\slash.txt", "name:stream.txt", "trailing. "):
            with self.subTest(path=path):
                case = self.case(fork={"fork.txt": "fork\n"}, upstream={path: "unsafe path\n"})
                self.failure("manual-conflict-path", prepare, self.clone(case), case.base, case.upstream)

    def test_resolution_cannot_change_file_mode_or_become_symlink(self):
        case = self.case()
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                repo = self.clone(case)
                prepare(repo, case.base, case.upstream)
                path = repo / "shared.txt"
                if symlink:
                    path.unlink()
                    path.symlink_to("/etc/passwd")
                else:
                    path.write_text("resolved\n")
                    path.chmod(0o755)
                self.failure("invalid-resolution", write_patch, repo, case.base, case.upstream, self.root / "mode.patch")

    def test_materialize_rejects_symlink_and_executable_mode_forgery(self):
        case = self.case()
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                repo = self.clone(case)
                prepare(repo, case.base, case.upstream)
                path = repo / "shared.txt"
                if symlink:
                    path.unlink()
                    path.symlink_to("/etc/passwd")
                else:
                    path.write_text("resolved\n")
                    path.chmod(0o755)
                patch = self.unchecked_patch(repo, case.base, self.root / "bad-mode.patch")
                self.failure("invalid-resolution", materialize, self.clone(case), case.base, case.upstream, patch)

    def test_path_traversal_and_git_administration_patch_cannot_escape(self):
        case = self.case(fork={"fork.txt": "fork\n"}, upstream={"upstream.txt": "automatic\n"})
        sentinel = self.root / "outside.txt"
        sentinel.write_text("do not change\n")
        for path in ("../outside.txt", ".git/config", "/tmp/upstream-sync-escape"):
            with self.subTest(path=path):
                patch = self.root / "escape.patch"
                patch.write_bytes((
                    f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
                    f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+escaped\n"
                ).encode("utf-8"))
                self.failure(("invalid-patch", "out-of-scope"), materialize, self.clone(case), case.base, case.upstream, patch)
                self.assertEqual(sentinel.read_text(), "do not change\n")

    def test_invalid_pins_empty_artifacts_and_noop_merges_rejected(self):
        case = self.case(fork={"fork.txt": "fork\n"}, upstream={}, unchanged=True)
        repo = self.clone(case)
        self.failure("invalid-revision", prepare, repo, "main", case.upstream)
        self.failure("invalid-revision", prepare, repo, case.base, "f" * 40)
        self.assertEqual(prepare(repo, case.base, case.upstream)["kind"], "noop")
        self.failure("noop", write_patch, repo, case.base, case.upstream, self.root / "noop.patch")
        patch = self.root / "empty.patch"
        patch.write_bytes(b"")
        self.failure("noop", materialize, self.clone(case), case.base, case.upstream, patch)
        changed = self.case(fork={"fork.txt": "fork\n"}, upstream={"upstream.txt": "new\n"})
        self.failure("invalid-patch", materialize, self.clone(changed), changed.base, changed.upstream, patch)
        patch.write_bytes(b"diff --git a/fork.txt b/fork.txt\n")
        self.failure("noop", materialize, self.clone(case), case.base, case.upstream, patch)

    def test_no_content_change_after_resolution_is_not_publishable(self):
        case = self.case(upstream={"shared.txt": "upstream\n"})
        repo = self.clone(case)
        prepare(repo, case.base, case.upstream)
        (repo / "shared.txt").write_text("fork\n")
        self.failure("empty-patch", write_patch, repo, case.base, case.upstream, self.root / "empty-result.patch")

    def test_repository_config_hooks_filters_drivers_and_host_environment_are_not_executed(self):
        case = self.case(common={".gitattributes": "shared.txt merge=hostile filter=hostile\n"})
        sentinel = self.root / "executed"
        command = self.root / "hostile-command"
        command.write_text(f"#!/bin/sh\nprintf executed >> {shlex.quote(str(sentinel))}\ncat\n")
        command.chmod(0o755)

        def install(repo):
            for key in ("merge.hostile.driver", "filter.hostile.clean", "filter.hostile.smudge", "core.fsmonitor"):
                self.git(repo, "config", key, str(command))
            self.git(repo, "config", "filter.hostile.required", "true")
            for hook in ("pre-commit", "post-checkout", "post-merge", "prepare-commit-msg"):
                target = repo / ".git" / "hooks" / hook
                target.write_bytes(command.read_bytes())
                target.chmod(0o755)

        repo = self.clone(case)
        install(repo)
        original_config = (repo / ".git" / "config").read_bytes()
        hostile_global = self.root / "hostile.gitconfig"
        hostile_global.write_text(f"[core]\nfsmonitor = {command}\nhooksPath = {repo / '.git/hooks'}\n")
        poison = {
            "GIT_CONFIG_GLOBAL": str(hostile_global), "GIT_EXTERNAL_DIFF": str(command),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": str(command),
            "GIT_INDEX_FILE": str(self.root / "stolen-index"), "GIT_WORK_TREE": str(self.root / "outside-tree"),
        }
        previous = {key: os.environ.get(key) for key in poison}
        try:
            os.environ.update(poison)
            self.assertEqual(prepare(repo, case.base, case.upstream)["conflicts"], ["shared.txt"])
            (repo / "shared.txt").write_text("resolved safely\n")
            patch = self.root / "safe.patch"
            write_patch(repo, case.base, case.upstream, patch)
            candidate = self.clone(case)
            install(candidate)
            result = materialize(candidate, case.base, case.upstream, patch)
            self.assertEqual(self.oid(candidate), result["commit"])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.root / "stolen-index").exists())
        self.assertEqual((repo / ".git" / "config").read_bytes(), original_config)


if __name__ == "__main__":
    unittest.main()
