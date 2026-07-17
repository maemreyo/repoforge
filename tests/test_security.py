from pathlib import Path

import pytest

from repoforge.config import RepositoryConfig
from repoforge.domain.egress import (
    EgressContentClass,
    EgressDecision,
    EgressDestination,
    EgressPolicy,
    EgressRequest,
    evaluate_egress,
    sanitize_egress_data,
)
from repoforge.domain.errors import SecurityError
from repoforge.domain.policy import (
    assert_path_allowed,
    extract_patch_paths,
    resolve_workspace_path,
    slugify,
    validate_patch,
)


def repo_config(tmp_path: Path) -> RepositoryConfig:
    return RepositoryConfig(
        repo_id="demo",
        path=tmp_path,
        denied_paths=(".env", ".github/workflows/**", "**/*.pem"),
    )


def test_slugify() -> None:
    assert slugify("Implement TODO Item #5") == "implement-todo-item-5"


def test_denied_paths(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    assert assert_path_allowed("src/main.py", repo) == "src/main.py"
    with pytest.raises(SecurityError):
        assert_path_allowed(".env", repo)
    with pytest.raises(SecurityError):
        assert_path_allowed(".github/workflows/ci.yml", repo)


def test_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    repo = repo_config(tmp_path)
    with pytest.raises(SecurityError):
        resolve_workspace_path(root, "../outside", repo)


def test_patch_paths() -> None:
    patch = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
"""
    assert extract_patch_paths(patch) == ("src/a.py",)


def test_denied_generated_change_is_rejected(tmp_path: Path) -> None:
    # Policy checks apply even to files created outside the MCP write tools.
    repo = repo_config(tmp_path)
    with pytest.raises(SecurityError):
        assert_path_allowed("private.pem", repo)


def test_symlink_patch_is_rejected(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    patch = r"""diff --git a/link b/link
new file mode 120000
index 0000000..1de5659
--- /dev/null
+++ b/link
@@ -0,0 +1 @@
+../outside
\ No newline at end of file
"""
    with pytest.raises(SecurityError, match="symlinks/submodules"):
        validate_patch(patch, repo, max_chars=10_000)


def test_egress_redacts_provider_assignments_urls_and_explicit_secrets_without_leaking() -> None:
    provider = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    explicit = "company-private-value"
    request = EgressRequest(
        content=(
            f"safe prefix api_key={provider} url=https://user:password@example.test/path "
            f"note={explicit} safe suffix"
        ),
        content_class=EgressContentClass.DIAGNOSTIC,
        destination=EgressDestination.MODEL,
        explicit_secrets=(explicit,),
    )

    result = evaluate_egress(request)

    assert result.decision is EgressDecision.REDACT_RANGES
    assert result.content is not None
    assert "safe prefix" in result.content and "safe suffix" in result.content
    assert provider not in result.content
    assert explicit not in result.content
    assert "password" not in result.content
    assert {item.category for item in result.findings} >= {
        "provider_token",
        "credential_url",
        "explicit_secret",
    }
    serialized_findings = repr(result.findings)
    assert provider not in serialized_findings
    assert explicit not in serialized_findings
    assert result.redaction_count == len(result.redaction_ranges)
    assert len(result.source_digest) == 64


def test_egress_withholds_private_keys_and_denied_sources_and_rejects_binary() -> None:
    private_key = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"
    private_result = evaluate_egress(
        EgressRequest(
            content=f"prefix\n{private_key}\nsuffix",
            content_class=EgressContentClass.SOURCE_SNIPPET,
            destination=EgressDestination.MODEL,
        )
    )
    assert private_result.decision is EgressDecision.WITHHOLD_SNIPPET
    assert private_result.content is None
    assert private_key not in repr(private_result)
    assert {item.category for item in private_result.findings} == {"private_key"}

    denied_result = evaluate_egress(
        EgressRequest(
            content="ordinary text",
            content_class=EgressContentClass.SOURCE_SNIPPET,
            destination=EgressDestination.MODEL,
            source_path=".env",
            source_denied=True,
        )
    )
    assert denied_result.decision is EgressDecision.WITHHOLD_SNIPPET
    assert denied_result.content is None
    assert denied_result.findings[0].category == "denied_source"

    binary_result = evaluate_egress(
        EgressRequest(
            content=b"valid-prefix\xff\x00secret",
            content_class=EgressContentClass.TRACE,
            destination=EgressDestination.MODEL,
        )
    )
    assert binary_result.decision is EgressDecision.REJECT_RESULT
    assert binary_result.content is None
    assert binary_result.findings[0].category == "binary_data"


def test_egress_allows_hashes_uuids_lock_integrity_and_explicit_safe_fixtures() -> None:
    sha256 = "a" * 64
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    integrity = "sha256-4M7sYV2XxwFe3Jk8zQ0pL6nT1uB5cD9eF2gH7iK3lN8="
    fixture = "sk-test-public-fixture-value-1234567890"
    value = f"digest={sha256} uuid={uuid} integrity={integrity} fixture={fixture}"

    result = evaluate_egress(
        EgressRequest(
            content=value,
            content_class=EgressContentClass.STRUCTURED_FIELD,
            destination=EgressDestination.MODEL,
            allow_values=(fixture,),
        )
    )

    assert result.decision is EgressDecision.ALLOW
    assert result.content == value
    assert result.findings == ()


def test_egress_merges_overlapping_ranges_bounds_output_and_preserves_unicode_context() -> None:
    secret = "top-secret-value-1234567890"
    value = f"αβ prefix password={secret} token={secret} suffix Ω"
    result = evaluate_egress(
        EgressRequest(
            content=value,
            content_class=EgressContentClass.LOG,
            destination=EgressDestination.OPERATOR_UI,
            explicit_secrets=(secret,),
            policy=EgressPolicy(max_output_chars=80, max_output_lines=2),
        )
    )

    assert result.decision is EgressDecision.REDACT_RANGES
    assert result.content is not None
    assert "αβ prefix" in result.content
    assert "suffix Ω" in result.content
    assert secret not in result.content
    assert all(
        first.end <= second.start
        for first, second in zip(result.redaction_ranges, result.redaction_ranges[1:], strict=False)
    )
    assert len(result.content) <= 80


def test_egress_rejects_oversized_input_and_recursively_sanitizes_payloads() -> None:
    oversized = evaluate_egress(
        EgressRequest(
            content="x" * 101,
            content_class=EgressContentClass.RECORDING,
            destination=EgressDestination.MODEL,
            policy=EgressPolicy(max_input_bytes=100),
        )
    )
    assert oversized.decision is EgressDecision.REJECT_RESULT
    assert oversized.findings[0].category == "size_limit"

    secret = "ghp_Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
    payload = {
        "workspace_fingerprint": "b" * 64,
        "selector": "check-run:12345",
        "nested": {
            "authorization": f"Bearer {secret}",
            "message": f"failed with token={secret}",
        },
        "items": ["ordinary", f"password={secret}"],
    }

    sanitized = sanitize_egress_data(payload, destination=EgressDestination.MODEL)

    assert isinstance(sanitized, dict)
    assert sanitized["workspace_fingerprint"] == "b" * 64
    assert sanitized["selector"] == "check-run:12345"
    rendered = repr(sanitized)
    assert secret not in rendered
    assert "ordinary" in rendered
