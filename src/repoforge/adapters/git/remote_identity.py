"""Constrained, non-executing Git remote and OpenSSH alias parsing."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urlsplit

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.git_remote_identity import (
    GitRemoteKind,
    ParsedGitRemote,
    SshAliasDefinition,
)
from ...ports.git_remote_identity import EffectiveUserPaths

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCP_REMOTE = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._-]+)@)?"
    r"(?P<host>[A-Za-z0-9.-]+):"
    r"(?P<path>[^\x00]+)$"
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_MAX_CONFIG_BYTES = 200_000
_MAX_CONFIG_LINES = 2_000
_ALLOWED_ALIAS_DIRECTIVES = frozenset(
    {"hostname", "user", "port", "identityfile", "identitiesonly"}
)


def _rejected(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
        retryable=False,
        unchanged_state=(
            "No SSH command or shell command was executed.",
            "No SSH or RepoForge configuration was modified.",
        ),
    )


@dataclass(frozen=True, slots=True)
class _HostBlock:
    patterns: tuple[str, ...]
    lines: tuple[str, ...]


class ConstrainedGitRemoteParser:
    """Parse safe repository remotes without resolving transport aliases."""

    def parse(self, remote_url: str) -> ParsedGitRemote:
        if (
            not isinstance(remote_url, str)
            or not remote_url
            or len(remote_url) > 4096
            or "\x00" in remote_url
        ):
            raise _rejected("The Git remote URL is not bounded safe text.")
        if "://" in remote_url:
            return self._parse_url(remote_url)
        return self._parse_scp(remote_url)

    def _parse_scp(self, remote_url: str) -> ParsedGitRemote:
        match = _SCP_REMOTE.fullmatch(remote_url)
        if match is None:
            raise _rejected("The Git remote is not a supported scp-style SSH URL.")
        raw_host = match.group("host")
        if raw_host != raw_host.lower():
            raise _rejected("The Git remote host must use canonical lowercase spelling.")
        host = raw_host
        self._require_remote_host(host)
        owner, repository = self._repository_parts(match.group("path"))
        try:
            return ParsedGitRemote(
                kind=GitRemoteKind.SSH,
                raw_host=host,
                owner=owner,
                repository=repository,
                user=match.group("user"),
                port=None,
                raw_url_digest=hashlib.sha256(remote_url.encode("utf-8")).hexdigest(),
            )
        except ValueError as exc:
            raise _rejected(f"The Git remote is not a safe repository endpoint: {exc}") from exc

    def _parse_url(self, remote_url: str) -> ParsedGitRemote:
        parsed = urlsplit(remote_url)
        scheme = parsed.scheme.lower()
        if scheme not in {GitRemoteKind.SSH.value, GitRemoteKind.HTTPS.value}:
            raise _rejected("The Git remote scheme is not supported.")
        if parsed.hostname is None or parsed.query or parsed.fragment:
            raise _rejected("The Git remote URL has an incomplete or mutable target.")
        if parsed.password is not None:
            raise _rejected("Credentials are not allowed in Git remote URLs.")
        if scheme == GitRemoteKind.HTTPS.value and parsed.username is not None:
            raise _rejected("HTTPS Git remote URLs cannot contain user information.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise _rejected("The Git remote URL contains an invalid port.") from exc
        authority = parsed.netloc.rsplit("@", 1)[-1]
        raw_host = authority.rsplit(":", 1)[0] if ":" in authority else authority
        if raw_host != raw_host.lower():
            raise _rejected("The Git remote host must use canonical lowercase spelling.")
        host = parsed.hostname
        self._require_remote_host(host)
        owner, repository = self._repository_parts(parsed.path)
        try:
            return ParsedGitRemote(
                kind=GitRemoteKind(scheme),
                raw_host=host,
                owner=owner,
                repository=repository,
                user=parsed.username,
                port=port,
                raw_url_digest=hashlib.sha256(remote_url.encode("utf-8")).hexdigest(),
            )
        except ValueError as exc:
            raise _rejected(f"The Git remote is not a safe repository endpoint: {exc}") from exc

    @staticmethod
    def _require_remote_host(host: str) -> None:
        if host in _LOCAL_HOSTS:
            raise _rejected("Local Git remote hosts are not repository identity targets.")

    @staticmethod
    def _repository_parts(path: str) -> tuple[str, str]:
        rendered = path[1:] if path.startswith("/") else path
        parts = rendered.split("/")
        if len(parts) != 2 or any(not part for part in parts):
            raise _rejected("The Git remote path must be exactly owner/repository.")
        owner, repository = parts
        if repository.endswith(".git"):
            repository = repository[:-4]
        if owner in {".", ".."} or repository in {"", ".", ".."}:
            raise _rejected("The Git remote path contains an unsafe repository segment.")
        return owner, repository


class ConstrainedSshConfigAliasResolver:
    """Resolve one exact alias from a deliberately small OpenSSH config subset."""

    def __init__(self, *, paths: EffectiveUserPaths) -> None:
        self._paths = paths

    def resolve(self, alias: str) -> SshAliasDefinition:
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise _rejected("The requested SSH alias is not a safe exact alias name.")
        raw = self._read_config()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _rejected("The SSH user configuration is not valid UTF-8.") from exc
        blocks = self._blocks(text)
        if any(
            block.patterns != (alias,) and self._patterns_may_match(alias, block.patterns)
            for block in blocks
        ):
            raise _rejected(
                "The SSH alias is affected by a wildcard, negated, or multi-pattern Host block."
            )
        matches = tuple(block for block in blocks if block.patterns == (alias,))
        if len(matches) != 1:
            raise _rejected("The SSH alias must have exactly one exact Host block.")
        block = matches[0]
        values: dict[str, list[str]] = {}
        for line in block.lines[1:]:
            rendered = line.split("#", 1)[0].strip()
            if not rendered:
                continue
            key, value = self._directive(rendered)
            normalized_key = key.lower()
            if normalized_key not in _ALLOWED_ALIAS_DIRECTIVES:
                raise _rejected(f"The SSH alias block contains unsupported directive {key}.")
            values.setdefault(normalized_key, []).append(value.strip())

        hostname = self._one(values, "hostname")
        user = self._one(values, "user")
        identity_file = self._expand_identity_file(self._one(values, "identityfile"))
        identities_only = self._one(values, "identitiesonly")
        if identities_only.lower() != "yes":
            raise _rejected("The SSH alias must declare IdentitiesOnly yes.")
        port_raw = values.get("port", ["22"])
        if len(port_raw) != 1:
            raise _rejected("The SSH alias must declare at most one Port.")
        try:
            port = int(port_raw[0])
        except ValueError as exc:
            raise _rejected("The SSH alias Port must be an integer.") from exc

        try:
            return SshAliasDefinition(
                alias=alias,
                canonical_host=hostname,
                user=user,
                port=port,
                identity_file=identity_file,
                source_config_digest=hashlib.sha256(raw).hexdigest(),
                selected_block_digest=hashlib.sha256(
                    "\n".join(block.lines).encode("utf-8")
                ).hexdigest(),
            )
        except ValueError as exc:
            raise _rejected(f"The SSH alias block is not a safe pinned endpoint: {exc}") from exc

    def _read_config(self) -> bytes:
        path = self._paths.ssh_config
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise _rejected("The SSH user configuration is unavailable or is a symlink.") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise _rejected(
                    "The SSH user configuration must be a regular file owned by the effective user."
                )
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise _rejected("The SSH user configuration must not be group or world writable.")
            raw = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
            if len(raw) > _MAX_CONFIG_BYTES:
                raise _rejected("The SSH user configuration exceeds the reviewed size bound.")
            return raw
        finally:
            os.close(descriptor)

    def _blocks(self, text: str) -> tuple[_HostBlock, ...]:
        lines = text.splitlines()
        if len(lines) > _MAX_CONFIG_LINES:
            raise _rejected("The SSH user configuration exceeds the reviewed line bound.")
        blocks: list[_HostBlock] = []
        current: list[str] | None = None
        patterns: tuple[str, ...] = ()
        for raw_line in lines:
            rendered = raw_line.split("#", 1)[0].strip()
            if not rendered:
                if current is not None:
                    current.append(raw_line)
                continue
            key, value = self._directive(rendered)
            if key.lower() == "host":
                if current is not None:
                    blocks.append(_HostBlock(patterns=patterns, lines=tuple(current)))
                patterns = tuple(value.split())
                current = [raw_line]
            elif current is not None:
                current.append(raw_line)
            else:
                raise _rejected(
                    "Global SSH directives make the selected alias configuration undecidable."
                )
        if current is not None:
            blocks.append(_HostBlock(patterns=patterns, lines=tuple(current)))
        return tuple(blocks)

    @staticmethod
    def _patterns_may_match(alias: str, patterns: tuple[str, ...]) -> bool:
        for pattern in patterns:
            candidate = pattern[1:] if pattern.startswith("!") else pattern
            if candidate and fnmatchcase(alias, candidate):
                return True
        return False

    @staticmethod
    def _directive(rendered: str) -> tuple[str, str]:
        fields = rendered.split(None, 1)
        if len(fields) != 2 or not fields[0] or not fields[1].strip():
            raise _rejected("The SSH configuration contains a malformed directive.")
        return fields[0], fields[1].strip()

    @staticmethod
    def _one(values: dict[str, list[str]], key: str) -> str:
        candidates = values.get(key, [])
        if len(candidates) != 1 or not candidates[0]:
            raise _rejected(f"The SSH alias must declare exactly one {key} value.")
        return candidates[0]

    def _expand_identity_file(self, value: str) -> str:
        if value.startswith("~/"):
            value = str(self._paths.home / value[2:])
        if value.startswith("~") or any(marker in value for marker in ("%", "$", "*", "?")):
            raise _rejected("The SSH identity file contains unsupported expansion tokens.")
        path = Path(value)
        if not path.is_absolute():
            raise _rejected("The SSH identity file must resolve to one absolute path.")
        return str(path)


__all__ = [
    "ConstrainedGitRemoteParser",
    "ConstrainedSshConfigAliasResolver",
    "EffectiveUserPaths",
]
