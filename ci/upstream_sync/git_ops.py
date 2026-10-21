"""Validate upstream merges in disposable clones without trusting their Git configuration.

The artifact is a complete binary diff against a pinned base, never an incremental
resolution patch. Only ordinary clones (a real .git directory) are supported. These
functions deliberately discard the working tree when preparing/materializing.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


class SyncError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_REGULAR = {"100644", "100755"}
_FIXTURES = (
    "blank_lines_before_control_flow_statements",
    "blank_lines_after_control_flow_statements",
    "blank_lines_between_items",
)
_PROTECTED = ("FORK.md",) + tuple(
    f"tests/{side}/configs/{name}" for side in ("source", "target") for name in _FIXTURES
)
_REVIEW_FILES = {"release-plz.toml", "rust-toolchain.toml"}
_MARKERS = re.compile(rb"^(?:<{7,}|={7,}|>{7,}|\|{7,})(?:[ \t].*)?\r?$", re.MULTILINE)
_LEGACY_DIRECTIVE = re.compile(rb"// rustfmt-[A-Za-z_]+: \S+")


def _path(raw: bytes, code: str = "manual-conflict-path") -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SyncError(code, "Non-UTF-8 Git paths require manual integration.") from None
    parts = path.split("/")
    if (
        not path
        or "\\" in path
        or ":" in path
        or not path.isprintable()
        or any(
            part in ("", ".", "..")
            or part.rstrip(" .").casefold() == ".git"
            or part.endswith((" ", "."))
            for part in parts
        )
    ):
        raise SyncError(code, "Unsafe Git path requires manual integration.")
    return path


def _atomic(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".upstream-sync-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _Git:
    def __init__(self, repo: Path, worktree: Path, control: Path, env: dict):
        self.repo = repo
        self.worktree = worktree
        self.control = control
        self.env = env

    def run(self, *args: str, data=None, allowed=(0,), code="git-infrastructure", extra_env=None):
        try:
            result = subprocess.run(
                [
                    "git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                    f"--git-dir={self.control}", f"--work-tree={self.worktree}", *args,
                ],
                cwd=self.worktree,
                env=self.env if extra_env is None else {**self.env, **extra_env},
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SyncError("git-infrastructure", f"Git {args[0]} could not complete.") from None
        if allowed is not None and result.returncode not in allowed:
            raise SyncError(code, f"Git {args[0]} failed in the disposable clone.")
        return result

    def publish(self, head: str, upstream: str | None = None) -> None:
        """Expose the safe index/HEAD without copying configuration or running hooks."""
        directory = self.repo / ".git"
        index = self.control / "index"
        if index.exists():
            _atomic(directory / "index", index.read_bytes())
        else:
            (directory / "index").unlink(missing_ok=True)
        _atomic(directory / "HEAD", (head + "\n").encode("ascii"))
        for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE"):
            (directory / name).unlink(missing_ok=True)
        if upstream is not None:
            _atomic(directory / "MERGE_HEAD", (upstream + "\n").encode("ascii"))
            _atomic(directory / "MERGE_MSG", b"Merge pinned upstream into fork\n")
            _atomic(directory / "MERGE_MODE", b"no-ff")


@contextmanager
def _session(repo: Path, base: str, upstream: str, *, separate=False, copy_index=False):
    if (
        not isinstance(base, str)
        or not isinstance(upstream, str)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", upstream)
        or len(base) != len(upstream)
    ):
        raise SyncError("invalid-revision", "Both revisions must be full pinned commit IDs.")
    try:
        repo = Path(repo).resolve(strict=True)
        directory = repo / ".git"
        if not directory.is_dir() or directory.is_symlink():
            raise SyncError("unsupported-repository", "An ordinary disposable clone is required.")
        objects = directory / "objects"
        if not objects.is_dir() or objects.is_symlink():
            raise SyncError("unsupported-repository", "A local Git object directory is required.")
        for name in ("HEAD", "index"):
            path = directory / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise SyncError("unsupported-repository", "Unsafe Git administrative file.")
        with tempfile.TemporaryDirectory(prefix="upstream-sync-git-") as temporary:
            root = Path(temporary)
            control = root / "control"
            for name in ("refs/heads", "objects", "home", "info"):
                (control / name).mkdir(parents=True)
            worktree = root / "worktree" if separate else repo
            worktree.mkdir(exist_ok=True)
            config = (
                "[core]\n"
                f"repositoryformatversion = {1 if len(base) == 64 else 0}\n"
                "bare = false\nfilemode = true\nlogallrefupdates = false\n"
                "autocrlf = false\nhooksPath = /dev/null\nfsmonitor = false\n"
                "attributesFile = /dev/null\nprotectHFS = true\nprotectNTFS = true\n"
                "[user]\nname = Upstream Sync\nemail = upstream-sync@users.noreply.github.com\n"
                "[commit]\ngpgSign = false\n[merge]\nautoStash = false\n"
                "[gc]\nauto = 0\n[maintenance]\nauto = false\n"
                "[submodule]\nrecurse = false\n[fetch]\nrecurseSubmodules = false\n"
                "[protocol]\nallow = never\n"
            )
            if len(base) == 64:
                config += "[extensions]\nobjectFormat = sha256\n"
            (control / "config").write_text(config, encoding="utf-8")
            # Highest-precedence attributes prevent repository-controlled filters,
            # encodings, or small conflict markers from changing our byte contract.
            (control / "info/attributes").write_text(
                "* -filter -working-tree-encoding conflict-marker-size=7\n", encoding="ascii"
            )
            (control / "HEAD").write_text(base + "\n", encoding="ascii")
            if copy_index and (directory / "index").exists():
                shutil.copyfile(directory / "index", control / "index")
            env = {
                "PATH": os.defpath,
                "HOME": str(control / "home"),
                "XDG_CONFIG_HOME": str(control / "home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OBJECT_DIRECTORY": str(objects),
            }
            git = _Git(repo, worktree, control, env)
            for revision in (base, upstream):
                result = git.run("cat-file", "-t", revision, code="invalid-revision")
                if result.stdout.strip() != b"commit":
                    raise SyncError("invalid-revision", "Pinned revisions must identify commits, not tags.")
            yield git
    except OSError:
        raise SyncError("git-infrastructure", "Disposable repository I/O failed.") from None


def _tree(git: _Git, revision: str, *, code="manual-conflict-path") -> dict:
    entries = {}
    for record in git.run("ls-tree", "-r", "-z", "--full-tree", revision).stdout.split(b"\0"):
        if record:
            metadata, raw = record.split(b"\t", 1)
            mode, _, oid = metadata.decode("ascii").split()
            entries[_path(raw, code)] = (mode, oid)
    return entries


def _index(git: _Git, *, code="manual-conflict-path") -> dict:
    entries = {}
    for record in git.run("ls-files", "--stage", "-z").stdout.split(b"\0"):
        if record:
            metadata, raw = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            entries.setdefault(_path(raw, code), {})[int(stage)] = (mode, oid)
    return entries


def _changed(before: dict, after: dict) -> set:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def _protected(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _PROTECTED)


def _preserve(before: dict, after: dict) -> None:
    if any(_protected(path) for path in _changed(before, after)):
        raise SyncError("protected-path", "Fork preservation files changed; manual integration is required.")


def _review(paths) -> list[str]:
    return sorted(
        path for path in paths
        if _protected(path) or path.startswith((".github/", "ci/")) or path in _REVIEW_FILES
    )


def _reset(git: _Git, base: str) -> None:
    # No ignored/untracked model output may survive another preparation. Never
    # follow a working-tree symlink, or touch the ordinary clone's .git directory.
    for path in git.worktree.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    git.run("reset", "--hard", base)


def _text(git: _Git, entry: tuple, code: str) -> bytes:
    data = git.run("cat-file", "blob", entry[1]).stdout
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise SyncError(code, "Only UTF-8 text content conflicts are supported.") from None
    if b"\0" in data:
        raise SyncError(code, "Binary conflicts require manual integration.")
    return data


def _migrate_fixture_directives(git: _Git, upstream: str, automatic: dict) -> None:
    # Follow only an unambiguous convention in upstream's own fixtures. The old
    # harness cannot read //@ directives, so do not migrate ahead of upstream.
    for directory in ("tests/source/configs", "tests/target/configs"):
        modern = git.run(
            "grep", "-q", "-E", "^//@ rustfmt-[A-Za-z_]+: [^[:space:]]+\r?$",
            upstream, "--", directory, allowed=(0, 1),
        )
        legacy = git.run(
            "grep", "-q", "-E", r"^[[:space:]]*//[[:space:]]*rustfmt-[A-Za-z_]+[[:space:]]*:",
            upstream, "--", directory, allowed=(0, 1),
        )
        if modern.returncode != 0 or legacy.returncode == 0:
            return

    for path, entry in automatic.items():
        if not (_protected(path) and path.startswith("tests/") and path.endswith(".rs")):
            continue
        if entry[0] not in _REGULAR:
            continue
        original = _text(git, entry, "protected-path")
        lines = original.splitlines(keepends=True)
        for index, line in enumerate(lines):
            content = line.removesuffix(b"\n").removesuffix(b"\r")
            if content.strip() and not content.startswith(b"//"):
                break
            if _LEGACY_DIRECTIVE.fullmatch(content):
                lines[index] = line[:2] + b"@" + line[2:]
        migrated = b"".join(lines)
        if migrated == original:
            continue
        workfile = git.worktree / path
        if not stat.S_ISREG(workfile.lstat().st_mode):
            raise SyncError("protected-path", "Fixture migration requires an ordinary file.")
        workfile.write_bytes(migrated)
        # Hash exact bytes, without repository-controlled filters or EOL changes.
        oid = git.run("hash-object", "-w", "--stdin", data=migrated).stdout.decode("ascii").strip()
        git.run("update-index", "--cacheinfo", f"{entry[0]},{oid},{path}")
        automatic[path] = (entry[0], oid)


@dataclass
class _Merge:
    kind: str
    original: dict
    automatic: dict
    conflicts: dict

    def metadata(self) -> dict:
        return {
            "kind": self.kind,
            "conflicts": sorted(self.conflicts),
            "review_paths": _review(_changed(self.original, self.automatic) | self.conflicts.keys()),
        }


def _automatic(git: _Git, base: str, upstream: str) -> _Merge:
    original = _tree(git, base)
    _tree(git, upstream)  # Reject unsafe names before any checkout/merge.
    _reset(git, base)
    ancestor = git.run("merge-base", "--is-ancestor", upstream, base, allowed=(0, 1))
    if ancestor.returncode == 0:
        return _Merge("noop", original, original.copy(), {})
    result = git.run("merge", "--no-commit", "--no-ff", "--no-edit", upstream, allowed=None)
    index = _index(git)
    automatic = {path: stages[0] for path, stages in index.items() if 0 in stages}
    unmerged = {path: stages for path, stages in index.items() if any(stage != 0 for stage in stages)}
    if result.returncode not in (0, 1) or (result.returncode != 0 and not unmerged) or (result.returncode == 0 and unmerged):
        raise SyncError("merge-infrastructure", "Git merge failed without a supported conflict result.")
    _preserve(original, automatic)
    for path in _changed(original, automatic) | unmerged.keys():
        for entry in (original.get(path), automatic.get(path)):
            if entry is not None and entry[0] not in _REGULAR:
                raise SyncError("manual-conflict-type", "Changed symlinks/submodules require manual integration.")
    conflicts = {}
    for path, stages in unmerged.items():
        modes = {entry[0] for entry in stages.values()}
        if set(stages) not in ({1, 2, 3}, {2, 3}) or len(modes) != 1 or not modes <= _REGULAR:
            raise SyncError("manual-conflict-type", "Only same-mode content/add-add conflicts are supported.")
        for entry in stages.values():
            _text(git, entry, "manual-conflict-type")
        workfile = git.worktree / path
        if not stat.S_ISREG(workfile.lstat().st_mode):
            raise SyncError("manual-conflict-type", "Conflict workfiles must be ordinary files.")
        try:
            workfile.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise SyncError("manual-conflict-type", "Conflict workfiles must be UTF-8 text.") from None
        conflicts[path] = next(iter(modes))
    if conflicts:
        attributes = git.run(
            "check-attr", "-z", "merge", "--", *sorted(conflicts)
        ).stdout.split(b"\0")
        for offset in range(0, len(attributes) - 1, 3):
            _, _, value = attributes[offset:offset + 3]
            if value in (b"unset", b"binary"):
                raise SyncError("manual-conflict-type", "Binary merge attributes require manual integration.")
    _migrate_fixture_directives(git, upstream, automatic)
    kind = "conflict" if conflicts else "clean"
    return _Merge(kind, original, automatic, conflicts)


def _candidate(git: _Git, merge: _Merge) -> tuple[str, dict]:
    index = _index(git, code="invalid-resolution")
    if any(set(stages) != {0} for stages in index.values()):
        raise SyncError("unresolved-conflicts", "Unmerged index entries remain.")
    tree = git.run("write-tree").stdout.decode("ascii").strip()
    candidate = _tree(git, tree, code="invalid-resolution")
    _preserve(merge.automatic, candidate)
    for path in merge.automatic.keys() | candidate.keys():
        if path not in merge.conflicts and candidate.get(path) != merge.automatic.get(path):
            raise SyncError("out-of-scope", "Candidate differs from the automatic merge outside original conflicts.")
    for path, mode in merge.conflicts.items():
        entry = candidate.get(path)
        if entry is None or entry[0] != mode:
            raise SyncError("invalid-resolution", "Content resolutions must preserve the original regular-file mode.")
        data = _text(git, entry, "invalid-resolution")
        if _MARKERS.search(data):
            raise SyncError("unresolved-conflicts", "Candidate still contains conflict markers.")
    if merge.kind == "conflict" and candidate == merge.original:
        raise SyncError("empty-patch", "A resolution must not discard every upstream change.")
    return tree, candidate


def prepare(repo: Path, base: str, upstream: str) -> dict:
    """Reset a disposable clone and expose its safe automatic merge/conflicts."""
    with _session(repo, base, upstream) as git:
        merge = _automatic(git, base, upstream)
        git.publish(base, upstream if merge.kind != "noop" else None)
        metadata = {**merge.metadata(), "base": base, "upstream": upstream}
        if not merge.conflicts:
            metadata["tree"] = git.run("write-tree").stdout.decode("ascii").strip()
        return metadata


def write_patch(repo: Path, base: str, upstream: str, destination: Path) -> dict:
    """Stage and validate a resolution, then export the full base-to-candidate diff."""
    destination = Path(destination).absolute()
    resolved = destination.resolve()
    root = Path(repo).resolve()
    if resolved == root or root in resolved.parents:
        raise SyncError("artifact-path", "The patch artifact must be outside the disposable clone.")
    with _session(repo, base, upstream, separate=True) as reference:
        merge = _automatic(reference, base, upstream)
    if merge.kind == "noop":
        raise SyncError("noop", "There is no upstream tree change to export.")
    with _session(repo, base, upstream, copy_index=True) as git:
        git.run("add", "--all", "--", ".", code="invalid-resolution")
        tree, candidate = _candidate(git, merge)
        patch = git.run(
            "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/", base, tree, "--",
        ).stdout
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic(destination, patch)
        except OSError:
            raise SyncError("git-infrastructure", "Patch artifact could not be written.") from None
        git.publish(base, upstream)
        return {
            **merge.metadata(), "base": base, "upstream": upstream, "tree": tree,
            "review_paths": _review(_changed(merge.original, candidate)),
            "patch_sha256": hashlib.sha256(patch).hexdigest(), "patch_bytes": len(patch),
        }


def materialize(repo: Path, base: str, upstream: str, patch: Path) -> dict:
    """Independently validate an untrusted full patch and create a two-parent merge."""
    try:
        patch = Path(patch)
        if patch.is_symlink() or not patch.is_file():
            raise SyncError("invalid-patch", "The patch must be an ordinary file.")
        data = patch.read_bytes()
    except OSError:
        raise SyncError("invalid-patch", "The patch artifact cannot be read.") from None
    if data and not data.startswith(b"diff --git "):
        raise SyncError("invalid-patch", "A complete Git patch is required.")
    with _session(repo, base, upstream) as git:
        merge = _automatic(git, base, upstream)
        if merge.kind == "noop":
            raise SyncError("noop", "A no-op upstream merge cannot be published.")
        if not data and (merge.kind != "clean" or merge.automatic != merge.original):
            raise SyncError("invalid-patch", "An empty patch cannot omit upstream changes or resolve conflicts.")
        _reset(git, base)
        if data:
            summary = git.run("apply", "--numstat", "-z", "-", data=data, code="invalid-patch").stdout
            allowed = _changed(merge.original, merge.automatic) | merge.conflicts.keys()
            records = [record for record in summary.split(b"\0") if record]
            if not records:
                raise SyncError("invalid-patch", "The artifact contains no file changes.")
            for record in records:
                fields = record.split(b"\t", 2)
                if len(fields) != 3:
                    raise SyncError("invalid-patch", "Unsupported patch path metadata.")
                path = _path(fields[2], "invalid-patch")
                if _protected(path) and path not in allowed:
                    raise SyncError("protected-path", "Patch touches fork preservation files.")
                if path not in allowed:
                    raise SyncError("out-of-scope", "Patch touches a path outside the automatic merge and original conflicts.")
            git.run("apply", "--check", "--index", "--binary", "--whitespace=nowarn", "-", data=data, code="invalid-patch")
            git.run("apply", "--index", "--binary", "--whitespace=nowarn", "-", data=data, code="invalid-patch")
        tree, candidate = _candidate(git, merge)
        timestamps = [
            int(git.run("show", "--no-patch", "--no-show-signature", "--format=%ct", revision).stdout)
            for revision in (base, upstream)
        ]
        date = f"{max(timestamps)} +0000"
        commit = git.run(
            "commit-tree", tree, "-p", base, "-p", upstream,
            data=f"Merge pinned upstream {upstream} into fork\n".encode("ascii"),
            extra_env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        ).stdout.decode("ascii").strip()
        git.publish(commit)
        return {
            **merge.metadata(), "base": base, "upstream": upstream, "commit": commit, "tree": tree,
            "review_paths": _review(_changed(merge.original, candidate)),
        }
