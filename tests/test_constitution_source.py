"""The constitution is compiled from the accepted commit, never from the worktree.

Prerequisite slice for #206, and the guard #209 shares. A task proposing a change to
`.repoforge/rules/` must be judged by the accepted constitution rather than by its own
uncommitted edits -- otherwise it can widen the rule it is about to be checked against,
in the same call.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.application.rules.constitution import (
    CONSTITUTION_PREFIXES,
    compile_constitution,
)
from repoforge.domain.errors import ConfigError
from repoforge.ports.git import GitSnapshotBlob, ResolvedRepositoryRef

_ACCEPTED_RULE = """\
- id: demo.file-length
  enforcement: checked
  validator: file_length
  paths: ["**/*.py"]
  max_lines: 400
"""

_WIDENED_RULE = """\
- id: demo.file-length
  enforcement: checked
  validator: file_length
  paths: ["**/*.py"]
  max_lines: 99999
"""


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _commit(source: Path, message: str) -> None:
    git("add", "-A", cwd=source)
    git("commit", "-m", message, cwd=source)


def _compile(forge_env: ForgeEnvironment) -> object:
    ctx = forge_env.service.application.context
    repo = ctx.repo("demo")
    return compile_constitution(git=ctx.git, path=repo.path, repo=repo)


def _rule_params(compiled: object, rule_id: str) -> dict[str, object]:
    rules = compiled.rules  # type: ignore[attr-defined]
    return next(rule.params for rule in rules if rule.id == rule_id)


# --------------------------------------------------------------------- the guard


def test_the_constitution_comes_from_the_commit_not_the_worktree(
    forge_env: ForgeEnvironment,
) -> None:
    """The acceptance criterion #206 and #209 both state."""

    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a file-length rule")

    # A task now proposes widening it, in the worktree, uncommitted.
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _WIDENED_RULE)

    compiled = _compile(forge_env)

    assert _rule_params(compiled, "demo.file-length")["max_lines"] == 400, (
        "the worktree's own edit must not feed the constitution it will be judged by"
    )


def test_a_rule_committed_only_on_another_branch_is_not_the_constitution(
    forge_env: ForgeEnvironment,
) -> None:
    """Committing does not accept: only the default branch does."""

    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a file-length rule")

    git("checkout", "-b", "ai/widen-the-rule", cwd=forge_env.source)
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _WIDENED_RULE)
    _commit(forge_env.source, "widen the rule on a feature branch")

    compiled = _compile(forge_env)

    assert _rule_params(compiled, "demo.file-length")["max_lines"] == 400


# ------------------------------------------------------------- constitution_sha


def test_the_same_commit_compiles_to_the_same_constitution_sha(
    forge_env: ForgeEnvironment,
) -> None:
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a rule")

    first = _compile(forge_env)
    second = _compile(forge_env)

    assert first.source.constitution_sha == second.source.constitution_sha  # type: ignore[attr-defined]
    assert first.source.commit_sha == second.source.commit_sha  # type: ignore[attr-defined]


def test_accepting_a_changed_rule_changes_the_constitution_sha(
    forge_env: ForgeEnvironment,
) -> None:
    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a rule")
    before = _compile(forge_env).source.constitution_sha  # type: ignore[attr-defined]

    _write(forge_env.source, ".repoforge/rules/limits.yaml", _WIDENED_RULE)
    _commit(forge_env.source, "accept the widened rule on the default branch")
    after = _compile(forge_env).source.constitution_sha  # type: ignore[attr-defined]

    assert before != after
    # And the accepted change is now in force.
    assert _rule_params(_compile(forge_env), "demo.file-length")["max_lines"] == 99999


def test_an_unrelated_commit_does_not_change_the_constitution_sha(
    forge_env: ForgeEnvironment,
) -> None:
    """The hash tracks the constitution, not the repository."""

    _write(forge_env.source, ".repoforge/rules/limits.yaml", _ACCEPTED_RULE)
    _commit(forge_env.source, "accept a rule")
    before = _compile(forge_env)

    _write(forge_env.source, "src/unrelated.py", "x = 1\n")
    _commit(forge_env.source, "an ordinary source change")
    after = _compile(forge_env)

    assert after.source.commit_sha != before.source.commit_sha  # type: ignore[attr-defined]
    assert after.source.constitution_sha == before.source.constitution_sha  # type: ignore[attr-defined]


# ------------------------------------------------- what counts as the constitution


def test_skill_definitions_outside_repoforge_are_part_of_the_constitution(
    forge_env: ForgeEnvironment,
) -> None:
    """Skills live in `.claude/skills` and friends, not under `.repoforge/`.

    The prefixes are derived from #205's own root constant precisely so this cannot
    drift; this test is what would catch it if the derivation were replaced by a
    restated list.
    """

    assert ".repoforge/" in CONSTITUTION_PREFIXES
    assert ".claude/skills/" in CONSTITUTION_PREFIXES

    _write(
        forge_env.source,
        ".claude/skills/reviewing/SKILL.md",
        "---\nname: reviewing\ndescription: How this repo reviews changes.\n---\n\nBody.\n",
    )
    _commit(forge_env.source, "accept a skill")

    compiled = _compile(forge_env)
    skill = compiled.catalog.get("reviewing")  # type: ignore[attr-defined]

    assert skill is not None
    assert skill.description == "How this repo reviews changes."
    assert ".claude/skills/reviewing/SKILL.md" in compiled.source.paths  # type: ignore[attr-defined]


def test_a_repository_with_no_constitution_compiles_the_conservative_defaults(
    forge_env: ForgeEnvironment,
) -> None:
    """Zero-config: absent `.repoforge/` is not an error."""

    compiled = _compile(forge_env)

    assert compiled.source.paths == ()  # type: ignore[attr-defined]
    assert compiled.bindings == ()  # type: ignore[attr-defined]
    assert compiled.catalog.skills == ()  # type: ignore[attr-defined]
    # The #204 defaults still apply, so a repository is never ungoverned.
    assert any(rule.id == "default.file-length" for rule in compiled.rules)  # type: ignore[attr-defined]


def test_the_git_adapter_never_surfaces_a_symlinked_constitution_entry(
    forge_env: ForgeEnvironment,
) -> None:
    """The shipped adapter filters symlinks in `ls-tree`, so one never reaches the compiler.

    Asserted as the adapter's behaviour rather than the compiler's: a committed symlink is
    simply absent from the constitution, with no error to handle.
    """

    rules_dir = forge_env.source / ".repoforge" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    os.symlink("../../../etc/passwd", rules_dir / "escape.yaml")
    _commit(forge_env.source, "commit a symlinked rule file")

    compiled = _compile(forge_env)

    assert ".repoforge/rules/escape.yaml" not in compiled.source.paths  # type: ignore[attr-defined]


class _SymlinkServingGit:
    """A port implementation that does *not* filter symlinks, unlike the shipped adapter.

    `GitRepository` does not promise that filtering -- it is `GitCliRepository`'s own
    `ls-tree` behaviour -- so the compiler refuses symlinks itself. Testing that through
    real git is impossible, which is exactly why it is tested here instead of being left
    as an unexercised claim.
    """

    def resolve_snapshot_ref(self, path: Path, repo: object, ref: str | None) -> object:
        return ResolvedRepositoryRef(resolved_ref="refs/heads/main", commit_sha="a" * 40)

    def list_snapshot_files(
        self, path: Path, repo: object, commit_sha: str, max_entries: int
    ) -> tuple[list[str], bool]:
        return ([".repoforge/rules/escape.yaml"], False)

    def read_snapshot_blob(
        self, path: Path, repo: object, commit_sha: str, relative_path: str
    ) -> object:
        return GitSnapshotBlob(
            path=relative_path,
            object_sha="b" * 40,
            mode="120000",
            size_bytes=20,
            data=b"../../../etc/passwd",
        )


def test_a_symlink_from_a_provider_that_does_not_filter_is_refused(
    forge_env: ForgeEnvironment,
) -> None:
    ctx = forge_env.service.application.context
    repo = ctx.repo("demo")

    with pytest.raises(ConfigError, match="symlink"):
        compile_constitution(
            git=_SymlinkServingGit(),  # type: ignore[arg-type]
            path=repo.path,
            repo=repo,
        )
