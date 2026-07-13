"""One-command setup, repository management, and secure tunnel startup."""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import DEFAULT_CONFIG_PATH, load_config
from .config_delta import classify_capability_delta
from .errors import ConfigError, PersonalCodingMCPError
from .proposal import assess_repository_proposal
from .runtime import (
    managed_start_claim,
    read_managed_runtime,
    read_runtime_log,
    read_runtime_state,
    stop_managed_runtime,
    write_managed_runtime,
)
from .security import slugify
from .service import CodingService
from .user_config import (
    TunnelSettings,
    UserConfig,
    UserRepository,
    atomic_write,
    build_lock_text,
    config_history,
    config_kind,
    detect_repository_for_setup,
    generation_snapshot_path,
    load_user_config,
    lock_generation,
    profile_summary,
    read_toml,
    resolve_runtime_config_path,
    resolved_config_path,
    rollback_generation,
    sha256_file,
    sha256_text,
    write_user_and_lock,
)


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _proposal_id(*, config_sha256: str, repo_id: str, path: Path, profiles: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "config_sha256": config_sha256,
            "repo_id": repo_id,
            "path": str(path),
            "profiles": profiles,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tunnel_state_path(config_path: Path) -> Path:
    return resolved_config_path(config_path).with_name("tunnel-profile.json")


def _runtime_state_path(config_path: Path) -> Path:
    return resolved_config_path(config_path).with_name("runtime.json")


def _managed_runtime_path(config_path: Path) -> Path:
    return resolved_config_path(config_path).with_name("managed-runtime.json")


def _managed_runtime_log_path(config_path: Path) -> Path:
    return resolved_config_path(config_path).with_name("managed-runtime.log")


def _managed_runtime_start_claim_path(config_path: Path) -> Path:
    return resolved_config_path(config_path).with_name("managed-runtime.start.lock")


def _activation_result(config_path: Path, repo_id: str | None = None) -> dict[str, Any]:
    generation = lock_generation(resolved_config_path(config_path))
    active = read_runtime_state(_runtime_state_path(config_path))
    active_generation = active.active_generation if active else None
    restart_required = active is not None and active_generation != generation
    result: dict[str, Any] = {
        "config_generation": generation,
        "active_generation": active_generation,
        "restart_required": restart_required,
    }
    if restart_required:
        target = f" repository {repo_id!r}" if repo_id else ""
        result.update(
            {
                "status": "restart_required",
                "safe_next_action": f"Restart the running `rf start` process to activate{target}.",
            }
        )
    else:
        result["status"] = "active" if active else "stopped"
    return result


def _runtime_health(managed: Any, active: Any) -> list[dict[str, Any]]:
    """Return bounded local runtime health evidence without invoking external commands."""
    return [
        {
            "name": "managed_tunnel",
            "ok": managed is not None,
            "detail": "managed tunnel process is live" if managed else "no managed tunnel process",
        },
        {
            "name": "mcp_process",
            "ok": active is not None,
            "detail": "MCP child reported an active generation"
            if active
            else "MCP child has not reported an active generation",
        },
    ]


def _activate_managed_runtime(
    config_path: Path, previous_generation: int, *, rollback_allowed: bool
) -> dict[str, Any]:
    """Restart a supervisor-owned runtime, restoring its prior generation on failure."""
    managed_path = _managed_runtime_path(config_path)
    if read_managed_runtime(managed_path) is None:
        return _activation_result(config_path)
    stop_managed_runtime(managed_path)
    args = argparse.Namespace(
        config=str(config_path),
        tunnel_id=None,
        profile=None,
        skip_doctor=False,
        dry_run=False,
        managed=True,
    )
    try:
        if _start(args, emit=False) != 0:
            raise ConfigError("Managed runtime restart failed")
    except ConfigError:
        if not rollback_allowed:
            raise ConfigError(
                "Managed runtime activation failed; restrictive configuration remains active on disk"
            ) from None
        rollback_generation(config_path, previous_generation)
        if _start(args, emit=False) != 0:
            raise ConfigError("Managed runtime rollback restart failed") from None
        return {
            "status": "rolled_back",
            "config_generation": previous_generation,
            "active_generation": previous_generation,
            "restart_required": False,
            "safe_next_action": "Review the failed generation before accepting it again.",
        }
    return {
        "status": "active",
        "config_generation": lock_generation(resolved_config_path(config_path)),
        "active_generation": lock_generation(resolved_config_path(config_path)),
        "restart_required": False,
        "safe_next_action": "Repository changes are active in the managed runtime.",
    }


class _SmokeService(Protocol):
    def repo_status(self, repo_id: str) -> dict[str, Any]: ...

    def repo_context(self, repo_id: str) -> dict[str, Any]: ...

    def workspace_create(
        self, repo_id: str, task_slug: str, base: str | None = None
    ) -> dict[str, Any]: ...

    def workspace_status(self, workspace_id: str) -> dict[str, Any]: ...

    def workspace_tree(self, workspace_id: str, max_entries: int = 2000) -> dict[str, Any]: ...

    def workspace_diff(self, workspace_id: str, staged: bool = False) -> dict[str, Any]: ...

    def workspace_remove(
        self, workspace_id: str, delete_local_branch: bool = False
    ) -> dict[str, Any]: ...


def _smoke_repository(service: _SmokeService, repo_id: str) -> dict[str, Any]:
    service.repo_status(repo_id)
    service.repo_context(repo_id)
    workspace = service.workspace_create(repo_id, "repoforge-setup-smoke")
    workspace_id = str(workspace["workspace_id"])
    try:
        service.workspace_status(workspace_id)
        service.workspace_tree(workspace_id, 50)
        service.workspace_diff(workspace_id)
    finally:
        service.workspace_remove(workspace_id, delete_local_branch=True)
    return {"repo_id": repo_id, "ok": True}


def _setup(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if config_path.exists() and not args.force:
        raise ConfigError(
            f"Configuration already exists: {config_path}. Use `rf repo add`, or pass --force."
        )
    repositories: list[UserRepository] = []
    seen: set[str] = set()
    for value in args.repos:
        repo_path = Path(value).expanduser().resolve()
        repo_id = slugify(repo_path.name)
        if repo_id in seen:
            raise ConfigError(f"Duplicate detected repository id {repo_id!r}; rename one directory")
        repositories.append(UserRepository(repo_id=repo_id, path=repo_path))
        seen.add(repo_id)
    user_config = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings(tunnel_id=args.tunnel_id, profile=args.profile),
        repositories=tuple(repositories),
    )
    lock_path, detections = write_user_and_lock(user_config)
    service = CodingService(load_config(lock_path))
    doctor = service.doctor()
    smoke: list[dict[str, Any]] = []
    if not args.skip_smoke and doctor["ok"]:
        for repository in repositories:
            try:
                smoke.append(_smoke_repository(service, repository.repo_id))
            except Exception as exc:
                smoke.append(
                    {
                        "repo_id": repository.repo_id,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
    result = {
        "ok": bool(doctor["ok"]) and all(item["ok"] for item in smoke),
        "config": str(config_path),
        "resolved_config": str(lock_path),
        "tunnel_id": args.tunnel_id,
        "repositories": [repository.repo_id for repository in repositories],
        "profiles": profile_summary(detections),
        "doctor": doctor["summary"],
        "smoke": smoke,
        "next": "Run `rf start`.",
    }
    _json(result)
    return 0 if result["ok"] else 1


def _load_minimal_or_error(path: str | Path) -> UserConfig:
    return load_user_config(Path(path).expanduser().resolve())


def _repo_list(args: argparse.Namespace) -> int:
    config = _load_minimal_or_error(args.config)
    lock_path = resolved_config_path(config.source_path)
    try:
        runtime = resolve_runtime_config_path(config.source_path)
        lock_status = "current"
    except ConfigError as exc:
        runtime = lock_path
        lock_status = str(exc)
    _json(
        {
            "config": str(config.source_path),
            "resolved_config": str(runtime),
            "lock_status": lock_status,
            "repositories": [
                {"repo_id": repository.repo_id, "path": str(repository.path)}
                for repository in config.repositories
            ],
        }
    )
    return 0


def _repo_add(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    expected_source_sha256 = sha256_file(config_path)
    config = _load_minimal_or_error(config_path)
    previous_generation = lock_generation(resolved_config_path(config_path))
    path = Path(args.path).expanduser().resolve()
    repo_id = args.repo_id or slugify(path.name)
    detection = detect_repository_for_setup(path, repo_id)
    if repo_id in {repository.repo_id for repository in config.repositories}:
        raise ConfigError(f"Repository id already exists: {repo_id}")
    if path in {repository.path for repository in config.repositories}:
        raise ConfigError(f"Repository path already exists: {path}")
    profiles = profile_summary([detection])[repo_id]
    assessment = assess_repository_proposal(detection)
    proposal_id = _proposal_id(
        config_sha256=expected_source_sha256,
        repo_id=repo_id,
        path=path,
        profiles=profiles,
    )
    requires_approval = hasattr(args, "approve") and args.approve != proposal_id
    if getattr(args, "preview", False) or requires_approval or assessment.decisions:
        _json(
            {
                "status": "pending_approval",
                "repo_id": repo_id,
                "path": str(path),
                "proposal_id": proposal_id,
                "confidence": assessment.confidence.value,
                "findings": list(assessment.findings),
                "required_decisions": [
                    {
                        "code": decision.code,
                        "prompt": decision.prompt,
                        "choices": list(decision.choices),
                    }
                    for decision in assessment.decisions
                ],
                "capability_delta": "expansion",
                "profiles": profiles,
                "safe_next_action": (
                    f"Re-run `rf repo add {path} --repo-id {repo_id} --approve {proposal_id}` "
                    "to enroll this exact reviewed proposal."
                ),
            }
        )
        return 0
    updated = replace(
        config,
        repositories=(*config.repositories, UserRepository(repo_id=repo_id, path=path)),
    )
    lock_path, detections = write_user_and_lock(
        updated, expected_source_sha256=expected_source_sha256
    )
    _json(
        {
            "added": repo_id,
            "path": str(path),
            "config": str(config.source_path),
            "resolved_config": str(lock_path),
            "profiles": profile_summary(detections)[repo_id],
            "proposal_id": proposal_id,
        }
        | _activate_managed_runtime(config_path, previous_generation, rollback_allowed=True)
    )
    return 0


def _repo_inspect(args: argparse.Namespace) -> int:
    """Return detected repository capability without writing configuration."""
    path = Path(args.path).expanduser().resolve()
    repo_id = args.repo_id or slugify(path.name)
    detection = detect_repository_for_setup(path, repo_id)
    profiles = profile_summary([detection])[detection.repo_id]
    assessment = assess_repository_proposal(detection)
    _json(
        {
            "status": "pending_approval",
            "repo_id": detection.repo_id,
            "path": str(detection.path),
            "ecosystem": detection.ecosystem,
            "package_manager": detection.package_manager,
            "instruction_files": list(detection.instruction_files),
            "warnings": list(detection.warnings),
            "confidence": assessment.confidence.value,
            "findings": list(assessment.findings),
            "required_decisions": [
                {
                    "code": decision.code,
                    "prompt": decision.prompt,
                    "choices": list(decision.choices),
                }
                for decision in assessment.decisions
            ],
            "proposal_id": _proposal_id(
                config_sha256="inspection",
                repo_id=detection.repo_id,
                path=detection.path,
                profiles=profiles,
            ),
            "capability_delta": "expansion",
            "profiles": profiles,
            "safe_next_action": "Review profiles, then run `rf repo add PATH` to enroll.",
        }
    )
    return 0


def _repo_remove(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    expected_source_sha256 = sha256_file(config_path)
    config = _load_minimal_or_error(config_path)
    previous_generation = lock_generation(resolved_config_path(config_path))
    remaining = tuple(
        repository for repository in config.repositories if repository.repo_id != args.repo_id
    )
    if len(remaining) == len(config.repositories):
        raise ConfigError(f"Unknown repository id: {args.repo_id}")
    if not remaining:
        raise ConfigError("Cannot remove the final repository; create a replacement config instead")
    updated = replace(config, repositories=remaining)
    lock_path, _ = write_user_and_lock(updated, expected_source_sha256=expected_source_sha256)
    _json(
        {
            "removed": args.repo_id,
            "config": str(config.source_path),
            "resolved_config": str(lock_path),
        }
        | _activate_managed_runtime(config_path, previous_generation, rollback_allowed=False)
    )
    return 0


def _repo_refresh(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    source_text = config_path.read_text(encoding="utf-8")
    expected_source_sha256 = sha256_text(source_text)
    config = _load_minimal_or_error(config_path)
    if sha256_file(config_path) != expected_source_sha256:
        raise ConfigError("Configuration changed while it was being read; retry refresh")
    lock_path = resolved_config_path(config.source_path)
    try:
        current_generation = lock_generation(lock_path)
    except ConfigError:
        current_generation = 0
    candidate, detections = build_lock_text(
        config, source_text, generation=max(1, current_generation)
    )
    current = lock_path.read_text(encoding="utf-8") if lock_path.is_file() else ""
    if current:
        try:
            delta = classify_capability_delta(current, candidate).kind.value
        except ConfigError:
            delta = "incompatible"
    else:
        delta = "expansion"
    if current == candidate:
        _json(
            {
                "changed": False,
                "capability_delta": "equivalent",
                "resolved_config": str(lock_path),
                "profiles": profile_summary(detections),
            }
            | _activation_result(config_path)
        )
        return 0
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile=str(lock_path),
            tofile=f"{lock_path} (proposed)",
        )
    )
    if not args.accept:
        _json(
            {
                "status": "pending_approval",
                "capability_delta": delta,
                "resolved_config": str(lock_path),
                "safe_next_action": "Review the diff, then re-run with `rf repo refresh --accept`.",
            }
        )
        print(diff, end="", file=sys.stderr)
        print(
            "\nNo changes written. Re-run with `rf repo refresh --accept` after review.",
            file=sys.stderr,
        )
        return 2
    if sha256_file(config_path) != expected_source_sha256:
        raise ConfigError("Configuration changed during refresh; no lock was written")
    candidate, _ = build_lock_text(config, source_text, generation=current_generation + 1)
    atomic_write(lock_path, candidate)
    snapshot = generation_snapshot_path(config.source_path, current_generation + 1)
    atomic_write(snapshot / "config.toml", source_text)
    atomic_write(snapshot / "resolved.toml", candidate)
    _json(
        {
            "changed": True,
            "accepted": True,
            "capability_delta": delta,
            "resolved_config": str(lock_path),
            "profiles": profile_summary(detections),
        }
        | _activate_managed_runtime(config_path, current_generation, rollback_allowed=True)
    )
    return 0


def _config_history(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    _json(
        {
            "generations": config_history(config_path),
            "current_generation": lock_generation(resolved_config_path(config_path)),
        }
    )
    return 0


def _config_rollback(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    lock_path = rollback_generation(config_path, args.generation)
    _json(
        {
            "status": "restart_required",
            "restored_generation": args.generation,
            "resolved_config": str(lock_path),
            "safe_next_action": "Restart the running `rf start` process to activate this generation.",
        }
        | _activation_result(config_path)
    )
    return 0


def _diagnostics_bundle(args: argparse.Namespace) -> int:
    """Write a bounded local diagnostic artifact without sensitive operational payloads."""
    config_path = Path(args.config).expanduser().resolve()
    lock_path = resolved_config_path(config_path)
    managed = read_managed_runtime(_managed_runtime_path(config_path))
    active = read_runtime_state(_runtime_state_path(config_path))
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else lock_path.parent
        / "diagnostics"
        / f"bundle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "path": str(config_path),
            "source_sha256": sha256_file(config_path),
            "resolved_config": str(lock_path),
            "resolved_sha256": sha256_file(lock_path) if lock_path.is_file() else None,
            "generations": config_history(config_path),
        },
        "runtime": {
            "managed": dataclasses.asdict(managed) if managed else None,
            "active": dataclasses.asdict(active) if active else None,
        },
        "exclusions": [
            "configuration file bodies",
            "file bodies and patches",
            "pull request bodies",
            "runtime logs",
            "process environment",
            "tunnel credentials",
        ],
    }
    atomic_write(output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _json(
        {
            "path": str(output),
            "included": ["config_fingerprints", "generations", "runtime_metadata"],
        }
    )
    return 0


def _repoforge_command(config_path: Path) -> list[str]:
    executable = shutil.which("repoforge") or shutil.which("rf")
    if executable:
        return [executable, "--config", str(config_path), "serve"]
    return [sys.executable, "-m", "repoforge", "--config", str(config_path), "serve"]


def _run_checked(argv: list[str], *, env: dict[str, str], timeout: int = 60) -> None:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise ConfigError(f"Command failed ({shlex.join(argv)}): {detail}")


def _start(args: argparse.Namespace, *, emit: bool = True) -> int:
    config_path = Path(args.config).expanduser().resolve()
    runtime_path = resolve_runtime_config_path(config_path)
    raw = read_toml(config_path)
    if config_kind(raw) == "minimal":
        user_config = load_user_config(config_path)
        tunnel_id = args.tunnel_id or user_config.tunnel.tunnel_id
        profile = args.profile or user_config.tunnel.profile
    else:
        tunnel_id = args.tunnel_id
        profile = args.profile or "repoforge"
        if not tunnel_id:
            raise ConfigError("Legacy config requires `rf start --tunnel-id tunnel_...`")

    service = CodingService(load_config(runtime_path))
    doctor: dict[str, Any] | None = None
    if not args.skip_doctor:
        doctor = service.doctor()
        if not doctor["ok"]:
            if emit:
                _json(
                    {"ok": False, "doctor": doctor, "message": "Fix doctor errors before starting."}
                )
            return 1

    tunnel_client = shutil.which("tunnel-client")
    if not tunnel_client and not args.dry_run:
        raise ConfigError("tunnel-client is not in PATH")
    tunnel_client = tunnel_client or "tunnel-client"
    mcp_argv = _repoforge_command(config_path)
    mcp_command = shlex.join(mcp_argv)
    init_argv = [
        tunnel_client,
        "init",
        "--sample",
        "sample_mcp_stdio_local",
        "--profile",
        profile,
        "--tunnel-id",
        tunnel_id,
        "--mcp-command",
        mcp_command,
    ]
    doctor_argv = [tunnel_client, "doctor", "--profile", profile, "--explain"]
    run_argv = [tunnel_client, "run", "--profile", profile]
    fingerprint = sha256_text(
        json.dumps(
            {
                "tunnel_id": tunnel_id,
                "profile": profile,
                "mcp_command": mcp_command,
                "runtime_config": str(runtime_path),
            },
            sort_keys=True,
        )
    )
    state_path = _tunnel_state_path(config_path)
    previous: dict[str, Any] = {}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            previous = {}

    if args.dry_run:
        _json(
            {
                "ok": True,
                "config": str(config_path),
                "resolved_config": str(runtime_path),
                "doctor": doctor["summary"] if doctor else None,
                "would_initialize": previous.get("fingerprint") != fingerprint,
                "init": init_argv,
                "doctor_command": doctor_argv,
                "run": run_argv,
            }
        )
        return 0

    environment = dict(os.environ)
    key = environment.get("CONTROL_PLANE_API_KEY")
    if not key:
        if not sys.stdin.isatty():
            raise ConfigError(
                "CONTROL_PLANE_API_KEY is not set and no interactive terminal is available"
            )
        try:
            key = getpass.getpass("Tunnel API key: ").strip()
        except EOFError as exc:
            raise ConfigError("Cannot read the tunnel API key from this terminal") from exc
        if not key:
            raise ConfigError("Tunnel API key cannot be empty")
        environment["CONTROL_PLANE_API_KEY"] = key

    if previous.get("fingerprint") != fingerprint:
        _run_checked(init_argv, env=environment)
        atomic_write(
            state_path,
            json.dumps(
                {"fingerprint": fingerprint, "updated_at": datetime.now(timezone.utc).isoformat()},
                indent=2,
            )
            + "\n",
        )
    try:
        _run_checked(doctor_argv, env=environment)
    except ConfigError:
        _run_checked(init_argv, env=environment)
        _run_checked(doctor_argv, env=environment)
        atomic_write(
            state_path,
            json.dumps(
                {"fingerprint": fingerprint, "updated_at": datetime.now(timezone.utc).isoformat()},
                indent=2,
            )
            + "\n",
        )
    if getattr(args, "managed", False):
        managed_path = _managed_runtime_path(config_path)
        with managed_start_claim(_managed_runtime_start_claim_path(config_path)):
            if read_managed_runtime(managed_path) is not None:
                raise ConfigError("ALREADY_RUNNING: run `rf runtime status` or `rf runtime stop`")
            log_path = _managed_runtime_log_path(config_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log_handle:
                process = subprocess.Popen(
                    run_argv,
                    env=environment,
                    start_new_session=True,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            managed = write_managed_runtime(
                managed_path,
                pid=process.pid,
                generation=lock_generation(runtime_path),
                profile=profile,
                executable=run_argv[0],
            )
        if emit:
            _json(
                {
                    "status": "starting",
                    "pid": managed.pid,
                    "config_generation": managed.active_generation,
                    "active_generation": managed.active_generation,
                    "restart_required": False,
                    "safe_next_action": "Run `rf runtime status` to observe tunnel health.",
                }
            )
        return 0
    os.execvpe(run_argv[0], run_argv, environment)
    raise AssertionError("os.execvpe returned unexpectedly")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rf", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="Configure repositories and the tunnel in one step")
    setup.add_argument("repos", nargs="+", help="Local Git repository paths")
    setup.add_argument("--tunnel-id", required=True)
    setup.add_argument("--profile", default="repoforge")
    setup.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--skip-smoke", action="store_true")

    start = subparsers.add_parser(
        "start", help="Validate configuration and start the secure tunnel"
    )
    start.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    start.add_argument("--tunnel-id", default=os.environ.get("TUNNEL_ID"))
    start.add_argument("--profile", default=os.environ.get("TUNNEL_PROFILE"))
    start.add_argument("--skip-doctor", action="store_true")
    start.add_argument("--dry-run", action="store_true")

    runtime = subparsers.add_parser("runtime", help="Inspect the managed runtime generation")
    runtime.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    runtime_subparsers = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_status = runtime_subparsers.add_parser("status")
    runtime_status.add_argument("--config", default=argparse.SUPPRESS)
    runtime_start = runtime_subparsers.add_parser("start")
    runtime_start.add_argument("--config", default=argparse.SUPPRESS)
    runtime_start.add_argument("--tunnel-id", default=os.environ.get("TUNNEL_ID"))
    runtime_start.add_argument("--profile", default=os.environ.get("TUNNEL_PROFILE"))
    runtime_start.add_argument("--skip-doctor", action="store_true")
    runtime_stop = runtime_subparsers.add_parser("stop")
    runtime_stop.add_argument("--config", default=argparse.SUPPRESS)
    runtime_logs = runtime_subparsers.add_parser("logs")
    runtime_logs.add_argument("--config", default=argparse.SUPPRESS)
    runtime_logs.add_argument("--tail", type=int, default=100)
    runtime_reload = runtime_subparsers.add_parser("reload")
    runtime_reload.add_argument("--config", default=argparse.SUPPRESS)
    runtime_reload.add_argument("--tunnel-id", default=os.environ.get("TUNNEL_ID"))
    runtime_reload.add_argument("--profile", default=os.environ.get("TUNNEL_PROFILE"))
    runtime_reload.add_argument("--skip-doctor", action="store_true")
    runtime_restart = runtime_subparsers.add_parser("restart")
    runtime_restart.add_argument("--config", default=argparse.SUPPRESS)
    runtime_restart.add_argument("--tunnel-id", default=os.environ.get("TUNNEL_ID"))
    runtime_restart.add_argument("--profile", default=os.environ.get("TUNNEL_PROFILE"))
    runtime_restart.add_argument("--skip-doctor", action="store_true")

    config = subparsers.add_parser("config", help="Inspect or restore reviewed config generations")
    config.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_history_parser = config_subparsers.add_parser("history")
    config_history_parser.add_argument("--config", default=argparse.SUPPRESS)
    config_rollback_parser = config_subparsers.add_parser("rollback")
    config_rollback_parser.add_argument("generation", type=int)
    config_rollback_parser.add_argument("--config", default=argparse.SUPPRESS)

    diagnostics = subparsers.add_parser(
        "diagnostics", help="Create a bounded local diagnostics bundle"
    )
    diagnostics.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    diagnostics_subparsers = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    diagnostics_bundle = diagnostics_subparsers.add_parser("bundle")
    diagnostics_bundle.add_argument("--config", default=argparse.SUPPRESS)
    diagnostics_bundle.add_argument("--output", default=None)

    repo = subparsers.add_parser("repo", help="Manage repositories in the minimal config")
    repo.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    repo_subparsers = repo.add_subparsers(dest="repo_command", required=True)
    repo_subparsers.add_parser("list")
    inspect = repo_subparsers.add_parser(
        "inspect", help="Preview a repository proposal without writes"
    )
    inspect.add_argument("path")
    inspect.add_argument("--repo-id", default=None)
    add = repo_subparsers.add_parser("add")
    add.add_argument("path")
    add.add_argument("--repo-id", default=None)
    add.add_argument("--preview", action="store_true")
    add.add_argument("--approve", default=None, metavar="PROPOSAL_ID")
    remove = repo_subparsers.add_parser("remove")
    remove.add_argument("repo_id")
    refresh = repo_subparsers.add_parser("refresh")
    refresh.add_argument("--accept", action="store_true")
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    commands = {"setup", "start", "repo", "runtime", "config", "diagnostics"}
    if len(argv) >= 3 and argv[0] == "--config" and argv[2] in commands:
        return [argv[2], "--config", argv[1], *argv[3:]]
    if len(argv) >= 2 and argv[0].startswith("--config=") and argv[1] in commands:
        return [argv[1], argv[0], *argv[2:]]
    return argv


def handle_onboarding_command(argv: list[str]) -> int | None:
    normalized = _normalize_argv(list(argv))
    if not normalized or normalized[0] not in {
        "setup",
        "start",
        "repo",
        "runtime",
        "config",
        "diagnostics",
    }:
        return None
    parser = _build_parser()
    try:
        args = parser.parse_args(normalized)
        if args.command == "setup":
            return _setup(args)
        if args.command == "start":
            return _start(args)
        if args.command == "repo":
            if args.repo_command == "list":
                return _repo_list(args)
            if args.repo_command == "add":
                return _repo_add(args)
            if args.repo_command == "inspect":
                return _repo_inspect(args)
            if args.repo_command == "remove":
                return _repo_remove(args)
            if args.repo_command == "refresh":
                return _repo_refresh(args)
        if args.command == "runtime" and args.runtime_command == "status":
            config_path = Path(args.config).expanduser().resolve()
            managed = read_managed_runtime(_managed_runtime_path(config_path))
            active = read_runtime_state(_runtime_state_path(config_path))
            result = _activation_result(config_path) | {
                "managed_pid": managed.pid if managed else None,
                "tool_surface_hash": active.tool_surface_hash if active else None,
                "health": _runtime_health(managed, active),
            }
            if managed is not None and result["status"] == "stopped":
                result["status"] = "starting"
                result["safe_next_action"] = (
                    "Wait for the MCP child to report its active generation."
                )
            _json(result)
            return 0
        if args.command == "runtime" and args.runtime_command == "start":
            args.managed = True
            args.dry_run = False
            return _start(args)
        if args.command == "runtime" and args.runtime_command == "stop":
            config_path = Path(args.config).expanduser().resolve()
            stopped = stop_managed_runtime(_managed_runtime_path(config_path))
            _json(
                {
                    "status": "stopped",
                    "pid": stopped.pid if stopped else None,
                    "what_happened": "Stopped managed runtime"
                    if stopped
                    else "No managed runtime was active",
                    "safe_next_action": "Run `rf runtime start` when ready.",
                }
            )
            return 0
        if args.command == "runtime" and args.runtime_command == "logs":
            config_path = Path(args.config).expanduser().resolve()
            lines = read_runtime_log(_managed_runtime_log_path(config_path), args.tail)
            _json({"path": str(_managed_runtime_log_path(config_path)), "lines": lines})
            return 0
        if args.command == "runtime" and args.runtime_command in {"reload", "restart"}:
            config_path = Path(args.config).expanduser().resolve()
            stop_managed_runtime(_managed_runtime_path(config_path))
            args.managed = True
            args.dry_run = False
            return _start(args)
        if args.command == "config" and args.config_command == "history":
            return _config_history(args)
        if args.command == "config" and args.config_command == "rollback":
            return _config_rollback(args)
        if args.command == "diagnostics" and args.diagnostics_command == "bundle":
            return _diagnostics_bundle(args)
        parser.error("Unknown onboarding command")
    except (PersonalCodingMCPError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2
