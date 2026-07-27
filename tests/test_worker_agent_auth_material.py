"""Round-7 findings 1 and 2: the worker is the trust boundary for the durable credential.

The supervisor shim no longer sources ``runtime/agent.env`` -- it passes the PATH. So the
worker must open it, re-prove every security invariant on that same descriptor, and parse it
as data. These are the tests for that boundary; nothing here starts a real runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.bootstrap import _agent_secret_from_file
from repoforge.domain.activation import AGENT_SECRET_FILE_ENV_VAR, AGENT_SECRET_KEY
from repoforge.domain.errors import ConfigError


def _secret_file(tmp_path: Path, content: str, *, mode: int = 0o600) -> Path:
    path = tmp_path / "agent.env"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def test_no_secret_file_configured_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rf start` from a shell has the credential in the environment and no file path."""
    monkeypatch.delenv(AGENT_SECRET_FILE_ENV_VAR, raising=False)

    assert _agent_secret_from_file() == {}


def test_a_valid_credential_file_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _secret_file(tmp_path, f"{AGENT_SECRET_KEY}=s3cret\n")
    monkeypatch.setenv(AGENT_SECRET_FILE_ENV_VAR, str(path))

    assert _agent_secret_from_file() == {AGENT_SECRET_KEY: "s3cret"}


def test_a_world_readable_credential_file_is_refused_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant is checked HERE, at boot, not only when install-agent ran."""
    path = _secret_file(tmp_path, f"{AGENT_SECRET_KEY}=s3cret\n", mode=0o644)
    monkeypatch.setenv(AGENT_SECRET_FILE_ENV_VAR, str(path))

    with pytest.raises(ConfigError, match="0600"):
        _agent_secret_from_file()


def test_a_symlinked_credential_file_is_refused_without_being_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _secret_file(tmp_path, f"{AGENT_SECRET_KEY}=stolen\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    monkeypatch.setenv(AGENT_SECRET_FILE_ENV_VAR, str(link))

    with pytest.raises(ConfigError, match="symlink"):
        _agent_secret_from_file()


def test_shell_shaped_content_is_refused_and_never_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "marker"
    path = _secret_file(tmp_path, f"{AGENT_SECRET_KEY}=''; touch '{marker}'\n")
    monkeypatch.setenv(AGENT_SECRET_FILE_ENV_VAR, str(path))

    with pytest.raises(ConfigError, match="AGENT_SECRET_UNUSABLE"):
        _agent_secret_from_file()
    assert not marker.exists()


def test_a_missing_credential_file_is_a_refusal_not_a_silent_empty_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silently absent credential would surface as an opaque control-plane auth failure."""
    monkeypatch.setenv(AGENT_SECRET_FILE_ENV_VAR, str(tmp_path / "absent.env"))

    with pytest.raises(ConfigError, match="AGENT_SECRET_UNUSABLE"):
        _agent_secret_from_file()
