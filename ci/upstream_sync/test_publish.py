"""Exercise synchronization with real local Git remotes, HTTP, and optional Docker."""

import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from ci.upstream_sync import control
from ci.upstream_sync.git_ops import SyncError
from ci.upstream_sync.test_control import LedgerHandler


REAL_GIT = control.git


class RepositoryHandler(LedgerHandler):
    def dispatch(self):
        path = self.path.split("?", 1)[0].removeprefix("/repos/fork/repo/")
        if path == "git/ref/heads/main":
            self.respond(200, {"object": {"sha": REAL_GIT(self.server.remote, "rev-parse", "refs/heads/main")}})
        elif self.path.startswith("/repos/rust-lang/rustfmt/git/ref/heads/main"):
            self.respond(200, {"object": {"sha": self.server.upstream}})
        elif path.startswith("git/ref/heads/"):
            reference = "refs/heads/" + path.removeprefix("git/ref/heads/")
            try:
                sha = REAL_GIT(self.server.remote, "rev-parse", "--verify", reference)
                self.respond(200, {"object": {"sha": sha}})
            except SyncError:
                self.respond(404, {})
        elif path.startswith("git/commits/"):
            sha = path.split("/")[-1]
            message = REAL_GIT(self.server.remote, "show", "--no-patch", "--format=%B", sha)
            self.respond(200, {"message": message})
        elif path == "pulls" and self.command == "GET":
            self.respond(200, list(self.server.prs.values()))
        elif path == "pulls" and self.command == "POST":
            import json
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            pr = dict(payload, number=1, html_url="https://github.com/fork/repo/pull/1")
            self.server.prs[1] = pr
            self.respond(201, pr)
        elif path.startswith("pulls/") and self.command == "PATCH":
            import json
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            pr = self.server.prs[int(path.split("/")[-1])]
            pr.update(payload)
            self.respond(200, pr)
        else:
            super().dispatch()

    do_GET = dispatch
    do_POST = dispatch
    do_PATCH = dispatch


class PublisherTransitions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="publisher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        REAL_GIT(self.source, "init", "--initial-branch=main")
        (self.source / "FORK.md").write_text("preservation contract\n")
        (self.source / "shared.rs").write_text("fn common() {}\n")
        self.commit("common")
        REAL_GIT(self.source, "branch", "upstream")
        (self.source / "fork.rs").write_text("fn fork_feature() {}\n")
        self.base = self.commit("fork")
        REAL_GIT(self.source, "checkout", "upstream")
        (self.source / "upstream.rs").write_text("fn upstream_feature() {}\n")
        self.upstream = self.commit("upstream")
        REAL_GIT(self.source, "checkout", "main")
        self.remote = self.root / "fork.git"
        REAL_GIT(self.source, "clone", "--bare", str(self.source), str(self.remote))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RepositoryHandler)
        self.server.remote = self.remote
        self.server.upstream = self.upstream
        self.server.prs = {}
        self.server.issues = {}
        self.server.reject = False
        self.server.environment = {
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            "protection_rules": [],
        }
        self.server.branches = [{"name": "main", "type": "branch"}]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.api = control.GitHub("fork/repo", "test-publisher-token", f"http://127.0.0.1:{self.server.server_port}")
        self.race_commit = None
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"GITHUB_REPOSITORY": "fork/repo", "GITHUB_SHA": self.base,
                                "GITHUB_RUN_ID": "1", "GITHUB_OUTPUT": ""}).start()
        # Redirect only network transport aliases. Git, merge verification, commits,
        # branch leases, HTTP request handling and durable issue state are real.
        patch.object(control, "git", self.transport).start()
        self.patch_file = self.root / "candidate.patch"
        control.plan(self.api, self.source, self.root / "prepare", self.patch_file, retry=False)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def commit(self, message):
        REAL_GIT(self.source, "add", "--all")
        REAL_GIT(self.source, "commit", "-m", message)
        return REAL_GIT(self.source, "rev-parse", "HEAD")

    def transport(self, repo, *arguments, auth=None):
        aliases = {
            "https://github.com/rust-lang/rustfmt.git": str(self.source),
            "https://github.com/fork/repo.git": str(self.remote),
        }
        if arguments[0] == "push" and self.race_commit:
            REAL_GIT(self.remote, "update-ref", "refs/heads/" + control.BRANCH, self.race_commit)
            self.race_commit = None
        return REAL_GIT(repo, *(aliases.get(argument, argument) for argument in arguments), auth=auth)

    def publish(self, name="publish"):
        control.publish(self.api, self.source, self.root / name, self.patch_file, self.base, self.upstream)

    def branch_sha(self):
        return REAL_GIT(self.remote, "rev-parse", "refs/heads/" + control.BRANCH)

    def test_clean_sync_publishes_real_merge_and_does_not_reopen_completed_pair(self):
        self.publish()
        tip = self.branch_sha()
        parents = REAL_GIT(self.remote, "show", "--no-patch", "--format=%P", tip).split()
        self.assertEqual(parents, [self.base, self.upstream])
        self.assertEqual(REAL_GIT(self.remote, "show", tip + ":fork.rs"), "fn fork_feature() {}")
        self.assertEqual(REAL_GIT(self.remote, "show", tip + ":upstream.rs"), "fn upstream_feature() {}")
        self.assertTrue(self.server.prs[1]["draft"])
        control.report(self.api, self.base, self.upstream, 1, {"publish": {"result": "success"}}, self.server.prs[1]["html_url"])
        control.plan(self.api, self.source, self.root / "duplicate", self.root / "unused.patch", retry=False)
        self.assertEqual(self.server.issues[1]["state"], "closed")
        self.assertEqual(self.branch_sha(), tip)
        self.assertFalse((self.root / "unused.patch").exists())

    def test_main_advancing_invalidates_validation_before_any_push(self):
        (self.source / "new-main.rs").write_text("fn newer_main() {}\n")
        new_main = self.commit("main advanced")
        REAL_GIT(self.source, "push", str(self.remote), "HEAD:refs/heads/main")
        with self.assertRaises(SyncError) as raised:
            self.publish()
        self.assertEqual(raised.exception.code, "stale_base")
        self.assertEqual(REAL_GIT(self.remote, "rev-parse", "refs/heads/main"), new_main)
        self.assertEqual(REAL_GIT(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/" + control.BRANCH), "")
        self.assertEqual(self.server.prs, {})

    def test_branch_lease_does_not_overwrite_a_concurrent_human_commit(self):
        self.publish()
        old_tip = self.branch_sha()
        (self.source / "human.rs").write_text("fn human_change() {}\n")
        human = self.commit("human branch work")
        REAL_GIT(self.source, "push", str(self.remote), human + ":refs/heads/human-object")
        self.race_commit = human
        with self.assertRaises(SyncError) as raised:
            self.publish("racing-publish")
        self.assertEqual(raised.exception.code, "git_transport")
        self.assertNotEqual(human, old_tip)
        self.assertEqual(self.branch_sha(), human)

    def test_ready_pr_is_not_rewritten_while_a_human_reviews_it(self):
        self.publish()
        tip = self.branch_sha()
        self.server.prs[1]["draft"] = False
        with self.assertRaises(SyncError) as raised:
            self.publish("ready-pr")
        self.assertEqual(raised.exception.code, "review_in_progress")
        self.assertEqual(self.branch_sha(), tip)
        self.assertFalse(self.server.prs[1]["draft"])

    def test_conflict_preparation_does_not_need_the_environment_model_secret(self):
        (self.source / "shared.rs").write_text("fn fork_variant() {}\n")
        base = self.commit("fork content change")
        REAL_GIT(self.source, "push", str(self.remote), "HEAD:refs/heads/main")
        REAL_GIT(self.source, "checkout", "upstream")
        (self.source / "shared.rs").write_text("fn upstream_variant() {}\n")
        upstream = self.commit("upstream content change")
        REAL_GIT(self.source, "checkout", "main")
        self.server.upstream = upstream
        candidate = self.root / "conflict.patch"
        with patch.dict(os.environ, {"GITHUB_SHA": base, "AI_BASE_URL": "https://provider.example/v1"}, clear=True):
            control.plan(self.api, self.source, self.root / "conflict-prepare", candidate, retry=False)
        _, state = control.find_state(self.api, base, upstream)
        self.assertEqual(state["status"], "reserved")
        self.assertEqual(state["attempts_reserved"], 2)
        self.assertEqual(state["paths"], ["shared.rs"])
        self.assertIn("<<<<<<<", (self.root / "conflict-prepare" / "shared.rs").read_text())
        self.assertFalse(candidate.exists())

    @unittest.skipUnless(os.environ.get("UPSTREAM_SYNC_CONTAINER_TESTS") == "1",
                         "Set UPSTREAM_SYNC_CONTAINER_TESTS=1 on a Docker runner for real validation tests")
    def test_validation_runs_executable_doctests_without_relaxing_mounts(self):
        trusted_ci = Path(__file__).resolve().parents[1]
        shutil.copytree(trusted_ci / "src", self.source / "ci" / "src")
        for name in ("Cargo.toml", "Cargo.lock"):
            shutil.copy2(trusted_ci / name, self.source / "ci" / name)
        (self.source / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.90.0"\nprofile = "minimal"\n'
        )
        for directory, name in ((".", "validation-root"), ("config_proc_macro", "validation-config"),
                                ("check_diff", "validation-diff")):
            crate = self.source / directory
            (crate / "src").mkdir(parents=True)
            (crate / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
            )
            (crate / "Cargo.lock").write_text(
                f'version = 4\n\n[[package]]\nname = "{name}"\nversion = "0.1.0"\n'
            )
            (crate / "src" / "lib.rs").write_text("")
        (self.source / "config_proc_macro" / "src" / "lib.rs").write_text("""\
//! ```
//! let mounts = std::fs::read_to_string("/proc/self/mountinfo").unwrap();
//! let has_option = |path: &str, option: &str| mounts.lines().any(|line| {
//!     let fields: Vec<_> = line.split_whitespace().collect();
//!     fields[4] == path && fields[5].split(',').any(|value| value == option)
//! });
//! assert!(has_option("/", "ro"), "container root must remain read-only");
//! assert!(has_option("/tmp", "noexec"), "/tmp must remain non-executable");
//! std::fs::write(std::env::temp_dir().join("doctest-output"), b"executed").unwrap();
//! std::fs::write("/work/doctest-proof", b"executed with hardened mounts").unwrap();
//! ```
""")
        base = self.commit("executable doctest fixture")
        prepared = self.root / "doctest-prepare"
        control.checkout(self.source, prepared, base, self.upstream)
        control.prepare(prepared, base, self.upstream)
        control.write_patch(prepared, base, self.upstream, self.patch_file)
        candidate = self.root / "validation"
        control.validate(self.source, candidate, self.patch_file, base, self.upstream)
        self.assertEqual((candidate / "doctest-proof").read_text(), "executed with hardened mounts")



if __name__ == "__main__":
    unittest.main()
