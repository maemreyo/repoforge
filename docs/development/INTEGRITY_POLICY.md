# Source and release integrity policy

RepoForge does not use a manually maintained file-hash inventory. The authoritative
integrity command is `scripts/verify-production.sh`, executed on the exact reviewed tree.
The policy combines source control, a frozen dependency graph, executable tests, and a
clean installed-package smoke check rather than trusting a stale generated list.

## Ordered guarantees

1. **Tree identity and cleanliness.** The gate records `HEAD`, rejects an unexpected dirty
   tree unless `--allow-dirty` is explicitly used for development, and finishes with
   `git diff --check`. The commit gate separately binds its verification receipt to the
   exact workspace fingerprint.
2. **Frozen dependencies.** `uv sync --extra dev --frozen` requires the committed
   `uv.lock` to resolve without modification.
3. **Ticket and release contract validation.** The machine-readable ticket graph and the
   reviewed MCP/runtime release contract must agree with executable validators.
4. **Static and behavioral evidence.** Formatting, Ruff, strict Mypy, and all Pytest
   security/integration tests must pass. Test files run in deterministic size-balanced
   shards, then their branch-coverage data is combined before enforcing the 80% threshold.
5. **Distribution evidence.** RepoForge builds both the source distribution and wheel in
   a temporary generated artifact directory, installs the wheel into an isolated
   environment, and runs the packaged smoke checks.
6. **No residue.** Temporary caches, coverage data, build output, and installation state
   live below a disposable temporary root. A clean production run must not change the
   checkout.
7. **Truthful execution binding.** Verification evidence records requested policy separately
   from effective backend behavior and binds the environment identity plus requested/effective
   policy hashes. Commit re-inspects the current backend and rejects toolchain, adapter, policy,
   or configuration drift even when workspace bytes are unchanged. Native host execution is
   advisory for network/filesystem isolation and unsupported resource controls are explicit.

## Scope and exclusions

Git is the source inventory and preserves filenames, file modes, symlink entries, and
line ending bytes. Reviewed source, tests, documentation, configuration, scripts,
`pyproject.toml`, and `uv.lock` are covered through the ordered gate. Generated artifact
files under the temporary build root are not source inputs and are destroyed after the
run. Existing committed binary assets are reviewed through Git diffs; the gate does not
normalize their bytes.

RepoForge does not follow a symlink as an alternate source path during controlled file
operations. Line ending changes remain visible in Git and must also satisfy formatter,
parser, and test behavior. Build timestamps or archive metadata may differ between
independent builds, so integrity is asserted through source identity, frozen inputs,
successful clean builds, and installed-wheel behavior rather than an unmanaged archive
hash.

## Update and review workflow

When the release contract or integrity stages change, update the executable validator,
this policy, and regression tests in the same pull request. Dependency changes must
include the reviewed `pyproject.toml` and `uv.lock` diff. Generated distributions are not
committed as evidence; CI or the local gate recreates them from the reviewed tree.

A failure at any stage is authoritative. Do not regenerate expected data merely to make
the gate green. Generated contract files may be updated only after reviewing that tool count,
tool names, annotations, runtime protocol, and all input schemas remain stable and that output
changes are intentional, bounded, and closed. Diagnose whether the source, lockfile, contract, test, build, permission,
or clean-tree invariant changed, then make a reviewed correction and rerun the complete
gate on the new exact tree.
