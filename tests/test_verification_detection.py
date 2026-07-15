from pathlib import Path

from repoforge.application.verification_detection import VerificationProfileDetector


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
