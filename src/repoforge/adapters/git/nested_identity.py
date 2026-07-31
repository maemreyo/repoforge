"""Bounded read-only Git discovery for submodule and LFS identity targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.nested_identity import (
    NestedResourceCandidate,
    NestedResourceKind,
    canonical_nested_endpoint,
    nested_endpoint_digest,
)
from ...ports.command import CommandExecutor, CommandResult
from ...ports.nested_identity import NestedDiscoveryRequest

_GITMODULE_LINE = re.compile(
    r"^submodule\.(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\."
    r"(?P<field>path|url)[ \t]+(?P<value>\S(?:.*\S)?)$"
)
_UNCHANGED = ("No nested credentialed action was admitted.",)


@dataclass(frozen=True, slots=True)
class _SubmoduleEntry:
    name: str
    path: str
    url: str


def _failure(
    message: str,
    *,
    code: ErrorCode = ErrorCode.SECURITY_POLICY_VIOLATION,
    retryable: bool = False,
    details: dict[str, object] | None = None,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=_UNCHANGED,
        safe_next_action=(
            "Correct the exact repository-local nested resource configuration and start a new operation."
        ),
        details=details,
    )


def _source_prefix(root: Path, current: Path) -> str:
    relative = current.relative_to(root).as_posix()
    return "" if relative == "." else f"{relative}/"


def _safe_submodule_path(value: str, *, current: Path, root: Path) -> tuple[str, Path]:
    if not value or "\\" in value or any(character.isspace() for character in value):
        raise _failure("Nested submodule path is malformed.")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise _failure("Nested submodule path escapes the reviewed repository.")
    lexical = current
    for part in pure.parts:
        lexical /= part
        if lexical.is_symlink():
            raise _failure("Nested submodule path cannot contain symlinks.")
    resolved = lexical.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise _failure("Nested submodule path escapes the reviewed repository.")
    return pure.as_posix(), resolved


class GitNestedResourceDiscovery:
    """Discover nested endpoints without fetching or consulting credential state."""

    def __init__(self, executor: CommandExecutor) -> None:
        self._executor = executor

    @staticmethod
    def _config_argv(config_file: str, key: str, *, regexp: bool) -> tuple[str, ...]:
        action = "--get-regexp" if regexp else "--get"
        return ("git", "config", "--file", config_file, action, key)

    def _config(
        self,
        request: NestedDiscoveryRequest,
        *,
        cwd: Path,
        config_file: str,
        key: str,
        regexp: bool,
        missing_allowed: bool,
    ) -> CommandResult:
        argv = self._config_argv(config_file, key, regexp=regexp)
        try:
            result = self._executor.run(
                argv,
                cwd=cwd,
                timeout=request.command_timeout_seconds,
                check=False,
                output_limit=request.max_output_bytes,
            )
        except RepoForgeError as exc:
            if exc.code not in {ErrorCode.COMMAND_FAILED, ErrorCode.COMMAND_TIMEOUT}:
                raise
            raise _failure(
                "Nested resource configuration inspection failed.",
                code=exc.code,
                retryable=exc.retryable,
                details={"config_file": config_file},
            ) from None
        if result.stdout_truncated or result.stderr_truncated:
            raise _failure(
                "Nested resource configuration exceeded the reviewed output bound.",
                details={"config_file": config_file},
            )
        if result.returncode == 1 and missing_allowed:
            return CommandResult(argv, str(cwd), 0, "", "")
        if result.returncode != 0:
            raise _failure(
                "Nested resource configuration inspection failed.",
                code=ErrorCode.COMMAND_FAILED,
                details={"config_file": config_file, "returncode": result.returncode},
            )
        return result

    def _submodules(
        self,
        request: NestedDiscoveryRequest,
        *,
        current: Path,
        root: Path,
    ) -> tuple[_SubmoduleEntry, ...]:
        config_path = current / ".gitmodules"
        if not config_path.exists():
            return ()
        if config_path.is_symlink() or not config_path.is_file():
            raise _failure("Repository-local .gitmodules must be a regular file.")
        result = self._config(
            request,
            cwd=current,
            config_file=".gitmodules",
            key=r"^submodule\..*\.(path|url)$",
            regexp=True,
            missing_allowed=False,
        )
        values: dict[str, dict[str, str]] = {}
        for raw_line in result.stdout.splitlines():
            match = _GITMODULE_LINE.fullmatch(raw_line)
            if match is None:
                raise _failure("Repository-local .gitmodules output is malformed.")
            name = match.group("name")
            field = match.group("field")
            value = match.group("value")
            record = values.setdefault(name, {})
            if field in record:
                raise _failure("Repository-local .gitmodules contains duplicate fields.")
            record[field] = value
        entries: list[_SubmoduleEntry] = []
        seen_paths: set[str] = set()
        for name in sorted(values):
            record = values[name]
            if set(record) != {"path", "url"}:
                raise _failure("Repository-local .gitmodules entry is incomplete.")
            normalized_path, _ = _safe_submodule_path(record["path"], current=current, root=root)
            if normalized_path in seen_paths:
                raise _failure("Repository-local .gitmodules contains duplicate paths.")
            seen_paths.add(normalized_path)
            entries.append(_SubmoduleEntry(name, normalized_path, record["url"]))
        return tuple(entries)

    def _lfs_candidate(
        self,
        request: NestedDiscoveryRequest,
        *,
        current: Path,
        root: Path,
        repository_endpoint: str,
        depth: int,
    ) -> NestedResourceCandidate:
        config_path = current / ".lfsconfig"
        source = f"{_source_prefix(root, current)}repository-default:lfs"
        endpoint = f"{repository_endpoint.rstrip('/')}/info/lfs"
        if config_path.exists():
            if config_path.is_symlink() or not config_path.is_file():
                raise _failure("Repository-local .lfsconfig must be a regular file.")
            result = self._config(
                request,
                cwd=current,
                config_file=".lfsconfig",
                key="lfs.url",
                regexp=False,
                missing_allowed=True,
            )
            lines = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
            if len(lines) > 1:
                raise _failure("Repository-local .lfsconfig contains multiple LFS endpoints.")
            if lines:
                endpoint = lines[0]
                source = f"{_source_prefix(root, current)}.lfsconfig:lfs.url"
        canonical = canonical_nested_endpoint(endpoint, base_endpoint=repository_endpoint)
        return NestedResourceCandidate(
            kind=NestedResourceKind.LFS,
            access=request.lfs_access,
            canonical_endpoint=canonical,
            source_location=source,
            depth=depth,
            endpoint_digest=nested_endpoint_digest(canonical),
        )

    def discover(self, request: NestedDiscoveryRequest) -> tuple[NestedResourceCandidate, ...]:
        if not isinstance(request, NestedDiscoveryRequest):
            raise ValueError("request must be a NestedDiscoveryRequest")
        root = request.root.resolve(strict=False)
        if root != request.root or not root.is_dir():
            raise _failure("Nested discovery root must be a canonical existing directory.")
        primary_endpoint = canonical_nested_endpoint(request.primary_endpoint)
        discovered: list[NestedResourceCandidate] = []

        def add(candidate: NestedResourceCandidate) -> None:
            if len(discovered) >= request.max_resources:
                raise _failure("Nested resource count exceeds the reviewed discovery bound.")
            discovered.append(candidate)

        def walk(
            current: Path,
            repository_endpoint: str,
            depth: int,
            path_ancestry: tuple[Path, ...],
            endpoint_ancestry: tuple[str, ...],
        ) -> None:
            if request.include_lfs:
                add(
                    self._lfs_candidate(
                        request,
                        current=current,
                        root=root,
                        repository_endpoint=repository_endpoint,
                        depth=depth,
                    )
                )
            for entry in self._submodules(request, current=current, root=root):
                nested_depth = depth + 1
                if nested_depth > request.max_depth:
                    raise _failure("Nested submodule depth exceeds the reviewed discovery bound.")
                relative_path, checked_path = _safe_submodule_path(
                    entry.path, current=current, root=root
                )
                canonical = canonical_nested_endpoint(
                    entry.url,
                    base_endpoint=repository_endpoint,
                )
                digest = nested_endpoint_digest(canonical)
                if checked_path in path_ancestry or digest in endpoint_ancestry:
                    raise _failure(
                        "Nested submodule cycle detected.",
                        details={"endpoint_digest": digest},
                    )
                source_location = f"{_source_prefix(root, current)}.gitmodules:{relative_path}"
                add(
                    NestedResourceCandidate(
                        kind=NestedResourceKind.SUBMODULE,
                        access=request.submodule_access,
                        canonical_endpoint=canonical,
                        source_location=source_location,
                        depth=nested_depth,
                        endpoint_digest=digest,
                    )
                )
                git_marker = checked_path / ".git"
                if checked_path.is_dir() and git_marker.exists() and not git_marker.is_symlink():
                    walk(
                        checked_path,
                        canonical,
                        nested_depth,
                        (*path_ancestry, checked_path),
                        (*endpoint_ancestry, digest),
                    )

        walk(
            root,
            primary_endpoint,
            0,
            (root,),
            (nested_endpoint_digest(primary_endpoint),),
        )
        return tuple(
            sorted(
                discovered,
                key=lambda item: (
                    item.depth,
                    item.kind.value,
                    item.source_location,
                    item.endpoint_digest,
                ),
            )
        )


__all__ = ["GitNestedResourceDiscovery"]
