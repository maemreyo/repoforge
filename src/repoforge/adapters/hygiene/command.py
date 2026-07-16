"""Fixed-argv hygiene adapter with exact-commit archive materialization."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from ...domain.errors import CommandError, SecurityError, WorkspaceError
from ...domain.hygiene import FormatterPolicy, HygieneFinding, HygieneParserKind
from ...ports.command import CommandExecutor, CommandResult
from ...ports.hygiene import HygieneFormatReceipt, HygieneInspection


class CommandHygieneGateway:
    def __init__(self, executor: CommandExecutor) -> None:
        self._executor = executor

    @staticmethod
    def _validate_paths(paths: tuple[str, ...], policy: FormatterPolicy) -> tuple[str, ...]:
        if len(paths) > policy.max_paths:
            raise WorkspaceError(
                f"Formatter path count {len(paths)} exceeds max_paths={policy.max_paths}"
            )
        normalized: list[str] = []
        for raw in paths:
            path = raw.replace("\\", "/")
            parsed = PurePosixPath(path)
            if (
                not path
                or path.startswith("/")
                or parsed.is_absolute()
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or any(ord(character) < 32 for character in path)
            ):
                raise SecurityError(f"Formatter path is unsafe: {raw!r}")
            if path not in normalized:
                normalized.append(path)
        return tuple(sorted(normalized))

    def _environment_identity(self, cwd: Path, policy: FormatterPolicy) -> str:
        try:
            if policy.parser is HygieneParserKind.RUFF_FORMAT and "format" in policy.check_argv:
                index = policy.check_argv.index("format")
                version_argv = (*policy.check_argv[:index], "--version")
            else:
                version_argv = (policy.check_argv[0], "--version")
            result = self._executor.run(
                version_argv,
                cwd=cwd,
                timeout=min(10, policy.timeout_seconds),
                check=False,
                output_limit=512,
            )
            version = (result.stdout or result.stderr).strip().splitlines()
            version_text = (
                version[0][:256] if result.returncode == 0 and version else "<unavailable>"
            )
        except CommandError:
            version_text = "<unavailable>"
        payload = {
            "architecture": platform.machine(),
            "executable": policy.check_argv[0],
            "platform": platform.system(),
            "python": platform.python_version(),
            "runtime": sys.implementation.name,
            "tool_version": version_text,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _parse_ruff_format(result: CommandResult) -> tuple[HygieneFinding, ...]:
        findings: list[HygieneFinding] = []
        for raw_line in result.combined.splitlines():
            line = raw_line.strip()
            prefix = "Would reformat:"
            if not line.startswith(prefix):
                continue
            path = line[len(prefix) :].strip()
            findings.append(HygieneFinding.create(path, "ruff-format", "Would reformat"))
        if result.returncode not in {0, 1}:
            raise CommandError(
                f"Formatter check failed with exit code {result.returncode}: {result.combined or '<no output>'}"
            )
        if result.returncode == 1 and not findings:
            raise CommandError("Formatter output did not match the reviewed ruff_format parser")
        return tuple(sorted(set(findings)))

    def _inspect(
        self,
        root: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
    ) -> HygieneInspection:
        resolved_paths = self._validate_paths(paths, policy)
        environment_identity = self._environment_identity(root, policy)
        if not resolved_paths:
            return HygieneInspection((), environment_identity, "", False)
        for relative in resolved_paths:
            candidate = root / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise SecurityError(f"Formatter input is not a regular file: {relative}")
        result = self._executor.run(
            (*policy.check_argv, *resolved_paths),
            cwd=root,
            timeout=policy.timeout_seconds,
            check=False,
            output_limit=policy.output_limit,
        )
        if policy.parser is HygieneParserKind.RUFF_FORMAT:
            findings = self._parse_ruff_format(result)
        else:  # pragma: no cover - enum is closed, retained as fail-closed guard.
            raise CommandError(f"Unsupported formatter parser: {policy.parser.value}")
        return HygieneInspection(
            findings,
            environment_identity,
            result.combined,
            result.stdout_truncated or result.stderr_truncated,
        )

    @staticmethod
    def _safe_member_name(name: str) -> str:
        normalized = name.replace("\\", "/").rstrip("/")
        parsed = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise SecurityError(f"Git archive contains an unsafe path: {name!r}")
        return normalized

    def _materialize_archive(
        self,
        repository: Path,
        commit_sha: str,
        destination: Path,
        *,
        max_archive_bytes: int,
        selected_paths: frozenset[str],
    ) -> None:
        archive = self._executor.run_bytes(
            ("git", "archive", "--format=tar", commit_sha),
            cwd=repository,
            timeout=120,
            max_bytes=max_archive_bytes,
        )
        total_regular_bytes = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
                for member in handle:
                    name = self._safe_member_name(member.name)
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise SecurityError(f"Git archive contains a non-regular entry: {name}")
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise SecurityError(f"Git archive contains an unsupported entry: {name}")
                    total_regular_bytes += member.size
                    if member.size < 0 or total_regular_bytes > max_archive_bytes:
                        raise SecurityError(
                            "Git archive exceeds the reviewed extraction byte bound"
                        )
                    if name not in selected_paths:
                        continue
                    source = handle.extractfile(member)
                    if source is None:
                        raise SecurityError(f"Git archive entry cannot be read: {name}")
                    data = source.read(member.size + 1)
                    if len(data) != member.size:
                        raise SecurityError(f"Git archive entry size mismatch: {name}")
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
        except tarfile.TarError as exc:
            raise SecurityError("Git archive is not a valid bounded tar stream") from exc

    def inspect_workspace(
        self,
        workspace: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
    ) -> HygieneInspection:
        return self._inspect(workspace, policy, paths)

    def inspect_base(
        self,
        repository: Path,
        commit_sha: str,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
        *,
        max_archive_bytes: int,
    ) -> HygieneInspection:
        resolved_paths = self._validate_paths(paths, policy)
        with tempfile.TemporaryDirectory(prefix="repoforge-hygiene-base-") as temporary:
            root = Path(temporary)
            self._materialize_archive(
                repository,
                commit_sha,
                root,
                max_archive_bytes=max_archive_bytes,
                selected_paths=frozenset(resolved_paths),
            )
            return self._inspect(root, policy, resolved_paths)

    def format_paths(
        self,
        workspace: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
    ) -> HygieneFormatReceipt:
        resolved_paths = self._validate_paths(paths, policy)
        environment_identity = self._environment_identity(workspace, policy)
        if not resolved_paths:
            return HygieneFormatReceipt(environment_identity, "", False)
        for relative in resolved_paths:
            candidate = workspace / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise SecurityError(f"Formatter input is not a regular file: {relative}")
        result = self._executor.run(
            (*policy.fix_argv, *resolved_paths),
            cwd=workspace,
            timeout=policy.timeout_seconds,
            check=False,
            output_limit=policy.output_limit,
        )
        if result.returncode != 0:
            raise CommandError(
                f"Formatter fix failed with exit code {result.returncode}: {result.combined or '<no output>'}"
            )
        return HygieneFormatReceipt(
            environment_identity,
            result.combined,
            result.stdout_truncated or result.stderr_truncated,
        )
