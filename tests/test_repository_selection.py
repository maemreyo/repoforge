"""Pure unit coverage for the deterministic repository-selection policy (#150)."""

from __future__ import annotations

from pathlib import Path

from repoforge.config import RepositoryConfig
from repoforge.domain.repository_selection import (
    RepositorySelectionOutcome,
    select_repository,
)


def _repo(repo_id: str, *, display_name: str = "", remote: str = "origin") -> RepositoryConfig:
    return RepositoryConfig(
        repo_id=repo_id,
        path=Path(f"/repos/{repo_id}"),
        display_name=display_name,
        remote=remote,
    )


def test_zero_enrolled_returns_no_match_without_inventing_a_repo_id() -> None:
    selection = select_repository(())

    assert selection.outcome is RepositorySelectionOutcome.NO_MATCH
    assert selection.repo_id is None
    assert selection.candidates == ()


def test_single_enrolled_returns_exact_repo_id_unconditionally() -> None:
    selection = select_repository((_repo("demo", display_name="Demo Repository"),))

    assert selection.outcome is RepositorySelectionOutcome.SINGLE_ENROLLED
    assert selection.repo_id == "demo"
    assert [c.repo_id for c in selection.candidates] == ["demo"]


def test_single_enrolled_wins_even_when_the_hint_matches_nothing() -> None:
    # A single enrolled repository is never ambiguous, so a stray/incorrect hint must not
    # block progress: it falls through to the only candidate rather than NO_MATCH.
    selection = select_repository(
        (_repo("demo", display_name="Demo Repository"),),
        requested_repo="totally-unrelated",
    )

    assert selection.outcome is RepositorySelectionOutcome.SINGLE_ENROLLED
    assert selection.repo_id == "demo"


def test_exact_repo_id_match_wins_among_multiple_enrolled() -> None:
    repos = (
        _repo("demo", display_name="Demo Repository"),
        _repo("widgets", display_name="Widgets Service"),
    )

    selection = select_repository(repos, requested_repo="widgets")

    assert selection.outcome is RepositorySelectionOutcome.EXACT_MATCH
    assert selection.repo_id == "widgets"


def test_unique_alias_match_by_display_name_or_remote() -> None:
    repos = (
        _repo("demo", display_name="Demo Repository", remote="origin"),
        _repo("widgets", display_name="Widgets Service", remote="upstream"),
    )

    by_display_name = select_repository(repos, requested_repo="Widgets Service")
    assert by_display_name.outcome is RepositorySelectionOutcome.EXACT_MATCH
    assert by_display_name.repo_id == "widgets"

    by_remote = select_repository(repos, requested_repo="upstream")
    assert by_remote.outcome is RepositorySelectionOutcome.EXACT_MATCH
    assert by_remote.repo_id == "widgets"

    # Case-insensitive on the alias fields only.
    by_lowercase = select_repository(repos, requested_repo="widgets service")
    assert by_lowercase.outcome is RepositorySelectionOutcome.EXACT_MATCH
    assert by_lowercase.repo_id == "widgets"


def test_exact_repo_id_match_wins_over_a_colliding_display_name_on_another_repo() -> None:
    repos = (
        _repo("widgets", display_name="widgets"),
        _repo("demo", display_name="Demo Repository"),
    )

    # requested_repo == "widgets" matches repo_id of the first repo exactly; it must win even
    # though nothing else aliases to it.
    selection = select_repository(repos, requested_repo="widgets")

    assert selection.outcome is RepositorySelectionOutcome.EXACT_MATCH
    assert selection.repo_id == "widgets"


def test_ambiguous_alias_match_reports_only_the_matching_candidates() -> None:
    repos = (
        _repo("api-a", display_name="API", remote="origin"),
        _repo("api-b", display_name="API", remote="origin"),
        _repo("unrelated", display_name="Unrelated Service", remote="origin"),
    )

    selection = select_repository(repos, requested_repo="API")

    assert selection.outcome is RepositorySelectionOutcome.INPUT_REQUIRED
    assert selection.repo_id is None
    assert [c.repo_id for c in selection.candidates] == ["api-a", "api-b"]


def test_no_unique_match_with_multiple_enrolled_returns_full_bounded_candidates() -> None:
    repos = (
        _repo("demo", display_name="Demo Repository"),
        _repo("widgets", display_name="Widgets Service"),
    )

    no_hint = select_repository(repos)
    assert no_hint.outcome is RepositorySelectionOutcome.INPUT_REQUIRED
    assert [c.repo_id for c in no_hint.candidates] == ["demo", "widgets"]

    misspelled_hint = select_repository(repos, requested_repo="wigdets")
    assert misspelled_hint.outcome is RepositorySelectionOutcome.INPUT_REQUIRED
    assert [c.repo_id for c in misspelled_hint.candidates] == ["demo", "widgets"]


def test_candidate_ordering_is_deterministic_regardless_of_input_order() -> None:
    forward = select_repository((_repo("alpha"), _repo("beta"), _repo("gamma")))
    reversed_input = select_repository((_repo("gamma"), _repo("beta"), _repo("alpha")))

    assert [c.repo_id for c in forward.candidates] == ["alpha", "beta", "gamma"]
    assert [c.repo_id for c in reversed_input.candidates] == ["alpha", "beta", "gamma"]


def test_selection_never_matches_a_filesystem_path() -> None:
    repo = _repo("demo", display_name="Demo Repository")

    selection = select_repository((repo,), requested_repo=str(repo.path))

    # A path is not repo_id, display_name, or remote -- the single-enrolled fallback still
    # applies, but not because the path was treated as an identity match.
    assert selection.outcome is RepositorySelectionOutcome.SINGLE_ENROLLED


def test_selection_is_pure_and_never_remembers_prior_calls() -> None:
    repos = (_repo("demo"), _repo("widgets"))

    first = select_repository(repos, requested_repo="widgets")
    second = select_repository(repos)  # no hint this time

    assert first.outcome is RepositorySelectionOutcome.EXACT_MATCH
    # The previous call's resolved repo_id must not leak as hidden "last used" authority.
    assert second.outcome is RepositorySelectionOutcome.INPUT_REQUIRED
    assert second.repo_id is None


def test_as_dict_is_json_stable_and_bounded() -> None:
    selection = select_repository((_repo("demo", display_name="Demo Repository"),))

    rendered = selection.as_dict()
    assert rendered == {
        "outcome": "single_enrolled",
        "repo_id": "demo",
        "candidates": [{"repo_id": "demo", "display_name": "Demo Repository"}],
        "guidance": rendered["guidance"],
    }
    assert isinstance(rendered["guidance"], str) and rendered["guidance"]
