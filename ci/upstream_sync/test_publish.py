"""Exercise the publisher against real local Git remotes and an HTTP GitHub fixture."""

import contextlib
import io
import json
import os
from pathlib import Path
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

    def test_unresolved_pair_requires_manual_retry_without_mutating_ledger(self):
        for status, with_summary in (("reserved", True), ("failed", True), ("failed", False)):
            with self.subTest(status=status, summary=with_summary):
                issue, state = control.find_state(self.api, self.base, self.upstream)
                control.update_state(self.api, issue, state, status=status, reason="Prior fixture result.")
                before = json.dumps(self.server.issues, sort_keys=True).encode()
                name = f"blocked-{status}-{with_summary}"
                candidate = self.root / (name + ".patch")
                summary = self.root / (name + ".md")
                outputs = self.root / (name + ".outputs")
                annotations = io.StringIO()
                server = "https://github.example" if status == "failed" else "https://github.com"
                issue_url = f"{server}/fork/repo/issues/{issue['number']}"
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}), contextlib.redirect_stdout(annotations):
                    os.environ.pop("GITHUB_SERVER_URL", None)
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                    if status == "failed":
                        os.environ["GITHUB_SERVER_URL"] = server
                    if with_summary:
                        summary.write_text("# Existing summary\n")
                        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
                    with self.assertRaises(SyncError) as raised:
                        control.plan(self.api, self.source, self.root / name, candidate, retry=False)
                self.assertEqual(raised.exception.code, "retry_required")
                self.assertEqual(json.dumps(self.server.issues, sort_keys=True).encode(), before)
                self.assertEqual(self.server.prs, {})
                self.assertEqual(REAL_GIT(self.remote, "for-each-ref", "--format=%(refname)",
                                          "refs/heads/" + control.BRANCH), "")
                self.assertFalse(candidate.exists())
                emitted = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
                self.assertEqual(emitted["active"], "false")
                self.assertEqual(emitted["issue"], "")
                self.assertIn("::error::", annotations.getvalue())
                self.assertIn(status, annotations.getvalue())
                self.assertIn(issue_url, annotations.getvalue())
                if with_summary:
                    contents = summary.read_text()
                    self.assertTrue(contents.startswith("# Existing summary\n"))
                    self.assertIn(issue_url, contents)
                    self.assertIn(f"`{status}`", contents)
                else:
                    self.assertFalse(summary.exists())

    def test_explicit_retry_rearms_a_failed_pair_once(self):
        issue, state = control.find_state(self.api, self.base, self.upstream)
        control.update_state(self.api, issue, state, status="failed", reason="Prior fixture failure.")
        candidate = self.root / "retry.patch"
        control.plan(self.api, self.source, self.root / "retry", candidate, retry=True)
        rearmed, state = control.find_state(self.api, self.base, self.upstream)
        self.assertEqual(len(self.server.issues), 1)
        self.assertEqual(rearmed["number"], issue["number"])
        self.assertEqual(rearmed["state"], "open")
        self.assertEqual(state["status"], "reserved")
        self.assertEqual(state["attempts_reserved"], 0)
        reconstructed = self.root / "retry-reconstruction"
        control.checkout(self.source, reconstructed, self.base, self.upstream)
        result = control.materialize(reconstructed, self.base, self.upstream, candidate)
        parents = REAL_GIT(reconstructed, "show", "--no-patch", "--format=%P", result["commit"]).split()
        self.assertEqual(parents, [self.base, self.upstream])

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



if __name__ == "__main__":
    unittest.main()
