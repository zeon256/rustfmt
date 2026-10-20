# Fork preservation inventory

This fork tracks [rust-lang/rustfmt](https://github.com/rust-lang/rustfmt) while
retaining the customizations below. This document records what an upstream merge
must preserve; it is not a patch replay queue. Git records implementation history,
and the existing formatting fixtures record expected output.

## Tracking baseline

| Item | Inventoried revision |
| --- | --- |
| Fork | `zeon256/rustfmt`, branch `main` |
| Published fork tip | `1fff67c20d11171129d66da968efe91d6aba5526` (v1.12.1) |
| Upstream | `rust-lang/rustfmt`, branch `main` |
| Upstream snapshot inspected | `58e7ab3f7b7eee99a344cf2bac3c44a433ce7ef4` |
| Most recent common ancestor | `677b954590cc0d5e86cb97e7db4e4b5fb6290add` |

The owner-specified patch range is inclusive:
`687a2d46bf12e1dff4c05860f4b9d30dee724120` through
`1fff67c20d11171129d66da968efe91d6aba5526`. It introduces F003 and F005. F001,
F002, and the original F004 release setup predate that range and are recorded as
inherited fork differences, not changes introduced within it.

The range also contains release metadata commits
[`f04194ea`](https://github.com/zeon256/rustfmt/commit/f04194ea3e56c27fd346c0bff22271ae6d1eae46)
and [`1fff67c2`](https://github.com/zeon256/rustfmt/commit/1fff67c20d11171129d66da968efe91d6aba5526),
and integration merges
[`9019659e`](https://github.com/zeon256/rustfmt/commit/9019659ed4350f830b0808e5f89d24e46a0f04d6)
and [`69189aa5`](https://github.com/zeon256/rustfmt/commit/69189aa5a86ecf91a9e3d6fe20aec413d5789121).
These are history, not additional formatter features to reapply after each sync.

## Shared formatting contract

All three spacing options are unstable, nightly-only configuration options. They
accept non-negative integer counts. An unset option leaves ordinary rustfmt
spacing in control; an explicitly configured `0` enforces zero blank lines at
eligible boundaries. Exact configured counts take precedence over the global
`blank_lines_lower_bound` and `blank_lines_upper_bound` settings.

Preserve comments and attributes with the statement or item they describe. Honor
`rustfmt::skip` and selected file-line boundaries rather than forcing spacing
through skipped or unformatted code. Formatting must remain idempotent.

The shared implementation includes [configuration registration](src/config/mod.rs),
[option defaults](src/config/options.rs), and
[gap/comment formatting](src/missed_spans.rs). These are touchpoints, not a fixed
file layout that upstream refactoring must retain.

## F001 — Blank lines before control-flow statements

- **Origin:** inherited;
  [`523874ed`](https://github.com/zeon256/rustfmt/commit/523874eddb1dcbb1a106fee66eb96b75cda74b10).
- **Configuration:**
  [`blank_lines_before_control_flow_statements`](Configurations.md#blank_lines_before_control_flow_statements).
- **Required behavior:** enforce the exact count before standalone `if`, `match`,
  `loop`, `while`, and `for` statements, except the first statement of a block.
  Do not insert spacing inside an `if`/`else` chain or around control-flow
  expressions embedded in another statement, such as `let x = if ...`.
  Leading comments and attributes stay attached to the control-flow statement.
- **Implementation touchpoints:** [statement classification](src/stmt.rs) and
  [statement boundary handling](src/visitor.rs), plus the shared implementation.
- **Existing validation:**
  [source fixtures](tests/source/configs/blank_lines_before_control_flow_statements/)
  and [expected output](tests/target/configs/blank_lines_before_control_flow_statements/).
  `0.rs` checks explicit zero against the global lower bound; `1.rs` covers the
  five statement kinds, nesting, first-in-block behavior, embedded expressions,
  labels, comments, attributes, and skipping; `2.rs` checks a larger exact count.
- **Upstream status / retirement:** fork-only at the inspected upstream snapshot.
  Retire the custom implementation only when upstream offers equivalent
  configuration and preserves these behaviors, demonstrated by the fixtures.

## F002 — Blank lines between items

- **Origin:** inherited;
  [`355315ec`](https://github.com/zeon256/rustfmt/commit/355315ece32eea167ecd889a3997a567309bde9b).
- **Configuration:**
  [`blank_lines_between_items`](Configurations.md#blank_lines_between_items).
- **Required behavior:** enforce the exact count before qualifying non-first
  items: `fn`, `impl`, `struct`, `enum`, `union`, `trait`, and `type`. Apply the
  corresponding rule to later function and type members of `trait`/`impl` bodies.
  Leave the first item of each container alone. Do not extend the rule to `use`,
  `extern crate`, `mod`, `const`, `static`, or macro-call items. Keep leading
  comments, documentation, and attributes attached to their item, and preserve
  spacing semantics when impl items are reordered.
- **Implementation touchpoints:** [item boundaries](src/visitor.rs),
  [associated-item formatting and reordering](src/items.rs),
  [item reordering](src/reorder.rs), and [macro formatting](src/macros.rs), plus
  the shared implementation.
- **Existing validation:**
  [source fixtures](tests/source/configs/blank_lines_between_items/)
  and [expected output](tests/target/configs/blank_lines_between_items/).
  `0.rs` checks explicit zero against the global lower bound; `1.rs` covers item
  kinds, associated and local items, comments, attributes, and skipping; `2.rs`
  checks an exact count above the global upper bound. These fixtures do not
  explicitly enable `reorder_impl_items`; preservation of that path also needs
  inspection or a targeted runtime check when it changes.
- **Upstream status / retirement:** fork-only at the inspected upstream snapshot.
  Retire only when upstream provides equivalent configuration and passes the
  preservation checks, including any affected reordered-item path.

## F003 — Blank lines after control-flow statements

- **Origin:** within the specified range;
  [`addb940d`](https://github.com/zeon256/rustfmt/commit/addb940d3e52321182aeb55612da1e27dd435687).
- **Configuration:**
  [`blank_lines_after_control_flow_statements`](Configurations.md#blank_lines_after_control_flow_statements).
- **Required behavior:** enforce the exact count between a standalone `if`,
  `match`, `loop`, `while`, or `for` statement and its following sibling. Do not
  add spacing before a closing brace, inside an `if`/`else` chain, or after a
  control-flow expression embedded in another statement. Place spacing before
  the successor's entire leading comment/attribute group. When F001 and F003
  both apply to the same boundary, use the larger enabled count; explicit zero
  must not cancel a larger enabled count. Preserve the existing interaction
  with F002 when the successor is a local item.
- **Implementation touchpoints:** [statement classification](src/stmt.rs) and
  [sibling boundary handling](src/visitor.rs), plus the shared implementation.
- **Existing validation:**
  [source fixtures](tests/source/configs/blank_lines_after_control_flow_statements/)
  and [expected output](tests/target/configs/blank_lines_after_control_flow_statements/).
  `0.rs`, `1.rs`, and `2.rs` cover exact counts, global bounds, statement kinds,
  nesting, comments, attributes, skipped statements, macros, and reordered uses.
  `overlap-before.rs`, `overlap-after.rs`, and `overlap-zero.rs` cover F001/F003
  precedence. `item-boundary.rs` covers F002/F003 interaction.
- **Upstream status / retirement:** fork-only at the inspected upstream snapshot.
  Retire only when upstream provides equivalent configuration and preserves all
  of these boundary and precedence behaviors.

## F004 — Independent GitHub-binary releases

- **Origin:** inherited release setup in
  [`523874ed`](https://github.com/zeon256/rustfmt/commit/523874eddb1dcbb1a106fee66eb96b75cda74b10),
  with git-only release detection in
  [`6841d40a`](https://github.com/zeon256/rustfmt/commit/6841d40a0ccc42a2dc94996787719a27d900e211)
  and the release-plz compatibility update in
  [`964e83d6`](https://github.com/zeon256/rustfmt/commit/964e83d6910834731ac19e1a2fb4d0201f060975).
- **Required behavior:** release this fork through GitHub tags, releases, and
  downloadable binaries, never through crates.io. Use git tags rather than the
  upstream package's registry version to determine fork releases. Maintain the
  fork changelog and `v{{ version }}` tag/release naming. Keep both release PR
  generation and release creation working.
- **Implementation touchpoints:** [release configuration](release-plz.toml),
  [release orchestration](.github/workflows/release-plz.yml), and
  [binary packaging](.github/workflows/release-binaries.yml). The inventoried
  workflow uses release-plz `0.3.169`; version upgrades are allowed, but must
  preserve the behavior rather than restore the older package-verification
  problem described by `964e83d6`.
- **Existing validation:** the release workflows are the execution paths; there
  is no dedicated release regression suite recorded here. Validate orchestration
  changes through a reviewed release PR/run. Verify that the selected tag is
  built with the toolchain in [rust-toolchain.toml](rust-toolchain.toml), and that
  Linux x86-64, macOS x86-64, Windows GNU x86-64, and Windows MSVC x86-64 archives
  contain `rustfmt`, `cargo-fmt`, `rustfmt-format-diff`, and `git-rustfmt`, with
  the appropriate executable suffixes.
- **Retirement:** independent GitHub distribution and the no-registry-publishing
  rule are intentional fork policy, not temporary upstream compatibility fixes.
  Do not remove them as routine conflict resolution.

## F005 — Release-plz rustdoc compatibility workaround

- **Origin:** start of the specified range;
  [`687a2d46`](https://github.com/zeon256/rustfmt/commit/687a2d46bf12e1dff4c05860f4b9d30dee724120).
- **Required behavior:** keep fork release generation usable with the pinned
  nightly toolchain. `semver_check = false` avoids the documented incompatibility
  between cargo-semver-checks bundled with release-plz `0.3.169` and rustdoc JSON
  v61. This does not disable formatter preservation checks.
- **Implementation touchpoints:** [release configuration](release-plz.toml),
  [release-plz version selection](.github/workflows/release-plz.yml), and
  [compiler toolchain](rust-toolchain.toml).
- **Existing validation:** no dedicated regression test. Inspect release-plz
  execution when changing these versions or the workaround; a successful
  formatter build alone does not verify release-tool compatibility.
- **Retirement:** reconsider the workaround when release tooling and the pinned
  rustdoc format are compatible and that combination has been exercised. Do not
  infer compatibility merely from a toolchain or release-plz version bump.

## F006 — Guarded upstream synchronization

- **Origin:** the CI/CD change `oprlmvmt`, after the tracking baseline above.
- **Required behavior:** integrate pinned upstream commits through a single
  draft PR, preserving fork contracts and upstream ancestry. Resolve supported
  conflicts with bounded OpenCode execution; never give the agent publishing
  credentials, weaken preservation checks, or automatically merge the PR.
- **Implementation touchpoints:** [the scheduled workflow](.github/workflows/upstream-sync.yml),
  [Git verification](ci/upstream_sync/git_ops.py),
  [OpenCode isolation](ci/upstream_sync/agent.py), and
  [coordination and publishing](ci/upstream_sync/control.py).
- **Existing validation:** [automation checks](.github/workflows/upstream-sync-check.yml)
  exercise real temporary Git histories, durable retry state, publication races,
  process limits, and Docker isolation. The candidate also passes secret-free
  nightly validation before publication and the existing full PR CI afterward.
- **Retirement:** retain while this repository needs independent fork maintenance.
  Changes to credentials, isolation, review gates, or preservation policy require
  human review.

## Preservation checks

[The CI test workflow](.github/workflows/test.yml) runs
[the build-and-test driver](ci/src/build_and_test.rs) for nightly and stable
release-channel modes across its platform matrix. The nightly mode is necessary
for the unstable spacing options and idempotence checks. On a supported host,
the corresponding local command is:

```sh
CFG_RELEASE_CHANNEL=nightly RUSTFLAGS='-D warnings' \
  cargo run --locked --manifest-path ci/Cargo.toml build-and-test
```

[The formatter test runner](src/test/mod.rs) compares source fixtures with their
expected target files in `system_tests` and checks target-file idempotence in
`idempotence_tests`. [The contribution guide](Contributing.md#test-and-file-issues)
explains the fixture convention. Use the repository's other applicable CI checks
as well; this inventory does not replace them.

Do not delete or rewrite fork-specific expected outputs merely to make an
upstream sync pass. If upstream intentionally changes behavior at one of these
boundaries, report the difference and obtain review of the contract change.
Changes to release workflows and publishing policy also require human review.

## Maintaining this inventory

Keep entry IDs stable. Update an entry when its behavior, implementation
locations, validation, or upstream status changes; update the baseline revisions
when recording a newly reviewed upstream integration. Mark a retired customization
with the integrating commit and evidence of equivalence rather than silently
removing its history. Remove obsolete implementation code when upstream replaces
it; do not retain compatibility shims solely to preserve the old file layout.

A clean textual merge is not proof that these contracts survived. Preserve
upstream commit ancestry with merge commits rather than replaying this list or
squashing upstream-sync history.

## Operating upstream synchronization

[Upstream sync](.github/workflows/upstream-sync.yml) runs daily at 04:23 UTC and
supports a manual trigger. It runs only on `zeon256/rustfmt`'s `main` branch.
GitHub schedules are not a freshness guarantee; check that the scheduled workflow
remains enabled.

### One-time GitHub setup

Enable repository Issues: a bot-authored issue for each fork/upstream SHA pair is
the durable execution ledger and failure report. Do not delete or repurpose these
issues to reset an execution budget.

Create these Actions repository variables:

| Variable | Value |
| --- | --- |
| `AI_MODEL` | Optional; defaults to `glm-5.3` |
| `AI_BASE_URL` | Required for conflicts: the provider's HTTPS API base URL, without credentials, query, or fragment |
| `AI_API_STYLE` | Optional; defaults to `openai-compatible` |
| `AI_PROVIDER` | Optional; defaults to `sync`, the internal OpenCode provider alias |
| `SYNC_APP_CLIENT_ID` | Client ID of the GitHub App used to publish PRs |

The API style selects a built-in runtime, not an arbitrary plugin. Supported
values are `openai-compatible`, `openai-chat`, `openai-responses`, `anthropic`,
`anthropic-compatible`, and `google`. Set the endpoint appropriate to the
provider/account; no GLM vendor endpoint is hard-coded.

Store `AI_API_KEY` as an Actions **environment secret in `upstream-sync`**.
Only the resolver step receives it, after the job enters that environment.
Preparation uses non-secret configuration only; the resolver validates the key
before contacting the model. The key is not needed in `upstream-sync-review`.
A clean merge does not require a model key or endpoint.

Install a GitHub App on this repository with **Contents: write**,
**Pull requests: write**, and **Workflows: write** repository permissions.
Workflows permission is needed to push approved upstream workflow changes; it
does not grant the resolver a token. The publisher requests a short-lived token
for this repository only and revokes it at job completion. An App token also lets
the resulting PR trigger normal PR workflows without the approval requirement
associated with PRs created using `GITHUB_TOKEN`.

Configure two GitHub environments:

| Environment | Deployment restriction | Approval |
| --- | --- | --- |
| `upstream-sync` | Selected branches/tags: exactly one **branch** rule named `main`; no tag rules | Required reviewers optional |
| `upstream-sync-review` | The same exact `main` branch restriction | At least one required reviewer |

Store the App's PEM private key as the `SYNC_APP_PRIVATE_KEY` **environment
secret in both environments**, not as a repository secret. The branch restriction
keeps a PR workflow from accessing it merely by naming the environment.
Preparation and the publisher check the environment protection through the API;
missing or broader protection fails closed. For a sole maintainer, allow
self-review if necessary to explicitly approve manually triggered runs.

Protect `main` with human PR approval and required checks, including the existing
`test conclusion` gate and the new `automation-tests` check. Keep the project's
other required PR checks enabled. Do not enable squash/rebase merging for sync PRs.
Configure these settings before landing/enabling the scheduled workflow, then
exercise its manual trigger first.

### Execution and review

Only one sync workflow runs at a time. Preparation freezes the current `main` and
upstream `main` SHAs. An already-integrated upstream commit is a no-op; a
no-content upstream commit still gets a merge that records its ancestry.

Supported AI conflicts are same-mode regular UTF-8 file content and add/add
conflicts. Binary/non-UTF-8, rename/delete/type conflicts, changed symlinks or
submodules, unsafe paths, and resource-limit violations stop for manual handling.
Changes to `FORK.md` or the three fork-specific source/target fixture directories
also stop for manual integration, even if introduced upstream. Environment
approval alone does not override that preservation rule.

The agent uses OpenCode **2.0.28**, pinned by OCI digest, with a managed
configuration checked by the actual binary before credentials are supplied.
It runs as a non-root user in a read-only Docker container. Only eligible conflict
files have writable mounts; context is tracked regular source, without `.git`,
untracked files, or host symlinks. Shell, subagents, external file access, MCP,
skills, repository plugin discovery, and other network tools are disabled.
Only the configured model connection gets a credential. There is no host-execution
fallback if Docker or its resource controls are unavailable.
`HOME` points at the existing private `/state` tmpfs so OpenCode can start its
service without requiring pre-created user directories.

Only the verified candidate patch crosses jobs. Validation reconstructs the
merge independently and runs the trusted base CI driver against the candidate
in a separate container without model, GitHub-write, or release credentials.
The publisher reconstructs and verifies the patch again without executing
candidate code, confirms that `main` is still the pinned base, and updates only
`automation/upstream-sync` using a branch lease.

Changes under `.github/` or `ci/`, or to `release-plz.toml` or
`rust-toolchain.toml`, require the protected `upstream-sync-review` environment
approval **before the App credential is minted and the branch is published**.
Review the linked artifact/diff and run before approving infrastructure changes:
same-repository PR workflows can access repository-level credentials.

The PR remains a draft. Review the diff and preservation results, require the full
PR CI to pass, mark it ready, and merge it with a merge commit. Automation will not
rewrite a PR that is already marked ready for human review. Do not add manual
commits to the reserved automation branch; unexpected commits and branch races
are rejected rather than overwritten.

### Budgets and failed runs

The resolver permits at most **two fresh resolution attempts** within a
**20-minute total job timeout**, including setup. Attempts share one wall-clock
budget and the same permissions. An invalid proposal may get one fresh attempt
with its verification failure code; authentication/transport, sandbox, Git, and
unsupported-conflict failures do not trigger a repair invocation.

Before launching a resolver, the ledger reserves both attempt slots. Cancellation
or a lost completion report conservatively retains the reservation, even if
fewer model invocations actually ran. Scheduled runs do not retry the same
reserved, failed, or published SHA pair. A changed fork/upstream pair gets a new
budget. Use the manual **retry** input to explicitly re-arm an unchanged pair
after investigating it.

The bounds limit resolver invocations and elapsed time, not the provider's HTTP
request count or a precise monetary amount. OpenCode can retry individual API
requests internally; configure provider-side quotas for a hard spend limit.
Nightly validation has its own bounded job and is not another agent attempt.

Reports contain pinned SHAs, conflict/review paths, safe status codes, and run/PR
links. Raw model transcripts and provider logs are neither printed nor uploaded.
The candidate patch artifact is retained for seven days; the issue ledger
persists. If a workflow is cancelled before reporting, inspect the run linked by
the reserved issue before authorizing another budget.

### Local verification

Run the standard-library regression suite from the repository root:

```sh
python3 -B -m unittest discover -s ci/upstream_sync -t . -p 'test_*.py' -v
```

On a Linux Docker host, also exercise the pinned image's configuration and actual
kernel-enforced filesystem/credential isolation:

```sh
UPSTREAM_SYNC_CONTAINER_TESTS=1 \
  python3 -B -m unittest ci.upstream_sync.test_agent.ActualContainerInvariants -v
```

The automation-check workflow enables these container checks on GitHub-hosted
Linux runners. They use no live model credential. Live GLM authentication and
GitHub App publication still need an initial configured manual workflow run.
