from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.runtime import unix_control
from repoforge.adapters.runtime.tunnel_cli import TunnelCliClient
from repoforge.domain.errors import ConfigError
from repoforge.interfaces.mcp.server import create_server


class _MissingRepositoryService:
    def repo_status(self, repo_id: str) -> dict[str, Any]:
        raise ConfigError(f"Unknown repository id: {repo_id}")


@pytest.mark.anyio
async def test_mcp_structured_failure_preserves_protocol_error_semantics() -> None:
    server = create_server(service=_MissingRepositoryService())  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_status", {"repo_id": "missing"})

    assert result.isError is True
    rendered = "\n".join(
        item.text for item in result.content if getattr(item, "type", None) == "text"
    )
    assert '"status": "failed"' in rendered
    assert '"error_code": "NOT_FOUND"' in rendered
    assert "Unknown repository id" in rendered


def test_peer_uid_falls_through_when_so_peercred_is_not_usable() -> None:
    class FakeConnection:
        def getsockopt(self, *_args: object) -> bytes:
            raise OSError("SO_PEERCRED is not supported by this socket ABI")

        def getpeereid(self) -> tuple[int, int]:
            return os.getuid(), os.getgid()

    assert unix_control._peer_uid(FakeConnection()) == os.getuid()  # type: ignore[arg-type]


def test_peer_uid_uses_native_getpeereid_when_python_socket_has_no_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def getsockopt(self, *_args: object) -> bytes:
            raise OSError("SO_PEERCRED is not supported by this socket ABI")

        def fileno(self) -> int:
            return 42

    monkeypatch.setattr(
        unix_control,
        "_native_getpeereid_uid",
        lambda descriptor: os.getuid() if descriptor == 42 else None,
        raising=False,
    )
    assert unix_control._peer_uid(FakeConnection()) == os.getuid()  # type: ignore[arg-type]


def test_child_finalization_waits_until_log_pump_has_drained(tmp_path: Path) -> None:
    client = TunnelCliClient("unused")
    release = threading.Event()

    def blocked_pump() -> None:
        release.wait(10)

    pump = threading.Thread(target=blocked_pump)
    pump.start()
    client._log_threads[123] = pump
    client._children[123] = object()  # type: ignore[assignment]

    finalized = threading.Thread(target=client._finalize_child, args=(123,))
    finalized.start()
    time.sleep(2.2)
    try:
        assert finalized.is_alive(), "finalization reported completion before log drain finished"
        assert 123 in client._log_threads
        assert 123 in client._children
    finally:
        release.set()
        finalized.join(3)
        pump.join(3)

    assert not finalized.is_alive()
    assert 123 not in client._log_threads
    assert 123 not in client._children
