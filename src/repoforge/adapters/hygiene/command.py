"""Fixed-argv hygiene adapter routed through the unified execution boundary."""

from __future__ import annotations

import io
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from ...application.execution.coordinator import ExecutionCoordinator
from ...application.execution.requests import hygiene_execution_request
from ...domain.errors import CommandError, SecurityError, WorkspaceError
from ...domain.hygiene import FormatterPolicy, HygieneFinding, HygieneParserKind
from ...ports.command import CommandResult
from ...ports.hygiene import HygieneFormatReceipt, HygieneInspection


class CommandHygieneGateway:
    def __init__(self, execution: ExecutionCoordinator) -> None:
        self._execution = execution

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
                f"Formatter check failed with exit code {result.returncode}: "
                f"{result.combined or '<no output>'}"
            )
        if result.returncode == 1 and not findings:
            raise CommandError("Formatter output did not match the reviewed ruff_format parser")
        return tuple(sorted(set(findings)))

    def _inspect(
        self,
        root: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
        *,
        snapshot: bool,
    ) -> HygieneInspection:
        resolved_paths = self._validate_paths(paths, policy)
        argv = (*policy.check_argv, *resolved_paths)
        request = hygiene_execution_request(
            root=root,
            command_cwd=root,
            argv=argv,
            timeout_seconds=policy.timeout_seconds,
            output_limit=policy.output_limit,
            read_only=True,
            snapshot=snapshot,
        )
        if not resolved_paths:
            inspection = self._execution.inspect(request)
            return HygieneInspection((), inspection.identity.identity_hash, "", False)
        for relative in resolved_paths:
            candidate = root / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise SecurityError(f"Formatter input is not a regular file: {relative}")
        with self._execution.prepare(request) as session:
            receipt = session.execute(argv)
            session.inspect()
        result = receipt.result
        if policy.parser is HygieneParserKind.RUFF_FORMAT:
            findings = self._parse_ruff_format(result)
        else:  # pragma: no cover - closed enum, retained as fail-closed guard.
            raise CommandError(f"Unsupported formatter parser: {policy.parser.value}")
        return HygieneInspection(
            findings,
            receipt.session_start_identity_hash,
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
        argv = ("git", "archive", "--format=tar", commit_sha)
        request = hygiene_execution_request(
            root=repository,
            command_cwd=repository,
            argv=argv,
            timeout_seconds=120,
            output_limit=max_archive_bytes,
            read_only=True,
            snapshot=True,
        )
        with self._execution.prepare(request) as session:
            archive = session.execute_bytes(argv, max_bytes=max_archive_bytes)
            session.inspect()
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
        return self._inspect(workspace, policy, paths, snapshot=False)

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
            return self._inspect(root, policy, resolved_paths, snapshot=True)

    def format_paths(
        self,
        workspace: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
    ) -> HygieneFormatReceipt:
        resolved_paths = self._validate_paths(paths, policy)
        argv = (*policy.fix_argv, *resolved_paths)
        request = hygiene_execution_request(
            root=workspace,
            command_cwd=workspace,
            argv=argv,
            timeout_seconds=policy.timeout_seconds,
            output_limit=policy.output_limit,
            read_only=False,
            snapshot=False,
        )
        if not resolved_paths:
            inspection = self._execution.inspect(request)
            return HygieneFormatReceipt(inspection.identity.identity_hash, "", False)
        for relative in resolved_paths:
            candidate = workspace / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise SecurityError(f"Formatter input is not a regular file: {relative}")
        with self._execution.prepare(request) as session:
            receipt = session.execute(argv)
            session.inspect()
        result = receipt.result
        if result.returncode != 0:
            raise CommandError(
                f"Formatter fix failed with exit code {result.returncode}: "
                f"{result.combined or '<no output>'}"
            )
        return HygieneFormatReceipt(
            receipt.session_start_identity_hash,
            result.combined,
            result.stdout_truncated or result.stderr_truncated,
        )
