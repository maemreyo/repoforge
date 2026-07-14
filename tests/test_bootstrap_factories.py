from __future__ import annotations

from pathlib import Path

from repoforge import bootstrap
from repoforge.adapters.configuration import ConfigGenerationStore
from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence import JsonOperationStore
from repoforge.adapters.repository import LocalRepositoryProbe
from repoforge.adapters.runtime import (
    InProcessOperationGate,
    JsonRuntimeStore,
    JsonTunnelProfileStore,
    SubprocessRuntimeLauncher,
    SystemProcessInspector,
    TunnelCliClient,
    UnixRuntimeControlClient,
    UnixRuntimeControlServer,
)


def test_bootstrap_factories_are_concrete_and_writable(tmp_path: Path) -> None:
    assert bootstrap.system_clock().now_iso()
    assert len(bootstrap.id_generator().new_hex(8)) == 8
    assert isinstance(bootstrap.build_lock_manager(tmp_path), FcntlLockManager)
    assert isinstance(
        bootstrap.build_configuration_store(tmp_path / "config.toml", state_root=tmp_path),
        ConfigGenerationStore,
    )
    assert isinstance(bootstrap.build_repository_probe(tmp_path), LocalRepositoryProbe)
    assert isinstance(bootstrap.build_operation_gate(), InProcessOperationGate)
    assert isinstance(bootstrap.build_operation_store(tmp_path), JsonOperationStore)
    assert isinstance(bootstrap.build_runtime_store(tmp_path / "runtime.json"), JsonRuntimeStore)
    assert isinstance(
        bootstrap.build_tunnel_profile_store(tmp_path / "profile.json"), JsonTunnelProfileStore
    )
    assert isinstance(
        bootstrap.build_runtime_control_client(tmp_path / "client.sock"), UnixRuntimeControlClient
    )
    assert isinstance(
        bootstrap.build_runtime_control_server(tmp_path / "server.sock"), UnixRuntimeControlServer
    )
    assert isinstance(bootstrap.build_runtime_launcher(), SubprocessRuntimeLauncher)
    assert isinstance(bootstrap.build_process_inspector(), SystemProcessInspector)
    assert isinstance(bootstrap.build_tunnel_client("tunnel-client"), TunnelCliClient)
    target = tmp_path / "private.bin"
    bootstrap.write_private_file(target, b"secret")
    assert target.read_bytes() == b"secret"
