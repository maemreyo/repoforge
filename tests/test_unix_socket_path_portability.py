from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from repoforge.adapters.runtime import UnixRuntimeControlClient, UnixRuntimeControlServer
from repoforge.domain.runtime import ControlCommand, ControlRequest, ControlResponse


def test_unix_control_fallback_stays_short_when_tempdir_is_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_tempdir = tmp_path / ("temp-" + "x" * 160)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(long_tempdir))
    logical = tmp_path / ("logical-" + "y" * 160) / "control.sock"
    server = UnixRuntimeControlServer(logical)

    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    try:
        response = UnixRuntimeControlClient(logical).request(
            ControlRequest(1, ControlCommand.PING, "long-tempdir")
        )
        assert response.ok
        assert len(os.fsencode(server.bound_path)) <= 100
    finally:
        bound = server.bound_path
        server.close()
    assert not bound.exists()
