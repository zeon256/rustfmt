"""Exercise workflow parsing and Rust temporary executables in secret-free containers."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ci.upstream_sync import control, test_publish


@unittest.skipUnless(os.environ.get("UPSTREAM_SYNC_CONTAINER_TESTS") == "1",
                     "Set UPSTREAM_SYNC_CONTAINER_TESTS=1 on a Docker runner for real OCI validation tests")
class ActualWorkflowContainerInvariants(unittest.TestCase):
    WORKFLOW = (
        "on: workflow_dispatch\n"
        "permissions: {}\n"
        "jobs:\n"
        "  smoke:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo unexpected > /work/workflow-executed\n"
    )

    @classmethod
    def setUpClass(cls):
        runtime = shutil.which("docker")
        if not runtime:
            raise AssertionError("Enabled workflow-container tests require Docker")
        pulled = subprocess.run([runtime, "pull", control.WORKFLOW_IMAGE],
                                timeout=180, capture_output=True, text=True)
        if pulled.returncode:
            raise AssertionError(pulled.stdout + pulled.stderr)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-container-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)

    def test_invalid_candidate_fails_before_rust_despite_candidate_ignore_config(self):
        fixture = test_publish.PublisherTransitions()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        test_publish.REAL_GIT(fixture.source, "checkout", "upstream")
        workflows = fixture.source / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "release.yml").write_text(self.WORKFLOW + "\npermissions: {}\n")
        (fixture.source / ".github" / "actionlint.yaml").write_text(
            "paths:\n  '.github/workflows/**':\n    ignore: ['.*']\n"
        )
        upstream = fixture.commit("invalid release workflow with candidate suppressions")
        test_publish.REAL_GIT(fixture.source, "checkout", "main")
        prepared = fixture.root / "workflow-prepare"
        candidate = fixture.root / "workflow.patch"
        control.checkout(fixture.source, prepared, fixture.base, upstream)
        control.prepare(prepared, fixture.base, upstream)
        control.write_patch(prepared, fixture.base, upstream, candidate)
        with self.assertRaises(control.SyncError) as raised:
            control.validate(fixture.source, fixture.root / "workflow-validation", candidate,
                             fixture.base, upstream)
        self.assertEqual(raised.exception.code, "workflow_invalid")
        self.assertFalse((fixture.root / "validation-storage").exists())

    def test_duplicate_job_keys_are_rejected(self):
        (self.workflows / "release.yml").write_text(self.WORKFLOW.replace(
            "    steps:\n", "    permissions: {}\n    permissions: {}\n    steps:\n"
        ))
        with self.assertRaises(control.SyncError) as raised:
            control._validate_workflows(self.repo, [".github/workflows/release.yml"])
        self.assertEqual(raised.exception.code, "workflow_invalid")

    def test_valid_changes_ignore_unchanged_and_deleted_workflows_without_execution(self):
        (self.workflows / "release.yaml").write_text(self.WORKFLOW)
        (self.workflows / "unchanged.yml").write_text(self.WORKFLOW + "\npermissions: {}\n")
        control._validate_workflows(self.repo, [
            ".github/workflows/release.yaml", ".github/workflows/deleted.yml", "README.md",
        ])
        self.assertFalse((self.repo / "workflow-executed").exists())


@unittest.skipUnless(os.environ.get("UPSTREAM_SYNC_CONTAINER_TESTS") == "1",
                     "Set UPSTREAM_SYNC_CONTAINER_TESTS=1 on a Docker runner for real OCI validation tests")
class ActualValidationContainerInvariants(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="validation-container-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = shutil.which("docker")
        self.assertIsNotNone(self.runtime, "Enabled validation-container tests require Docker")
        pulled = subprocess.run([self.runtime, "pull", control.VALIDATION_IMAGE],
                                timeout=180, capture_output=True, text=True)
        self.assertEqual(pulled.returncode, 0, pulled.stdout + pulled.stderr)
        resolved = subprocess.run([self.runtime, "run", "--rm", "--network=none",
                                   control.VALIDATION_IMAGE, "rustup", "which", "rustdoc"],
                                  timeout=30, capture_output=True, text=True)
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        self.rustdoc = resolved.stdout.strip()
        self.assertTrue(Path(self.rustdoc).is_absolute(), resolved.stdout + resolved.stderr)

    def test_rustdoc_executes_in_build_temp_without_relaxing_tmpfs(self):
        source = self.root / "trusted"
        (source / "ci").mkdir(parents=True)
        (source / "ci" / "tempdir-smoke.rs").write_text(
            '/// ```\n'
            '/// assert_eq!(std::env::temp_dir(), std::path::PathBuf::from("/target/tmp"));\n'
            '/// std::fs::write(std::env::temp_dir().join("doctest-executed"), b"ran").unwrap();\n'
            '/// ```\n'
            'pub fn tempdir_smoke() {}\n'
        )
        repo = self.root / "candidate"
        repo.mkdir()
        scratch = self.root / "scratch"
        command = control._validation_container_command(source, repo, scratch)
        command.insert(len(command) - 1, "--network=none")
        command += [
            "/bin/sh", "-eu", "-c",
            "printf '#!/bin/sh\\nexit 0\\n' >/tmp/noexec-smoke; "
            "chmod 700 /tmp/noexec-smoke; "
            "if /tmp/noexec-smoke; then echo '/tmp unexpectedly permits execution' >&2; exit 1; fi; "
            'exec "$1" --test /trusted/ci/tempdir-smoke.rs',
            "validation-smoke", self.rustdoc,
        ]
        result = subprocess.run(command, timeout=60, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((scratch / "target" / "tmp" / "doctest-executed").read_bytes(), b"ran")


if __name__ == "__main__":
    unittest.main()
