"""Run a candidate runtime alongside production, fully isolated (#271).

Testing a branch must never disturb the production generation. A dev runtime is
isolated on three axes the exploration identified as load-bearing:

* ``state_root`` -- its own operation/worker-binding/approvals state, so a dev run
  can never pick up or reap production's background work (this is what keeps the
  #270 double-action hazard impossible by construction between prod and dev).
* config source path -- a distinct path re-namespaces the ``config-locks/<digest>``
  root, which is where the supervisor/mcp control sockets and generation state live,
  so the dev supervisor binds different sockets automatically.
* tunnel identity -- a distinct ``[tunnel].id`` + profile so the candidate never
  collides with production's tunnel registration.

`promote` intentionally reuses the #268 upgrade pipeline: a candidate only becomes
production ``current`` after the same clean-build -> smoke -> activate gates.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...domain.errors import ConfigError
from ...ports.activation import DevConfigProvisioner
from ...ports.runtime_control import RuntimeLauncher, RuntimeStore
from ..configuration.paths import resolve_repoforge_paths

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DEV_TUNNEL_PREFIX = "dev-"


@dataclass(frozen=True, slots=True)
class DevRuntimeLayout:
    """The isolated paths and identity of one named dev runtime."""

    name: str
    root: Path
    state_root: Path
    config_path: Path
    tunnel_id: str
    profile: str

    def env(self) -> dict[str, str]:
        return {
            "REPOFORGE_TUNNEL_ID": self.tunnel_id,
            "REPOFORGE_TUNNEL_PROFILE": self.profile,
        }


def derive_dev_runtime_layout(name: str, *, base_state_root: Path) -> DevRuntimeLayout:
    """Compute the isolated layout for a dev runtime called ``name``."""
    if not _NAME.fullmatch(name):
        raise ConfigError(
            "DEV_RUNTIME_NAME_INVALID: name must be a lowercase slug [a-z0-9-], 1-64 chars"
        )
    root = base_state_root.expanduser().resolve() / "dev-runtimes" / name
    return DevRuntimeLayout(
        name=name,
        root=root,
        state_root=root / "state",
        config_path=root / "config.toml",
        tunnel_id=f"{_DEV_TUNNEL_PREFIX}{name}",
        profile=f"{_DEV_TUNNEL_PREFIX}{name}",
    )


def dev_runtime_record_path(layout: DevRuntimeLayout) -> Path:
    """The supervisor runtime-record path for a dev runtime's namespaced root."""
    paths = resolve_repoforge_paths(layout.config_path, state_root=layout.state_root)
    return paths.generation_root.parent / "managed-runtime-v3.json"


class DevRuntimeService:
    """Start/stop/inspect a candidate runtime isolated from production."""

    def __init__(
        self,
        *,
        launcher: RuntimeLauncher,
        provisioner: DevConfigProvisioner,
        runtime_store_factory: Callable[[Path], RuntimeStore],
        base_config: Path,
        base_state_root: Path,
    ) -> None:
        self._launcher = launcher
        self._provisioner = provisioner
        self._runtime_store_factory = runtime_store_factory
        self._base_config = base_config
        self._base_state_root = base_state_root

    def _layout(self, name: str) -> DevRuntimeLayout:
        return derive_dev_runtime_layout(name, base_state_root=self._base_state_root)

    def start(self, name: str) -> dict[str, object]:
        layout = self._layout(name)
        layout.state_root.mkdir(parents=True, exist_ok=True)
        self._provisioner.provision(
            self._base_config,
            layout.config_path,
            state_root=layout.state_root,
            tunnel_id=layout.tunnel_id,
            profile=layout.profile,
        )
        pid = self._launcher.start(layout.config_path, foreground=False, extra_env=layout.env())
        return {
            "status": "starting",
            "name": name,
            "pid": pid,
            "config_path": str(layout.config_path),
            "state_root": str(layout.state_root),
            "tunnel_id": layout.tunnel_id,
            "isolated_from_production": True,
            "safe_next_action": f"Run `rf dev-runtime status {name}` to observe health.",
        }

    def stop(self, name: str) -> dict[str, object]:
        layout = self._layout(name)
        record = self._runtime_store_factory(dev_runtime_record_path(layout)).read()
        if record is None:
            return {"status": "not_running", "name": name}
        stopped = self._launcher.force_stop(record, grace_seconds=5.0)
        return {"status": "stopped" if stopped else "not_running", "name": name, "forced": stopped}

    def status(self, name: str) -> dict[str, object]:
        layout = self._layout(name)
        record = self._runtime_store_factory(dev_runtime_record_path(layout)).read()
        return {
            "name": name,
            "exists": layout.root.exists(),
            "config_path": str(layout.config_path),
            "state_root": str(layout.state_root),
            "tunnel_id": layout.tunnel_id,
            "phase": record.phase.value if record else "stopped",
            "pid": record.pid if record else None,
            "active_generation": record.active_generation if record else None,
        }

    def names(self) -> list[str]:
        """Every provisioned dev runtime name, whether running or not."""
        parent = self._base_state_root.expanduser().resolve() / "dev-runtimes"
        if not parent.is_dir():
            return []
        return sorted(entry.name for entry in parent.iterdir() if entry.is_dir())

    def list(self) -> dict[str, object]:
        """List dev runtimes WITH their live state.

        Names alone cannot answer the only question a listing is for -- "which of these is
        running, and which one do I switch to" -- and ``status()`` already computes exactly
        that, so the listing reports it per runtime instead of making the reader call
        ``status`` once per name to find out.
        """
        return {"dev_runtimes": [self.status(name) for name in self.names()]}
