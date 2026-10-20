"""Real Git/filesystem/process invariants; optional tests exercise the OCI sandbox.

Parent verification may enable UPSTREAM_SYNC_CONTAINER_TESTS=1 on a Linux Docker
runner. These tests never call a model or use a real provider credential.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from . import agent, git_ops


class GitFixture:
    def __init__(self, root: Path):
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Resolver invariant tests")
        self.git("config", "user.email", "resolver-tests@example.invalid")
        (self.repo / "FORK.md").write_text("Preserve the fork contract.\n")
        (self.repo / "conflict.txt").write_text("original\n")
        (self.repo / "context.txt").write_text("read-only context\n")
        self.commit("initial")
        self.ancestor = self.git("rev-parse", "HEAD")
        self.git("checkout", "-b", "upstream")
        (self.repo / "conflict.txt").write_text("upstream\n")
        self.commit("upstream")
        self.upstream = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        (self.repo / "conflict.txt").write_text("fork\n")
        self.commit("fork")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *arguments],
            cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
        return result.stdout.strip()

    def commit(self, message: str) -> None:
        self.git("add", "--all")
        self.git("commit", "--message", message)


class ResolverInvariants(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="resolver-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_provider_configuration_rejects_non_https_credentials_and_package_injection(self):
        base = {"AI_BASE_URL": "https://provider.example/v1", "AI_API_KEY": "test-key"}
        invalid = [
            {"AI_BASE_URL": "http://provider.example/v1"},
            {"AI_BASE_URL": "https://key@provider.example/v1"},
            {"AI_BASE_URL": "https://provider.example/v1?api_key=secret"},
            {"AI_BASE_URL": "https://provider.example/{env:SECRET}"},
            {"AI_BASE_URL": "https://provider.example:invalid/v1"},
            {"AI_API_STYLE": "file:///untrusted-provider.js"},
            {"AI_PROVIDER": "*"},
            {"AI_MODEL": "glm#unsafe-variant"},
            {"AI_API_KEY": "line\nbreak"},
            {"AI_API_KEY": ""},
        ]
        for override in invalid:
            with self.subTest(override=override), patch.dict(os.environ, {**base, **override}, clear=True):
                with self.assertRaises(git_ops.SyncError) as raised:
                    agent.ProviderConfig.from_environment()
                self.assertEqual(raised.exception.code, "invalid-provider-config")
        with patch.dict(os.environ, base, clear=True):
            configured = agent.ProviderConfig.from_environment()
        self.assertEqual(configured.base_url, base["AI_BASE_URL"])
        self.assertNotIn(base["AI_API_KEY"], repr(configured))

    def test_limits_cannot_expand_the_attempt_or_wall_clock_bounds(self):
        for limits in (
            {"max_attempts": 3}, {"max_attempts": 0}, {"max_attempts": True},
            {"deadline_seconds": 1201}, {"deadline_seconds": float("nan")},
            {"deadline_seconds": 0}, {"deadline_seconds": True},
        ):
            with self.subTest(limits=limits), self.assertRaises(git_ops.SyncError) as raised:
                agent.resolve(self.root / "absent", "base", "upstream", self.root / "candidate.patch", **limits)
            self.assertEqual(raised.exception.code, "invalid-resolver-limits")

    def test_external_symlink_and_hardlink_never_become_conflict_mounts(self):
        source = self.root / "source"
        source.mkdir()
        outside = self.root / "private"
        outside.write_text("private host data\n")
        (source / "symlink").symlink_to(outside)
        (source / "directory").symlink_to(self.root, target_is_directory=True)
        os.link(outside, source / "hardlink")
        for name in ("symlink", "directory/private", "hardlink"):
            with self.subTest(name=name), self.assertRaises(git_ops.SyncError) as raised:
                agent._regular_path(source, name)
            self.assertEqual(raised.exception.code, "manual-conflict-type")

    def test_paths_cannot_escape_or_widen_exact_mounts(self):
        for name in ("../secret", "a/../secret", "/secret", ".git/config", "a/.Git/config",
                     "a//b", "a*", "a?", "a,b", "a\\b", "a\nb", "./a"):
            with self.subTest(name=name), self.assertRaises(git_ops.SyncError):
                agent._safe_relative(name)

    def test_binary_and_oversized_conflicts_are_rejected(self):
        binary = self.root / "binary"
        for content in (b"text\0binary", b"\xff", b"text\x1bcontrol"):
            binary.write_bytes(content)
            with self.assertRaises(git_ops.SyncError) as raised:
                agent._read_text(binary, agent._Budget(5))
            self.assertEqual(raised.exception.code, "manual-conflict-type")
        with binary.open("wb") as stream:
            stream.truncate(agent.MAX_FILE_BYTES + 1)
        with self.assertRaises(git_ops.SyncError) as raised:
            agent._read_text(binary, agent._Budget(5))
        self.assertEqual(raised.exception.code, "resource-limit")

    def test_snapshot_excludes_git_untracked_secrets_and_host_links(self):
        fixture = GitFixture(self.root)
        secret = fixture.repo / "checkout-token"
        secret.write_text("must not enter snapshot\n")
        (fixture.repo / "host-link").symlink_to(secret)
        fixture.git("add", "host-link")
        fixture.git("commit", "--message", "tracked host link")
        fixture.base = fixture.git("rev-parse", "HEAD")
        prepared = git_ops.prepare(fixture.repo, fixture.base, fixture.upstream)
        snapshot_root = self.root / "snapshot"
        snapshot_root.mkdir()
        mounts = agent._snapshot(fixture.repo, snapshot_root, prepared["conflicts"],
                                 self.root / "home", agent._Budget(30))
        source = snapshot_root / "workspace" / "repo"
        self.assertFalse((source / ".git").exists())
        self.assertFalse((source / "checkout-token").exists())
        self.assertFalse((source / "host-link").exists())
        self.assertEqual(set(mounts), {"conflict.txt"})
        mounts["conflict.txt"].write_text("model proposal\n")
        self.assertIn(b"<<<<<<<", (fixture.repo / "conflict.txt").read_bytes())
        self.assertIn(b"<<<<<<<", (source / "conflict.txt").read_bytes())
        self.assertEqual((source / "context.txt").read_text(), "read-only context\n")

    def test_fresh_prepare_discards_a_failed_attempt(self):
        fixture = GitFixture(self.root)
        budget = agent._Budget(30)
        home = self.root / "git-home"
        first = agent._git_call("prepare", fixture.repo, fixture.base, fixture.upstream, None, home, budget)
        (fixture.repo / "conflict.txt").write_text("invalid first proposal\n")
        second = agent._git_call("prepare", fixture.repo, fixture.base, fixture.upstream, None, home, budget)
        self.assertEqual(first["conflicts"], second["conflicts"])
        self.assertIn(b"<<<<<<<", (fixture.repo / "conflict.txt").read_bytes())
        self.assertNotIn(b"invalid first proposal", (fixture.repo / "conflict.txt").read_bytes())
        self.assertEqual(fixture.git("rev-parse", "HEAD"), fixture.base)

    def test_unavailable_sandbox_never_runs_host_opencode_or_emits_patch(self):
        fixture = GitFixture(self.root)
        output = self.root / "candidate.patch"
        with patch.dict(os.environ, {"AI_BASE_URL": "https://provider.example/v1",
                                    "AI_API_KEY": "never-used-test-key", "DOCKER_HOST": "tcp://remote:2375"}, clear=True):
            with self.assertRaises(git_ops.SyncError) as raised:
                agent.resolve(fixture.repo, fixture.base, fixture.upstream, output, deadline_seconds=30)
        self.assertEqual(raised.exception.code, "sandbox-unavailable")
        self.assertEqual(raised.exception.attempts, 0)
        self.assertFalse(output.exists())
        self.assertIn(b"<<<<<<<", (fixture.repo / "conflict.txt").read_bytes())

    def test_clean_merge_produces_a_real_validated_patch_without_ai(self):
        fixture = GitFixture(self.root)
        fixture.git("checkout", "-b", "clean-upstream", fixture.ancestor)
        (fixture.repo / "upstream-only.txt").write_text("automatic upstream change\n")
        fixture.commit("clean upstream")
        upstream = fixture.git("rev-parse", "HEAD")
        fixture.git("checkout", "main")
        output = self.root / "candidate.patch"
        with patch.dict(os.environ, {}, clear=True):
            result = agent.resolve(fixture.repo, fixture.base, upstream, output, deadline_seconds=30)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["status"], "resolved")
        checked = git_ops.materialize(fixture.repo, fixture.base, upstream, output)
        self.assertEqual(fixture.git("rev-parse", "HEAD^1"), fixture.base)
        self.assertEqual(fixture.git("rev-parse", "HEAD^2"), upstream)
        self.assertEqual((fixture.repo / "upstream-only.txt").read_text(), "automatic upstream change\n")
        self.assertEqual(checked["kind"], "clean")
        self.assertEqual((fixture.repo / "conflict.txt").read_text(), "fork\n")

    def test_clean_ancestry_only_merge_publishes_verified_empty_patch(self):
        fixture = GitFixture(self.root)
        # Content was ported independently, but upstream is not yet an ancestor.
        (fixture.repo / "conflict.txt").write_text("upstream\n")
        fixture.commit("independently port upstream content")
        base = fixture.git("rev-parse", "HEAD")
        base_tree = fixture.git("rev-parse", "HEAD^{tree}")
        output = self.root / "not-created-yet" / "candidate.patch"
        with patch.dict(os.environ, {}, clear=True):
            result = agent.resolve(fixture.repo, base, fixture.upstream, output, deadline_seconds=30)
        self.assertEqual(result["kind"], "clean")
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(output.read_bytes(), b"")
        git_ops.materialize(fixture.repo, base, fixture.upstream, output)
        self.assertEqual(fixture.git("rev-parse", "HEAD^{tree}"), base_tree)
        self.assertEqual(fixture.git("rev-parse", "HEAD^1"), base)
        self.assertEqual(fixture.git("rev-parse", "HEAD^2"), fixture.upstream)

    def test_actual_process_timeout_kills_the_process_group(self):
        pid_file = self.root / "child.pid"
        program = "import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(pid_file)
        started = time.monotonic()
        with self.assertRaises(git_ops.SyncError) as raised:
            agent._run([sys.executable, "-c", program], env=agent._isolated_environment(self.root / "home"),
                       cwd=self.root, budget=agent._Budget(5), timeout=0.3, timeout_code="agent-timeout")
        self.assertEqual(raised.exception.code, "agent-timeout")
        self.assertLess(time.monotonic() - started, 2)
        child_pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_subprocesses_share_one_wall_clock_budget(self):
        environment = agent._isolated_environment(self.root / "home")
        budget = agent._Budget(0.4)
        started = time.monotonic()
        agent._run([sys.executable, "-c", "import time; time.sleep(0.2)"],
                   env=environment, cwd=self.root, budget=budget)
        pid_file = self.root / "budget-child.pid"
        program = "import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(pid_file)
        with self.assertRaises(git_ops.SyncError) as raised:
            agent._run([sys.executable, "-c", program],
                       env=environment, cwd=self.root, budget=budget)
        self.assertEqual(raised.exception.code, "resolver-timeout")
        self.assertLess(time.monotonic() - started, 1.5)
        with self.assertRaises(ChildProcessError):
            os.waitpid(int(pid_file.read_text()), os.WNOHANG)

    def test_metadata_capture_is_bounded_and_never_relayed(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(git_ops.SyncError) as raised:
            agent._run([sys.executable, "-c", "import os; os.write(1, b'sensitive-output' * 10000)"],
                       env=agent._isolated_environment(self.root / "home"), cwd=self.root,
                       budget=agent._Budget(5), capture=True, output_limit=1024)
        self.assertEqual(raised.exception.code, "resource-limit")
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("sensitive-output", str(raised.exception))

    def test_git_workers_do_not_inherit_provider_or_github_credentials(self):
        with patch.dict(os.environ, {"AI_API_KEY": "private-key", "GITHUB_TOKEN": "write-token",
                                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                    "GIT_CONFIG_VALUE_0": "/untrusted/command"}):
            environment = agent._isolated_environment(self.root / "home")
        code, data = agent._run(
            [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
            env=environment, cwd=self.root, budget=agent._Budget(5), capture=True,
        )
        self.assertEqual(code, 0)
        inherited = json.loads(data)
        for name in ("AI_API_KEY", "GITHUB_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.assertNotIn(name, inherited)


@unittest.skipUnless(os.environ.get("UPSTREAM_SYNC_CONTAINER_TESTS") == "1",
                     "Set UPSTREAM_SYNC_CONTAINER_TESTS=1 on a Docker runner for real OCI security tests")
class ActualContainerInvariants(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="resolver-container-test-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.budget = agent._Budget(240)
        self.sandbox = agent._Sandbox(self.root, self.budget)
        self.sandbox.check()
        workspace = self.root / "workspace" / "repo"
        workspace.mkdir(parents=True)
        (workspace / "conflict.txt").write_text("conflict fixture\n")
        (workspace / "context.txt").write_text("read-only fixture\n")
        self.editable = self.root / "editable"
        self.editable.write_text("conflict fixture\n")
        self.editable.chmod(0o666)
        self.mounts = {"conflict.txt": self.editable}
        self.provider = agent.ProviderConfig("glm-5.3", "sync", "https://provider.example/v1",
                                             "openai-compatible", "not-a-real-key")
        self.config = agent.managed_config(self.provider, list(self.mounts), 30)
        (self.root / "opencode.json").write_text(json.dumps(self.config))
        (self.root / "opencode.json").chmod(0o444)

    def test_pinned_binary_actually_accepts_every_managed_setting(self):
        code, loaded = self.sandbox.invoke(self.root, self.mounts, ["debug", "config"], seconds=45, capture=True)
        self.assertEqual(code, 0)
        agent._validate_loaded_config(loaded, self.config)

    def test_kernel_enforces_exact_file_writes_and_credential_isolation(self):
        name = "upstream-sync-test-" + uuid.uuid4().hex
        command = self.sandbox.arguments(name, self.root, self.mounts, network=False)
        command += [
            "--entrypoint=/bin/sh", agent.OPENCODE_IMAGE, "-c",
            "set -eu; test \"$(id -u)\" -ne 0; "
            "test ! -e /workspace/repo/.git; test ! -e /var/run/docker.sock; "
            "test -z \"${GITHUB_TOKEN:-}\"; test -z \"${AI_API_KEY:-}\"; "
            "test -z \"${SYNC_PROVIDER_API_KEY:-}\"; "
            "if touch /workspace/new-file 2>/dev/null; then exit 10; fi; "
            "if printf forbidden >/workspace/repo/context.txt 2>/dev/null; then exit 11; fi; "
            "if printf forbidden >/etc/opencode/opencode.json 2>/dev/null; then exit 12; fi; "
            "printf resolved >/workspace/repo/conflict.txt; "
            "test \"$(cat /workspace/repo/context.txt)\" = 'read-only fixture'",
        ]
        try:
            code, _ = agent._run(command, env=self.sandbox.env, cwd=self.root,
                                 budget=self.budget, timeout=30)
            self.assertEqual(code, 0)
            self.assertEqual(self.editable.read_text(), "resolved")
            self.assertEqual((self.root / "workspace" / "repo" / "conflict.txt").read_text(), "conflict fixture\n")
        finally:
            agent._run(self.sandbox.command + ["rm", "--force", name], env=self.sandbox.env,
                       cwd=self.root, budget=self.budget, timeout=5)


if __name__ == "__main__":
    unittest.main()
