"""Durable, secret-free tunnel profile metadata persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.runtime import TunnelProfile
from ...ports.filesystem import FileSystem


class JsonTunnelProfileStore:
    """Store only the reviewed profile fingerprint and executable metadata.

    The tunnel id itself and credentials are deliberately excluded. The adapter relies on the
    injected filesystem port for atomic file and parent-directory durability.
    """

    def __init__(self, path: Path, filesystem: FileSystem) -> None:
        self.path = path
        self._filesystem = filesystem

    def fingerprint(self) -> str | None:
        if not self._filesystem.is_file(self.path):
            return None
        try:
            raw: Any = json.loads(self._filesystem.read_text(self.path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid tunnel profile metadata {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Tunnel profile metadata {self.path} must be an object")
        value = raw.get("fingerprint")
        if value is None:
            return None
        if not isinstance(value, str) or len(value) != 64:
            raise ConfigError(f"Tunnel profile fingerprint is invalid: {self.path}")
        return value

    def commit(self, profile: TunnelProfile) -> None:
        payload = {
            "format_version": 1,
            "fingerprint": profile.fingerprint,
            "profile": profile.profile,
            "executable": profile.executable,
            "executable_version": profile.executable_version,
            "mcp_argv_sha256": __import__("hashlib")
            .sha256("\0".join(profile.mcp_argv).encode("utf-8"))
            .hexdigest(),
        }
        self._filesystem.write_bytes_atomic(
            self.path,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            preserve_mode=False,
        )
