"""Contained one-shot CodeGraph CLI boundary."""

from __future__ import annotations

import threading
from pathlib import Path

from ...domain.provider_manifest import (
    ProviderAvailabilityStatus,
    ProviderExecutableIdentity,
    ProviderManifest,
)
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandExecutor
from ...ports.provider_registry import ProviderRegistry
from .command_contract import (
    CodeGraphCommandOutput,
    affected_paths,
    argument,
    command_environment,
    graph_depth,
    prepare_roots,
    result_limit,
    unavailable,
)


class CodeGraphCommandRunner:
    """Build and execute only the reviewed one-shot CodeGraph CLI surface."""

    def __init__(
        self,
        manifest: ProviderManifest,
        registry: ProviderRegistry,
        executor: CommandExecutor,
    ) -> None:
        if manifest.codegraph is None:
            raise ValueError("CodeGraph command runner requires CodeGraph enrollment options")
        if not isinstance(manifest.runtime, ProviderExecutableIdentity):
            raise ValueError("CodeGraph command runner requires an executable provider")
        self._manifest = manifest
        self._options = manifest.codegraph
        self._registry = registry
        self._executor = executor
        self._verification_lock = threading.Lock()
        self._verified_identity: tuple[str, str] | None = None

    def version(
        self,
        projection_root: Path,
        home_root: Path,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> str:
        projection, home = prepare_roots(projection_root, home_root)
        executable = self._verified_executable()
        version = self._run_version(executable, projection, home, cancel_token)
        with self._verification_lock:
            self._verified_identity = (self._manifest.manifest_hash, executable)
        return version

    def init(
        self,
        projection_root: Path,
        home_root: Path,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        projection, home, executable = self._ready(projection_root, home_root, cancel_token)
        return self._execute(
            "init",
            (executable, "init", str(projection)),
            projection,
            home,
            timeout=self._options.init_timeout_seconds,
            cancel_token=cancel_token,
        )

    def sync(
        self,
        projection_root: Path,
        home_root: Path,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        projection, home, executable = self._ready(projection_root, home_root, cancel_token)
        return self._execute(
            "sync",
            (executable, "sync", str(projection), "--quiet"),
            projection,
            home,
            timeout=self._options.sync_timeout_seconds,
            cancel_token=cancel_token,
        )

    def status(
        self,
        projection_root: Path,
        home_root: Path,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        projection, home, executable = self._ready(projection_root, home_root, cancel_token)
        return self._execute(
            "status",
            (executable, "status", str(projection), "--json"),
            projection,
            home,
            timeout=self._options.query_timeout_seconds,
            cancel_token=cancel_token,
        )

    def affected(
        self,
        projection_root: Path,
        home_root: Path,
        paths: tuple[str, ...],
        *,
        depth: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        normalized = affected_paths(paths, self._options)
        actual_depth = graph_depth(depth, self._options)
        projection, home, executable = self._ready(projection_root, home_root, cancel_token)
        return self._execute(
            "affected",
            (
                executable,
                "affected",
                *normalized,
                "--depth",
                str(actual_depth),
                "--json",
            ),
            projection,
            home,
            timeout=self._options.query_timeout_seconds,
            cancel_token=cancel_token,
        )

    def query(
        self,
        projection_root: Path,
        home_root: Path,
        search: str,
        *,
        limit: int = 20,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        return self._symbol_command(
            "query",
            projection_root,
            home_root,
            argument(search, "query"),
            ("--limit", str(result_limit(limit))),
            cancel_token,
        )

    def callers(
        self,
        projection_root: Path,
        home_root: Path,
        symbol: str,
        *,
        limit: int = 50,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        return self._symbol_command(
            "callers",
            projection_root,
            home_root,
            argument(symbol, "symbol"),
            ("--limit", str(result_limit(limit))),
            cancel_token,
        )

    def callees(
        self,
        projection_root: Path,
        home_root: Path,
        symbol: str,
        *,
        limit: int = 50,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        return self._symbol_command(
            "callees",
            projection_root,
            home_root,
            argument(symbol, "symbol"),
            ("--limit", str(result_limit(limit))),
            cancel_token,
        )

    def impact(
        self,
        projection_root: Path,
        home_root: Path,
        symbol: str,
        *,
        depth: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CodeGraphCommandOutput:
        return self._symbol_command(
            "impact",
            projection_root,
            home_root,
            argument(symbol, "symbol"),
            ("--depth", str(graph_depth(depth, self._options))),
            cancel_token,
        )

    def _symbol_command(
        self,
        command: str,
        projection_root: Path,
        home_root: Path,
        value: str,
        options: tuple[str, str],
        cancel_token: CancellationToken | None,
    ) -> CodeGraphCommandOutput:
        projection, home, executable = self._ready(projection_root, home_root, cancel_token)
        return self._execute(
            command,
            (executable, command, value, *options, "--json"),
            projection,
            home,
            timeout=self._options.query_timeout_seconds,
            cancel_token=cancel_token,
        )

    def _ready(
        self,
        projection_root: Path,
        home_root: Path,
        cancel_token: CancellationToken | None,
    ) -> tuple[Path, Path, str]:
        projection, home = prepare_roots(projection_root, home_root)
        executable = self._verified_executable()
        identity = (self._manifest.manifest_hash, executable)
        with self._verification_lock:
            verified = self._verified_identity == identity
        if not verified:
            self._run_version(executable, projection, home, cancel_token)
            with self._verification_lock:
                self._verified_identity = identity
        return projection, home, executable

    def _verified_executable(self) -> str:
        registered = self._registry.get_provider(self._manifest.provider_id)
        if registered is None or registered.manifest_hash != self._manifest.manifest_hash:
            raise unavailable("Reviewed CodeGraph enrollment is unavailable or changed")
        availability = self._registry.check_availability(self._manifest.provider_id)
        executable = availability.resolved_executable
        if (
            availability.status is not ProviderAvailabilityStatus.AVAILABLE
            or executable is None
            or not Path(executable).is_absolute()
        ):
            raise unavailable("Reviewed CodeGraph executable is unavailable")
        return str(Path(executable).resolve())

    def _run_version(
        self,
        executable: str,
        projection: Path,
        home: Path,
        cancel_token: CancellationToken | None,
    ) -> str:
        output = self._execute(
            "version",
            (executable, "version"),
            projection,
            home,
            timeout=self._options.query_timeout_seconds,
            cancel_token=cancel_token,
        )
        if output.truncated:
            raise unavailable("CodeGraph version output exceeded its reviewed bound")
        observed = output.stdout.strip()
        if observed != self._manifest.version:
            raise unavailable("CodeGraph executable version does not match the reviewed enrollment")
        return observed

    def _execute(
        self,
        command: str,
        argv: tuple[str, ...],
        projection: Path,
        home: Path,
        *,
        timeout: int,
        cancel_token: CancellationToken | None,
    ) -> CodeGraphCommandOutput:
        try:
            result = self._executor.run_isolated(
                argv,
                cwd=projection,
                environment=command_environment(home),
                secrets=(),
                timeout=timeout,
                check=False,
                output_limit=min(
                    self._manifest.output_bounds.max_stdout_chars,
                    self._manifest.output_bounds.max_stderr_chars,
                ),
                cancel_token=cancel_token,
            )
        except Exception as exc:
            raise unavailable(f"CodeGraph {command} command could not be completed") from exc
        if result.returncode != 0:
            suffix = (
                " was cancelled"
                if cancel_token is not None and cancel_token.is_cancelled()
                else " failed"
            )
            raise unavailable(f"CodeGraph {command} command{suffix}")
        return CodeGraphCommandOutput(
            command=command,
            stdout=result.stdout,
            truncated=result.stdout_truncated or result.stderr_truncated,
        )


__all__ = ["CodeGraphCommandOutput", "CodeGraphCommandRunner"]
