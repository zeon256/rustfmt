"""Exercise Rust temporary executables in the actual secret-free validation container."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ci.upstream_sync import control


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
