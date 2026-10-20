"""GitHub coordination for upstream-sync; all candidate code stays outside this module."""

import argparse
import base64
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .git_ops import SyncError, materialize, prepare, write_patch

UPSTREAM = "rust-lang/rustfmt"
BRANCH = "automation/upstream-sync"
STATE_MARKER = "<!-- rustfmt-upstream-sync-state-v1\n"
PR_MARKER = "<!-- rustfmt-upstream-sync-pr-v1 -->"
COMMIT_MARKER = "Upstream-Sync: v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_ATTEMPTS = 2
VALIDATION_IMAGE = "rust:1.90.0-bookworm@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f"


class GitHub:
    def __init__(self, repository, token, api_url="https://api.github.com"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise SyncError("configuration", "Invalid repository name")
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.token = token

    def request(self, method, path, data=None, *, missing=False):
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            self.api_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "rustfmt-upstream-sync",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404 and missing:
                return None
            # Never include response bodies or request headers in reports.
            raise SyncError("github_api", f"GitHub API returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise SyncError("github_api", "GitHub API transport or response failure") from None

    def repo(self, suffix, method="GET", data=None, *, missing=False):
        return self.request(method, f"/repos/{self.repository}/{suffix}", data, missing=missing)

    def pages(self, suffix):
        separator = "&" if "?" in suffix else "?"
        page = 1
        while True:
            values = self.repo(f"{suffix}{separator}per_page=100&page={page}")
            yield from values
            if len(values) < 100:
                return
            page += 1

    def main_sha(self):
        return checked_sha(self.repo("git/ref/heads/main")["object"]["sha"])

    def upstream_sha(self):
        result = self.request("GET", f"/repos/{UPSTREAM}/git/ref/heads/main")
        return checked_sha(result["object"]["sha"])

    def sync_pr(self):
        owner = self.repository.split("/")[0]
        query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{BRANCH}", "base": "main"})
        prs = list(self.pages("pulls?" + query))
        if len(prs) > 1:
            raise SyncError("branch_ownership", "More than one open sync PR")
        if prs and PR_MARKER not in (prs[0].get("body") or ""):
            raise SyncError("branch_ownership", "Reserved sync branch has an unmanaged PR")
        return prs[0] if prs else None


def checked_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SyncError("invalid_sha", "Expected a complete lowercase commit SHA")
    return value


def emit(**values):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                if isinstance(value, bool):
                    value = str(value).lower()
                value = str(value)
                if "\n" in value or "\r" in value:
                    raise SyncError("invalid_output", "Multiline workflow output rejected")
                stream.write(f"{key}={value}\n")


def run_url():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repository}/actions/runs/{run_id}"


def state_title(base, upstream):
    return f"[upstream-sync] {base[:12]} / {upstream[:12]}"


def decode_state(issue, base, upstream):
    if issue.get("user", {}).get("login") != "github-actions[bot]":
        raise SyncError("state_ownership", "Tracking issue is not owned by Actions")
    body = issue.get("body") or ""
    if body.count(STATE_MARKER) != 1:
        raise SyncError("state_corrupt", "Missing or duplicate tracking state")
    encoded = body.split(STATE_MARKER, 1)[1].split("\n-->", 1)[0]
    try:
        state = json.loads(encoded)
    except json.JSONDecodeError:
        raise SyncError("state_corrupt", "Invalid tracking state") from None
    if (
        state.get("base") != base
        or state.get("upstream") != upstream
        or type(state.get("attempts_reserved")) is not int
        or not 0 <= state["attempts_reserved"] <= MAX_ATTEMPTS
        or state.get("status") not in {"reserved", "failed", "published"}
    ):
        raise SyncError("state_corrupt", "Tracking state does not match the requested pair")
    return state


def state_body(state, reason, paths=(), pr_url=None):
    lines = [
        "# Upstream synchronization",
        "",
        f"- Fork base: `{state['base']}`",
        f"- Upstream: `{state['upstream']}`",
        f"- Status: **{state['status']}**",
        f"- Agent attempt slots reserved: **{state['attempts_reserved']} / {MAX_ATTEMPTS}**",
        f"- Result: {reason}",
        f"- Run: {run_url()}",
    ]
    if pr_url:
        lines.append(f"- Pull request: {pr_url}")
    if not paths:
        paths = state.get("paths", ())
    if paths:
        lines.extend(["", "Paths requiring attention:"])
        lines.extend(f"- <code>{html.escape(path)}</code>" for path in paths)
    lines.extend([
        "",
        "An identical pair is not automatically retried. After addressing the report, use",
        "the manual workflow with **retry** enabled to authorize a fresh two-attempt budget.",
        "A cancellation conservatively retains its reservation. Raw model logs are not published.",
        "",
        STATE_MARKER + json.dumps(state, sort_keys=True) + "\n-->",
        "",
    ])
    return "\n".join(lines)


def find_state(api, base, upstream):
    title = state_title(base, upstream)
    matches = [issue for issue in api.pages("issues?state=all&sort=created&direction=desc")
               if "pull_request" not in issue and issue.get("title") == title]
    if len(matches) > 1:
        raise SyncError("state_corrupt", "Duplicate tracking issues")
    if not matches:
        return None, None
    return matches[0], decode_state(matches[0], base, upstream)


def reserve(api, base, upstream, *, retry, attempts):
    issue, state = find_state(api, base, upstream)
    if issue and not retry:
        return issue, state, False
    state = {"base": base, "upstream": upstream, "attempts_reserved": attempts, "status": "reserved"}
    payload = {"body": state_body(state, "Reserved for this run."), "state": "open"}
    if issue:
        issue = api.repo(f"issues/{issue['number']}", "PATCH", payload)
    else:
        issue = api.repo("issues", "POST", {"title": state_title(base, upstream), "body": payload["body"]})
    return issue, state, True


def update_state(api, issue, state, *, status, reason, paths=(), pr_url=None):
    state = dict(state, status=status)
    api.repo(f"issues/{issue['number']}", "PATCH", {
        "body": state_body(state, reason, paths, pr_url),
        "state": "closed" if status == "published" else "open",
    })


def check_environment(api, name, *, require_review):
    environment = api.repo("environments/" + urllib.parse.quote(name, safe=""))
    policy = environment.get("deployment_branch_policy") or {}
    if not policy.get("custom_branch_policies") or policy.get("protected_branches"):
        raise SyncError("environment_protection", f"{name} must allow only the main branch")
    rules = api.repo("environments/" + urllib.parse.quote(name, safe="") + "/deployment-branch-policies")
    branches = rules.get("branch_policies", [])
    if len(branches) != 1 or branches[0].get("name") != "main" or branches[0].get("type") != "branch":
        raise SyncError("environment_protection", f"{name} must allow exactly main, with no tag policy")
    if require_review:
        reviewers = [rule for rule in environment.get("protection_rules", [])
                     if rule.get("type") == "required_reviewers" and rule.get("reviewers")]
        if not reviewers:
            raise SyncError("environment_protection", f"{name} needs required reviewers")


def git(repo, *args, auth=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in {"GH_TOKEN", "GITHUB_TOKEN", "AI_API_KEY"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="upstream-sync[bot]", GIT_AUTHOR_EMAIL="upstream-sync@users.noreply.github.com",
               GIT_COMMITTER_NAME="upstream-sync[bot]", GIT_COMMITTER_EMAIL="upstream-sync@users.noreply.github.com")
    if auth:
        header = base64.b64encode(("x-access-token:" + auth).encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + header)
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
                             "-C", str(repo), *args], env=env, capture_output=True)
    if result.returncode:
        raise SyncError("git_transport", "Git command failed; credentials and raw output withheld")
    return result.stdout.decode("utf-8").strip()


def checkout(source, destination, base, upstream):
    if destination.exists():
        raise SyncError("workspace_exists", "Disposable checkout destination already exists")
    git(source, "clone", "--no-hardlinks", "--no-checkout", str(source.resolve()), str(destination.resolve()))
    git(destination, "fetch", "--no-tags", f"https://github.com/{UPSTREAM}.git",
        f"{checked_sha(upstream)}:refs/upstream-sync/upstream")
    git(destination, "update-ref", "refs/upstream-sync/base", checked_sha(base))


def plan(api, source, repo, patch, *, retry):
    emit(active=False, resolve=False, review=False, issue="")
    base, upstream = api.main_sha(), api.upstream_sha()
    emit(base=base, upstream=upstream)
    if base != os.environ["GITHUB_SHA"]:
        raise SyncError("stale_base", "Main advanced after this workflow was selected")
    pr = api.sync_pr()
    if pr and not pr.get("draft"):
        print("The existing sync PR is under review; leaving its branch unchanged.")
        return
    checkout(source, repo, base, upstream)
    try:
        merged = prepare(repo, base, upstream)
    except SyncError as error:
        issue, state, reserved = reserve(api, base, upstream, retry=retry, attempts=0)
        if reserved:
            emit(issue=issue["number"])
            update_state(api, issue, state, status="failed", reason=f"Merge preparation stopped ({error.code}).")
        raise
    if merged["kind"] == "noop":
        print("The fork already contains the selected upstream commit.")
        return
    issue, previous = find_state(api, base, upstream)
    if issue and not retry:
        print("This exact fork/upstream pair already has a reservation or result; manual retry is required.")
        return
    # Reserve before handing control to any job that can contact the model.
    conflict = merged["kind"] == "conflict"
    issue, state, _ = reserve(api, base, upstream, retry=retry, attempts=0)
    state["paths"] = merged["conflicts"] or merged["review_paths"]
    emit(issue=issue["number"])
    try:
        check_environment(api, "upstream-sync", require_review=False)
        review = bool(merged["review_paths"])
        if review:
            check_environment(api, "upstream-sync-review", require_review=True)
        # The model key is environment-scoped and validated by the resolver.
        if conflict and not os.environ.get("AI_BASE_URL"):
            raise SyncError("configuration", "Conflicts need AI_BASE_URL")
        state["attempts_reserved"] = MAX_ATTEMPTS if conflict else 0
        if not conflict:
            patch.parent.mkdir(parents=True, exist_ok=True)
            write_patch(repo, base, upstream, patch)
        update_state(api, issue, state, status="reserved", reason="Prepared pinned inputs; execution pending.")
        emit(active=True, resolve=conflict, review=review)
        print(f"Prepared {merged['kind']} upstream sync; tracking issue #{issue['number']}.")
    except SyncError as error:
        update_state(api, issue, state, status="failed", reason=f"Preparation stopped ({error.code}).",
                     paths=merged["conflicts"])
        raise


def validate(source, repo, patch, base, upstream):
    checkout(source, repo, base, upstream)
    candidate = materialize(repo, base, upstream, patch)
    runtime = shutil.which("docker")
    if not runtime:
        raise SyncError("sandbox_unavailable", "Validation requires Docker; there is no host fallback")
    scratch = repo.parent / "validation-storage"
    scratch.mkdir()
    mounts = []
    for name in ("cargo", "rustup", "target"):
        directory = scratch / name
        directory.mkdir()
        mounts.extend(["--mount", f"type=bind,src={directory.resolve()},dst=/{name}"])
    command = [runtime, "run", "--rm", "--init", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=512", "--memory=6g", "--cpus=2",
               "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
               "--mount", f"type=bind,src={repo.resolve()},dst=/work",
               "--mount", f"type=bind,src={(source / 'ci').resolve()},dst=/trusted/ci,readonly",
               *mounts, "--workdir", "/work", "--env", "CARGO_HOME=/cargo", "--env", "RUSTUP_HOME=/rustup",
               "--env", "CARGO_TARGET_DIR=/target", "--env", "HOME=/tmp", "--env", "CFG_RELEASE_CHANNEL=nightly",
               "--env", "RUSTFLAGS=-D warnings", VALIDATION_IMAGE,
               "cargo", "run", "--locked", "--manifest-path", "/trusted/ci/Cargo.toml", "build-and-test"]
    try:
        result = subprocess.run(command, timeout=5400)
    except (OSError, subprocess.TimeoutExpired):
        raise SyncError("validation_runtime", "Validation container could not complete") from None
    if result.returncode:
        raise SyncError("validation_failed", "Candidate did not pass secret-free nightly validation")
    print(f"Candidate tree {candidate['tree']} passed nightly validation.")


def publish(api, source, repo, patch, base, upstream):
    checkout(source, repo, base, upstream)
    candidate = materialize(repo, base, upstream, patch)
    if api.main_sha() != base:
        raise SyncError("stale_base", "Main advanced; refusing to publish stale validation")
    pr = api.sync_pr()
    if pr and not pr.get("draft"):
        raise SyncError("review_in_progress", "The existing PR is no longer a draft")
    reference = api.repo("git/ref/heads/" + BRANCH, missing=True)
    previous = reference["object"]["sha"] if reference else ""
    if previous:
        commit = api.repo("git/commits/" + checked_sha(previous))
        if COMMIT_MARKER not in commit.get("message", "").splitlines():
            raise SyncError("branch_ownership", "Reserved sync branch contains an unmanaged commit")
    message = (f"Merge {UPSTREAM} main\n\n{COMMIT_MARKER}\n"
               f"Fork-Base: {base}\nUpstream-Commit: {upstream}\n")
    # The publisher independently creates the commit from a verified tree and pinned parents.
    commit = git(repo, "commit-tree", candidate["tree"], "-p", base, "-p", upstream, "-m", message)
    if api.main_sha() != base:
        raise SyncError("stale_base", "Main advanced immediately before publication")
    git(repo, "push", f"--force-with-lease=refs/heads/{BRANCH}:{previous}",
        f"https://github.com/{api.repository}.git", f"{commit}:refs/heads/{BRANCH}", auth=api.token)
    title = f"Sync upstream rustfmt ({upstream[:12]})"
    body = "\n".join([
        PR_MARKER, "", f"Merge `{UPSTREAM}@{upstream}` into fork base `{base}`.", "",
        "- Secret-free Linux nightly validation passed before publication.",
        "- The existing PR CI matrix must pass before a human merges this PR.",
        "- Review FORK.md preservation and any workflow/release changes.",
        "- Use a merge commit, not squash/rebase, to preserve upstream ancestry.",
        f"- Workflow run: {run_url()}", "",
        "This reserved branch is maintained by automation; do not add manual commits.",
    ])
    if pr:
        pr = api.repo(f"pulls/{pr['number']}", "PATCH", {"title": title, "body": body})
    else:
        pr = api.repo("pulls", "POST", {"title": title, "body": body, "head": BRANCH,
                                                "base": "main", "draft": True, "maintainer_can_modify": False})
    if api.main_sha() != base:
        raise SyncError("stale_base", "Main advanced during publication; draft PR requires fresh validation")
    emit(pr_url=pr["html_url"])
    print(f"Published draft PR #{pr['number']}; human review and full PR CI remain required.")


def report(api, base, upstream, issue_number, results, pr_url):
    issue = api.repo(f"issues/{issue_number}")
    state = decode_state(issue, base, upstream)
    if results.get("publish", {}).get("result") == "success":
        update_state(api, issue, state, status="published", reason="Validated candidate published as a draft PR.",
                     pr_url=pr_url)
        return
    failed = [name for name, result in results.items() if result.get("result") in {"failure", "cancelled"}]
    reason = "Execution stopped in: " + ", ".join(sorted(failed)) if failed else "Execution did not publish a candidate."
    update_state(api, issue, state, status="failed", reason=reason + " Inspect the linked run; manual retry is required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "checkout", "validate", "publish", "check-environment", "report"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--patch", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--upstream")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "checkout":
            checkout(args.source, args.repo, checked_sha(args.base), checked_sha(args.upstream))
            return
        if args.command == "validate":
            validate(args.source, args.repo, args.patch, checked_sha(args.base), checked_sha(args.upstream))
            return
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"],
                     os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        if args.command == "plan":
            plan(api, args.source, args.repo, args.patch, retry=args.retry)
        elif args.command == "publish":
            publish(api, args.source, args.repo, args.patch, checked_sha(args.base), checked_sha(args.upstream))
        elif args.command == "check-environment":
            review = os.environ.get("REQUIRES_REVIEW") == "true"
            check_environment(api, "upstream-sync-review" if review else "upstream-sync", require_review=review)
        else:
            report(api, checked_sha(args.base), checked_sha(args.upstream), int(os.environ["PAIR_ISSUE"]),
                   json.loads(os.environ["JOB_RESULTS"]), os.environ.get("PR_URL", ""))
    except SyncError as error:
        print(f"Upstream sync stopped ({error.code}); raw provider/Git output is withheld.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
