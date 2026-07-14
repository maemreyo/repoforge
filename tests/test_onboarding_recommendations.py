from repoforge.application.onboarding.recommendations import (
    DecisionRecommendation,
    recommend_safe_decisions,
)


def test_safe_defaults_do_not_guess_ambiguous_repository_choices() -> None:
    required = (
        (
            "dependency_install",
            "network",
            ("include_non_verification", "exclude", "block"),
        ),
        ("package_manager", "ambiguous", ("npm", "pnpm")),
        ("risky_commands", "danger", ("exclude", "block")),
        ("publish_remote", "no remote", ("read_only",)),
        ("working_directory_override", "path", ("required",)),
    )

    assert recommend_safe_decisions(required) == (
        DecisionRecommendation("dependency_install", "exclude", "avoid networked dependency setup"),
        DecisionRecommendation(
            "risky_commands",
            "exclude",
            "keep deploy, release, and destructive commands unavailable",
        ),
        DecisionRecommendation("publish_remote", "read_only", "only available bounded option"),
    )
