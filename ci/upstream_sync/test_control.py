"""Behavioral tests for the durable retry ledger and publication approval gates."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from ci.upstream_sync.control import (
    GitHub,
    SyncError,
    check_environment,
    decode_state,
    find_state,
    report,
    reserve,
    state_title,
    update_state,
)


BASE = "a" * 40
UPSTREAM = "b" * 40


class LedgerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        path = self.path.split("?", 1)[0].removeprefix("/repos/fork/repo/")
        if self.server.reject:
            self.respond(403, {"message": "provider-secret-do-not-log"})
            return
        payload = None
        if self.command in {"POST", "PATCH"}:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if path == "issues" and self.command == "GET":
            self.respond(200, list(self.server.issues.values()))
        elif path == "issues" and self.command == "POST":
            number = len(self.server.issues) + 1
            issue = dict(payload, number=number, state="open", user={"login": "github-actions[bot]"})
            self.server.issues[number] = issue
            self.respond(201, issue)
        elif path.startswith("issues/"):
            number = int(path.split("/")[-1])
            issue = self.server.issues[number]
            if self.command == "PATCH":
                issue.update(payload)
            self.respond(200, issue)
        elif path.startswith("environments/"):
            if path.endswith("/deployment-branch-policies"):
                self.respond(200, {"branch_policies": self.server.branches})
            else:
                self.respond(200, self.server.environment)
        else:
            self.respond(404, {"message": "Not found"})

    do_GET = dispatch
    do_POST = dispatch
    do_PATCH = dispatch


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LedgerHandler)
        self.server.issues = {}
        self.server.reject = False
        self.server.environment = {
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User", "id": 1}]}],
        }
        self.server.branches = [{"name": "main", "type": "branch"}]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = GitHub("fork/repo", "test-token", f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_crash_reservation_prevents_scheduled_retry_but_manual_retry_rearms(self):
        issue, state, allowed = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        self.assertTrue(allowed)
        # No completion report: this is the durable state left by a killed agent job.
        issue_again, state_again, allowed = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        self.assertFalse(allowed)
        self.assertEqual(issue_again["number"], issue["number"])
        self.assertEqual(state_again["attempts_reserved"], 2)
        _, _, allowed = reserve(self.api, BASE, UPSTREAM, retry=True, attempts=2)
        self.assertTrue(allowed)
        self.assertEqual(len(self.server.issues), 1)

    def test_changed_upstream_gets_independent_budget(self):
        reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        _, state, allowed = reserve(self.api, BASE, "c" * 40, retry=False, attempts=2)
        self.assertTrue(allowed)
        self.assertEqual(state["upstream"], "c" * 40)
        self.assertEqual(len(self.server.issues), 2)

    def test_published_pair_remains_blocked_after_issue_closes(self):
        issue, state, _ = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=0)
        update_state(self.api, issue, state, status="published", reason="Published.", pr_url="https://github.com/fork/repo/pull/1")
        _, persisted, allowed = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=0)
        self.assertFalse(allowed)
        self.assertEqual(persisted["status"], "published")
        self.assertEqual(self.server.issues[issue["number"]]["state"], "closed")

    def test_failed_validation_preserves_conflict_report_without_rearming(self):
        issue, state, _ = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        state["paths"] = ["src/conflict.rs"]
        update_state(self.api, issue, state, status="reserved", reason="Resolving.")
        report(self.api, BASE, UPSTREAM, issue["number"], {"validate": {"result": "failure"}}, "")
        final = self.server.issues[issue["number"]]
        self.assertEqual(decode_state(final, BASE, UPSTREAM)["status"], "failed")
        self.assertIn("src/conflict.rs", final["body"])
        self.assertFalse(reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)[2])

    def test_foreign_issue_cannot_authorize_or_reset_a_budget(self):
        issue, _, _ = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        self.server.issues[issue["number"]]["user"]["login"] = "untrusted-user"
        with self.assertRaises(SyncError) as raised:
            reserve(self.api, BASE, UPSTREAM, retry=True, attempts=2)
        self.assertEqual(raised.exception.code, "state_ownership")

    def test_short_sha_collision_does_not_overwrite_another_pair(self):
        reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        different_base = BASE[:12] + "d" * 28
        self.assertEqual(state_title(BASE, UPSTREAM), state_title(different_base, UPSTREAM))
        with self.assertRaises(SyncError) as raised:
            reserve(self.api, different_base, UPSTREAM, retry=True, attempts=2)
        self.assertEqual(raised.exception.code, "state_corrupt")
        self.assertEqual(len(self.server.issues), 1)

    def test_duplicate_tracking_issues_fail_closed(self):
        issue, _, _ = reserve(self.api, BASE, UPSTREAM, retry=False, attempts=2)
        self.server.issues[2] = dict(self.server.issues[issue["number"]], number=2)
        with self.assertRaises(SyncError) as raised:
            find_state(self.api, BASE, UPSTREAM)
        self.assertEqual(raised.exception.code, "state_corrupt")

    def test_review_gate_requires_a_real_reviewer_not_just_an_environment_name(self):
        self.server.environment["protection_rules"] = []
        with self.assertRaises(SyncError) as raised:
            check_environment(self.api, "upstream-sync-review", require_review=True)
        self.assertEqual(raised.exception.code, "environment_protection")
        # A single-maintainer repository may approve its own manual run explicitly.
        self.server.environment["protection_rules"] = [{"type": "required_reviewers", "reviewers": [{"type": "User", "id": 1}]}]
        check_environment(self.api, "upstream-sync-review", require_review=True)

    def test_environment_rejects_tags_and_additional_deployment_branches(self):
        for branches in ([{"name": "main", "type": "tag"}],
                         [{"name": "main", "type": "branch"}, {"name": "*", "type": "branch"}]):
            with self.subTest(branches=branches):
                self.server.branches = branches
                with self.assertRaises(SyncError) as raised:
                    check_environment(self.api, "upstream-sync", require_review=False)
                self.assertEqual(raised.exception.code, "environment_protection")

    def test_api_error_never_echoes_server_body_or_auth_token(self):
        self.server.reject = True
        with self.assertRaises(SyncError) as raised:
            self.api.repo("issues")
        self.assertEqual(raised.exception.code, "github_api")
        self.assertNotIn("provider-secret", str(raised.exception))
        self.assertNotIn("test-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
