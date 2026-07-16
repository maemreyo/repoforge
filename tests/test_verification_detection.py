from pathlib import Path

from repoforge.application.profile_drift import ProfileDriftAssessor
from repoforge.application.verification_detection import VerificationProfileDetector
from repoforge.config import load_config


def test_detects_uv_python_profiles_with_provenance_and_bounds(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    candidates = VerificationProfileDetector().detect(tmp_path)

    assert [(item.profile_id, item.argv, item.provenance) for item in candidates] == [
        ("python-setup", ("uv", "sync", "--extra", "dev"), ("pyproject.toml", "uv.lock")),
        (
            "python-test",
            ("uv", "run", "--extra", "dev", "pytest", "-q"),
            ("pyproject.toml", "uv.lock"),
        ),
    ]
    assert candidates[0].requires_network_confirmation is True
    assert candidates[1].network_policy == "local_only"
    assert all(item.timeout_seconds > 0 for item in candidates)


def test_detects_node_go_rust_and_make_profiles_without_running_commands(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "test:\n\tpytest\ncheck:\n\truff check .\n", encoding="utf-8"
    )

    candidates = VerificationProfileDetector().detect(tmp_path)

    assert [(item.profile_id, item.argv) for item in candidates] == [
        ("node-setup", ("pnpm", "install", "--frozen-lockfile")),
        ("node-test", ("pnpm", "run", "test")),
        ("go-build", ("go", "build", "./...")),
        ("go-test", ("go", "test", "./...")),
        ("cargo-build", ("cargo", "build")),
        ("cargo-test", ("cargo", "test")),
        ("make-check", ("make", "check")),
        ("make-test", ("make", "test")),
    ]


def test_proposed_profiles_exclude_networked_setup_until_explicitly_allowed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    detector = VerificationProfileDetector()

    assert [
        item.name for item in detector.proposed_profiles(tmp_path, include_dependency_setup=False)
    ] == ["python-test"]
    assert [
        item.name for item in detector.proposed_profiles(tmp_path, include_dependency_setup=True)
    ] == [
        "python-setup",
        "python-test",
    ]


def test_markerless_repository_has_no_candidates(tmp_path: Path) -> None:
    assert VerificationProfileDetector().detect(tmp_path) == ()


def _repository_with_profile(tmp_path: Path, *, command: str) -> object:
    (tmp_path / ".git").mkdir(exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[repositories.demo]
path = "{tmp_path}"

[repositories.demo.profiles.active]
verification = true
commands = [[{command}]]
""",
        encoding="utf-8",
    )
    return load_config(config).repositories["demo"]


def test_profile_drift_deduplicates_semantically_equivalent_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    repo = _repository_with_profile(
        tmp_path,
        command='"uv", "run", "--extra", "dev", "pytest", "-q"',
    )

    assessment = ProfileDriftAssessor().assess(
        repo,
        head_sha="a" * 40,
        config_identity="b" * 64,
        policy_hash="c" * 64,
        source_dirty=False,
    )

    assert [item.profile_id for item in assessment.detected_unenrolled_profiles] == ["python-setup"]
    candidate = assessment.detected_unenrolled_profiles[0]
    assert candidate.capability_delta == "expansion"
    assert candidate.requires_operator_confirmation is True
    assert candidate.proposal_ready is True
    assert candidate.repo_policy_apply == {
        "repo_id": "demo",
        "set_profiles": [
            {
                "name": "python-setup",
                "description": "Detected from pyproject.toml, uv.lock",
                "commands": [["uv", "sync", "--extra", "dev"]],
                "verification": False,
                "timeout_seconds": 1800,
            }
        ],
        "dry_run": True,
    }


def test_profile_drift_is_not_proposal_ready_on_dirty_source_snapshot(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    repo = _repository_with_profile(tmp_path, command='"echo", "ok"')

    assessment = ProfileDriftAssessor().assess(
        repo,
        head_sha="a" * 40,
        config_identity="b" * 64,
        policy_hash="c" * 64,
        source_dirty=True,
    )

    assert assessment.stale is True
    assert assessment.detected_unenrolled_profiles
    assert all(not item.proposal_ready for item in assessment.detected_unenrolled_profiles)
    assert all(item.repo_policy_apply is None for item in assessment.detected_unenrolled_profiles)
