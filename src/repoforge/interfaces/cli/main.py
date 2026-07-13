"""Production CLI for configuration proposals, immutable generations, and managed runtime."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...application.configuration.document import (
    apply_proposal,
    parse_resolved,
    remove_repository,
    render_resolved,
)
from ...application.configuration.source import (
    SourceConfiguration,
    SourceRepository,
    add_source_repository,
    parse_source,
    remove_source_repository,
    render_source,
)
from ...application.diagnostics import build_diagnostics_bundle
from ...application.repository_admin.proposals import RepositoryProposalService
from ...application.runtime.activation import GenerationActivator
from ...application.runtime.hot_reload import (
    AtomicServiceRouter,
    GenerationServiceContainer,
    HotReloadCoordinator,
)
from ...application.service import CodingService
from ...bootstrap import (
    AdapterOverrides,
    build_application,
    build_configuration_store,
    build_lock_manager,
    build_metrics_sink,
    build_operation_gate,
    build_repository_probe,
    build_runtime_control_client,
    build_runtime_control_server,
    build_runtime_launcher,
    build_runtime_store,
    clear_runtime_state,
    default_state_root,
    id_generator,
    read_runtime_log,
    system_clock,
    write_private_file,
    write_runtime_state,
)
from ...config import DEFAULT_CONFIG_PATH, load_config
from ...domain.config_generation import (
    ApprovalEvent,
    CapabilityDeltaKind,
    ConfigGeneration,
    ConfigMutation,
    classify_capability_delta,
    sha256_text,
)
from ...domain.errors import (
    ConfigError,
    PersonalCodingMCPError,
    operation_error_from_exception,
)
from ...domain.redaction import redact_text
from ...domain.repository_proposal import EnrollmentMode, RepositoryProposal
from ...domain.runtime import ControlCommand, ControlRequest, RuntimePhase
from ...ports import ConfigurationStore, LockManager, RepositoryProbe
from ..runtime.host import McpRuntimeHost

_OUTPUT_FORMAT = "json"


def _human_lines(value: object, *, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            label = f"{prefix}{key}"
            if isinstance(item, (dict, list, tuple)):
                lines.append(f"{label}:")
                lines.extend(_human_lines(item, prefix=prefix + "  "))
            else:
                lines.append(f"{label}: {item}")
        return lines
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                lines.append(f"{prefix}-")
                lines.extend(_human_lines(item, prefix=prefix + "  "))
            else:
                lines.append(f"{prefix}- {item}")
        return lines
    return [f"{prefix}{value}"]


def _json(value: object) -> None:
    if _OUTPUT_FORMAT == "human":
        print("\n".join(_human_lines(value)))
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _error_code(exc: BaseException) -> str:
    value = getattr(exc, "code", None)
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value or "OPERATION_FAILED")


def _state_root() -> Path:
    return default_state_root()


def _locks() -> LockManager:
    return build_lock_manager(_state_root())


def _store(config_path: Path) -> ConfigurationStore:
    return build_configuration_store(config_path, state_root=_state_root(), locks=_locks())


def _probe() -> RepositoryProbe:
    return build_repository_probe(_state_root())


def _ensure_generation(config_path: Path) -> ConfigurationStore:
    store = _store(config_path)
    if store.current() is not None:
        return store
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")
    source_text = config_path.read_text(encoding="utf-8")
    legacy_resolved = (
        store.active_resolved_path.read_text(encoding="utf-8")
        if store.active_resolved_path.is_file()
        else source_text
    )
    store.import_legacy(source_text, legacy_resolved, created_at=system_clock().now_iso())
    return store


def _editable_source(store: ConfigurationStore) -> SourceConfiguration:
    try:
        return parse_source(store.read_source_text())
    except ValueError as exc:
        raise ConfigError(
            "Legacy resolved configuration is supported for serve/doctor/runtime, but repository "
            "mutation requires migration. Run `rf setup --force ...` after reviewing proposals."
        ) from exc


def _source_for_display(store: ConfigurationStore) -> SourceConfiguration:
    try:
        return parse_source(store.read_source_text())
    except ValueError as exc:
        current = store.current()
        if current is None:
            raise ConfigError("No accepted configuration generation") from exc
        config = load_config(store.resolved_path(current.generation))
        return SourceConfiguration(
            os.environ.get("REPOFORGE_TUNNEL_ID", "legacy-unconfigured"),
            os.environ.get("REPOFORGE_TUNNEL_PROFILE", "repoforge"),
            tuple(
                SourceRepository(repo.repo_id, str(repo.path))
                for repo in sorted(config.repositories.values(), key=lambda item: item.repo_id)
            ),
        )


def _runtime_environment(args: argparse.Namespace) -> dict[str, str]:
    environment: dict[str, str] = {}
    tunnel_id = getattr(args, "tunnel_id", None)
    profile = getattr(args, "profile", None)
    if tunnel_id:
        environment["REPOFORGE_TUNNEL_ID"] = str(tunnel_id)
    if profile:
        environment["REPOFORGE_TUNNEL_PROFILE"] = str(profile)
    return environment


def _parse_decisions(values: list[str]) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for value in values:
        key, separator, selected = value.partition("=")
        if not separator or not key or not selected:
            raise ValueError("--decision must use CODE=CHOICE or REPO_ID.CODE=CHOICE")
        decisions[key] = selected
    return decisions


def _decisions_for_repo(decisions: dict[str, str], repo_id: str) -> dict[str, str]:
    """Return global decisions plus repository-scoped overrides for multi-repo setup."""
    selected = {key: value for key, value in decisions.items() if "." not in key}
    prefix = f"{repo_id}."
    selected.update(
        {
            key.removeprefix(prefix): value
            for key, value in decisions.items()
            if key.startswith(prefix)
        }
    )
    return selected


def _parse_overrides(values: list[str]) -> dict[str, str]:
    return _parse_decisions(values)


def _overrides_for_repo(overrides: dict[str, str], repo_id: str) -> dict[str, str]:
    return _decisions_for_repo(overrides, repo_id)


def _proposal_data(proposal: RepositoryProposal) -> dict[str, Any]:
    return asdict(proposal)


def _smoke_resolved(resolved_text: str, repo_id: str, state_root: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="repoforge-proposal-smoke-") as directory:
        resolved = Path(directory) / "resolved.toml"
        resolved.write_text(resolved_text, encoding="utf-8")
        config = load_config(resolved)
        # Isolate registry/workspaces from the production roots during proposal smoke.
        server = config.server
        isolated = type(server)(
            workspace_root=Path(directory) / "workspaces",
            state_root=Path(directory) / "state",
            max_file_bytes=server.max_file_bytes,
            max_tool_output_chars=server.max_tool_output_chars,
            default_command_timeout_seconds=server.default_command_timeout_seconds,
            verification_timeout_seconds=server.verification_timeout_seconds,
            max_fingerprint_bytes=server.max_fingerprint_bytes,
            max_batch_files=server.max_batch_files,
            path_prefixes=server.path_prefixes,
            allowed_environment=server.allowed_environment,
        )
        config = type(config)(config.source_path, isolated, config.repositories)
        service = CodingService(config)
        workspace = service.workspace_create(repo_id, "proposal-smoke")
        workspace_id = str(workspace["workspace_id"])
        try:
            service.repo_status(repo_id)
            service.repo_context(repo_id)
            service.workspace_status(workspace_id)
            service.workspace_tree(workspace_id, 50)
            service.workspace_diff(workspace_id)
        finally:
            service.workspace_remove(workspace_id, delete_local_branch=True)
    return {"ok": True, "repo_id": repo_id, "state_root": str(state_root)}


def _render_candidate(
    store: ConfigurationStore,
    source_text: str,
    document: dict[str, Any],
    *,
    reason: str,
    proposal_id: str | None,
    fingerprints: tuple[tuple[str, str], ...],
) -> str:
    current = store.current()
    return render_resolved(
        document,
        generation=(current.generation if current else 0) + 1,
        source_path=str(store.source_path),
        source_sha256=sha256_text(source_text),
        created_at=system_clock().now_iso(),
        reason=reason,
        proposal_id=proposal_id,
        repository_fingerprints=fingerprints,
    )


def _combined_proposal_id(proposals: list[RepositoryProposal]) -> str:
    payload = "\n".join(sorted(item.proposal_id for item in proposals))
    return hashlib.sha256(payload.encode()).hexdigest()


def _approval_map(values: list[str]) -> set[str]:
    return {value for value in values if value}


def _setup(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if config_path.exists() and not args.force:
        raise ConfigError(f"Configuration already exists: {config_path}; use repo enroll/refresh")
    proposal_service = RepositoryProposalService(_probe())
    decisions = _parse_decisions(args.decision)
    overrides = _parse_overrides(args.policy_override)
    inspected = [proposal_service.inspect(Path(path)) for path in args.repos]
    proposals = [
        proposal_service.propose(
            Path(path),
            repo_id=str(facts["repo_id"]),
            decisions=_decisions_for_repo(decisions, str(facts["repo_id"])),
            template=EnrollmentMode(args.template),
            overrides=_overrides_for_repo(overrides, str(facts["repo_id"])),
        )
        for path, facts in zip(args.repos, inspected, strict=True)
    ]
    required = [
        {"repo_id": proposal.repo_id, **asdict(decision)}
        for proposal in proposals
        for decision in proposal.required_decisions
    ]
    required_tokens = {f"approve:{proposal.proposal_id}" for proposal in proposals}
    supplied = _approval_map(args.approve)
    missing_tokens = sorted(required_tokens - supplied)
    blocked = [proposal.repo_id for proposal in proposals if proposal.confidence.value == "blocked"]
    if required or missing_tokens or blocked:
        _json(
            {
                "status": "input_required" if required else "pending_approval",
                "required_decisions": required,
                "required_approval_tokens": missing_tokens,
                "blocked_repositories": blocked,
                "proposals": [_proposal_data(item) for item in proposals],
                "unchanged_state": ["configuration", "runtime"],
                "safe_next_action": "Resolve every decision and supply every exact approval token.",
            }
        )
        return 3
    source = SourceConfiguration(
        args.tunnel_id,
        args.profile,
        tuple(
            SourceRepository(
                item.repo_id,
                item.path,
                item.proposal_id,
                args.template,
                tuple(sorted(_decisions_for_repo(decisions, item.repo_id).items())),
                tuple(sorted(_overrides_for_repo(overrides, item.repo_id).items())),
            )
            for item in sorted(proposals, key=lambda proposal: proposal.repo_id)
        ),
    )
    source_text = render_source(source)
    store = _store(config_path)
    previous = store.current()
    expected_source_sha = (
        sha256_text(config_path.read_text(encoding="utf-8")) if config_path.is_file() else None
    )
    if config_path.exists() and args.force:
        backup = config_path.with_suffix(config_path.suffix + f".backup-{int(time.time())}")
        backup.write_bytes(config_path.read_bytes())
        backup.chmod(0o600)
    document = parse_resolved(None)
    for proposal in proposals:
        document = apply_proposal(document, proposal)
    fingerprints = tuple(sorted((item.repo_id, item.facts_fingerprint) for item in proposals))
    proposal_id = _combined_proposal_id(proposals)
    candidate = _render_candidate(
        store,
        source_text,
        document,
        reason="initial approved setup",
        proposal_id=proposal_id,
        fingerprints=fingerprints,
    )
    smoke = [_smoke_resolved(candidate, item.repo_id, _state_root()) for item in proposals]
    now = system_clock().now_iso()
    generation = store.accept(
        ConfigMutation(
            source_text,
            candidate,
            fingerprints,
            "initial approved setup",
            now,
            previous.generation if previous else 0,
            expected_source_sha,
            proposal_id,
            ApprovalEvent(
                os.environ.get("USER", "local-user"),
                now,
                proposal_id,
                sha256_text("\n".join(sorted(supplied))),
            ),
            correlation_id=id_generator().new_hex(24),
        )
    )
    activation = _activate(
        store,
        config_path,
        generation,
        mode=args.activate,
        wait=args.wait,
        rollback_on_failure=args.rollback_on_failure,
    )
    _json(
        {
            "status": "configured",
            "config": str(config_path),
            "generation": asdict(generation),
            "smoke": smoke,
            **activation,
        }
    )
    return 0


def _repo_refresh(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    store = _ensure_generation(config_path)
    current = store.current()
    if current is None:
        raise ConfigError("No accepted generation")
    source = _editable_source(store)
    selected = [
        item for item in source.repositories if args.repo_id is None or item.repo_id == args.repo_id
    ]
    if not selected:
        raise ConfigError(f"Unknown repository id: {args.repo_id}")
    proposal_service = RepositoryProposalService(_probe())
    decisions = _parse_decisions(args.decision)
    overrides = _parse_overrides(args.policy_override)
    effective_inputs: dict[str, tuple[dict[str, str], dict[str, str], str]] = {}
    proposals: list[RepositoryProposal] = []
    for item in selected:
        item_decisions = dict(item.decisions)
        item_decisions.update(_decisions_for_repo(decisions, item.repo_id))
        item_overrides = dict(item.policy_overrides)
        item_overrides.update(_overrides_for_repo(overrides, item.repo_id))
        item_template = args.template or item.policy_template
        effective_inputs[item.repo_id] = (item_decisions, item_overrides, item_template)
        proposals.append(
            proposal_service.propose(
                Path(item.path),
                repo_id=item.repo_id,
                decisions=item_decisions,
                template=EnrollmentMode(item_template),
                overrides=item_overrides,
            )
        )
    required = [
        {"repo_id": proposal.repo_id, **asdict(decision)}
        for proposal in proposals
        for decision in proposal.required_decisions
    ]
    supplied = _approval_map(args.approve)
    previous_proposals = {item.repo_id: item.proposal_id for item in selected}
    required_tokens = {
        f"approve:{proposal.proposal_id}"
        for proposal in proposals
        if proposal.proposal_id != previous_proposals.get(proposal.repo_id)
    }
    missing = sorted(required_tokens - supplied)
    if required or (args.accept and missing):
        _json(
            {
                "status": "input_required" if required else "pending_approval",
                "required_decisions": required,
                "required_approval_tokens": missing,
                "proposals": [_proposal_data(item) for item in proposals],
                "unchanged_state": ["configuration", "runtime"],
                "safe_next_action": (
                    "Resolve the required decisions, then preview again."
                    if required
                    else "Re-run with the exact approval tokens shown after reviewing the preview."
                ),
            }
        )
        return 3
    proposal_by_id = {item.repo_id: item for item in proposals}
    updated_source = SourceConfiguration(
        source.tunnel_id,
        source.profile,
        tuple(
            SourceRepository(
                item.repo_id,
                item.path,
                proposal_by_id[item.repo_id].proposal_id,
                effective_inputs[item.repo_id][2],
                tuple(sorted(effective_inputs[item.repo_id][0].items())),
                tuple(sorted(effective_inputs[item.repo_id][1].items())),
            )
            if item.repo_id in proposal_by_id
            else item
            for item in source.repositories
        ),
    )
    source_text = render_source(updated_source)
    document = parse_resolved(store.read_resolved_text())
    fingerprint_map = current.repository_fingerprint_map()
    for proposal in proposals:
        document = apply_proposal(document, proposal)
        fingerprint_map[proposal.repo_id] = proposal.facts_fingerprint
    fingerprints = tuple(sorted(fingerprint_map.items()))
    proposal_id = _combined_proposal_id(proposals)
    candidate = _render_candidate(
        store,
        source_text,
        document,
        reason="refresh approved repository facts and policy",
        proposal_id=proposal_id,
        fingerprints=fingerprints,
    )
    delta = classify_capability_delta(store.read_resolved_text(), candidate)
    if not args.accept:
        _json(
            {
                "status": "preview",
                "capability_delta": delta.kind.value,
                "changes": [asdict(item) for item in delta.changes],
                "proposals": [_proposal_data(item) for item in proposals],
                "required_approval_tokens": missing,
                "source_sha256": sha256_text(source_text),
                "resolved_sha256": sha256_text(candidate),
                "unchanged_state": ["configuration", "active runtime"],
                "safe_next_action": "Review this preview, then re-run with --accept and any required approval tokens.",
            }
        )
        return 0
    for proposal in proposals:
        _smoke_resolved(candidate, proposal.repo_id, _state_root())
    now = system_clock().now_iso()
    generation = store.accept(
        ConfigMutation(
            source_text,
            candidate,
            fingerprints,
            "refresh approved repository facts and policy",
            now,
            current.generation,
            sha256_text(store.read_source_text()),
            proposal_id,
            ApprovalEvent(
                os.environ.get("USER", "local-user"),
                now,
                proposal_id,
                sha256_text("\n".join(sorted(supplied))),
            ),
            correlation_id=id_generator().new_hex(24),
        )
    )
    changed = generation.generation != current.generation
    _json(
        {
            "status": "accepted" if changed else "unchanged",
            "changed": changed,
            "generation": asdict(generation),
            **(
                _activate(
                    store,
                    config_path,
                    generation,
                    mode=args.activate,
                    wait=args.wait,
                    rollback_on_failure=args.rollback_on_failure,
                )
                if changed
                else _activation_result(store, generation.generation)
            ),
        }
    )
    return 0


def _runtime_paths(store: ConfigurationStore) -> tuple[Path, Path, Path]:
    return (
        store.root / "managed-runtime-v3.json",
        store.root / "supervisor.sock",
        store.root / "mcp.sock",
    )


def _activation_result(store: ConfigurationStore, generation: int) -> dict[str, object]:
    runtime_path, _, _ = _runtime_paths(store)
    record = build_runtime_store(runtime_path).read()
    return {
        "config_generation": generation,
        "active_generation": record.active_generation if record else None,
        "restart_required": record is None or record.active_generation != generation,
        "runtime_state": record.phase.value if record else "stopped",
    }


def _activate(
    store: ConfigurationStore,
    config_path: Path,
    generation: ConfigGeneration,
    *,
    mode: str,
    wait: bool = True,
    rollback_on_failure: bool = True,
) -> dict[str, object]:
    if mode not in {"auto", "always", "never"}:
        raise ValueError(f"Unsupported activation mode: {mode}")
    if not wait and rollback_on_failure:
        raise ValueError(
            "--no-wait requires --no-rollback-on-failure because automatic rollback "
            "cannot be guaranteed after the command returns"
        )
    runtime_path, supervisor_socket, mcp_socket = _runtime_paths(store)
    runtime_store = build_runtime_store(runtime_path)
    running = runtime_store.read()
    managed = running is not None and running.phase not in {
        RuntimePhase.STOPPED,
        RuntimePhase.FAILED,
    }
    if mode == "never" or (mode == "auto" and not managed):
        active = store.active()
        return {
            "status": "restart_required" if active else "stopped",
            "config_generation": generation.generation,
            "active_generation": active.generation if active else None,
            "restart_required": active is None or active.generation != generation.generation,
            "safe_next_action": (
                f"Run `rf --config {config_path} runtime start` to activate generation "
                f"{generation.generation}."
            ),
        }
    activator = GenerationActivator(
        configs=store,
        runtime=runtime_store,
        mcp_control=build_runtime_control_client(mcp_socket),
        supervisor_control=build_runtime_control_client(supervisor_socket),
        launcher=build_runtime_launcher(),
        ids=id_generator(),
        clock=system_clock(),
        config_path=config_path,
    )
    return asdict(
        activator.activate(
            generation,
            extra_env={},
            wait_for_health=wait,
            rollback_on_failure=rollback_on_failure,
        )
    )


def _repo_inspect(args: argparse.Namespace) -> int:
    service = RepositoryProposalService(_probe())
    _json(
        {
            "status": "inspected",
            "facts": service.inspect(Path(args.path), repo_id=args.repo_id),
            "unchanged_state": ["configuration", "runtime"],
        }
    )
    return 0


def _repo_propose(args: argparse.Namespace) -> int:
    service = RepositoryProposalService(_probe())
    proposal = service.propose(
        Path(args.path),
        repo_id=args.repo_id,
        decisions=_parse_decisions(args.decision),
        template=EnrollmentMode(args.template),
        overrides=_parse_overrides(args.policy_override),
    )
    result = _proposal_data(proposal)
    result.update(
        {
            "status": (
                "blocked"
                if proposal.confidence.value == "blocked"
                else "input_required"
                if proposal.required_decisions
                else "pending_approval"
            ),
            "approval_token": f"approve:{proposal.proposal_id}"
            if not proposal.required_decisions and proposal.confidence.value != "blocked"
            else None,
            "unchanged_state": ["configuration", "runtime"],
            "safe_next_action": (
                "Resolve the blocking repository feature or choose a safe non-blocking policy."
                if proposal.confidence.value == "blocked"
                else "Answer required decisions and propose again."
                if proposal.required_decisions
                else "Review the proposal and enroll with its exact approval token."
            ),
        }
    )
    _json(result)
    return (
        3
        if args.non_interactive
        and (proposal.required_decisions or proposal.confidence.value == "blocked")
        else 0
    )


def _repo_enroll(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    store = _ensure_generation(config_path)
    proposal_service = RepositoryProposalService(_probe())
    proposal = proposal_service.propose(
        Path(args.path),
        repo_id=args.repo_id,
        decisions=_parse_decisions(args.decision),
        template=EnrollmentMode(args.template),
        overrides=_parse_overrides(args.policy_override),
    )
    if proposal.required_decisions:
        _json(
            {
                "status": "input_required",
                "required_decisions": [asdict(item) for item in proposal.required_decisions],
                "proposal_id": proposal.proposal_id,
                "unchanged_state": ["configuration", "runtime"],
            }
        )
        return 3
    if proposal.confidence.value == "blocked":
        raise ConfigError("Repository proposal is blocked by safety findings")
    approval_hash = proposal_service.verify_approval(proposal, args.approve)
    current = store.current()
    source = _editable_source(store)
    source = add_source_repository(
        source,
        SourceRepository(
            proposal.repo_id,
            proposal.path,
            proposal.proposal_id,
            args.template,
            tuple(sorted(_parse_decisions(args.decision).items())),
            tuple(sorted(_parse_overrides(args.policy_override).items())),
        ),
    )
    source_text = render_source(source)
    document = apply_proposal(parse_resolved(store.read_resolved_text()), proposal)
    fingerprints = (
        tuple(
            sorted(
                (*current.repository_fingerprints, (proposal.repo_id, proposal.facts_fingerprint))
            )
        )
        if current
        else ((proposal.repo_id, proposal.facts_fingerprint),)
    )
    candidate = _render_candidate(
        store,
        source_text,
        document,
        reason=f"enroll repository {proposal.repo_id}",
        proposal_id=proposal.proposal_id,
        fingerprints=fingerprints,
    )
    smoke = _smoke_resolved(candidate, proposal.repo_id, _state_root())
    now = system_clock().now_iso()
    generation = store.accept(
        ConfigMutation(
            source_text,
            candidate,
            fingerprints,
            f"enroll repository {proposal.repo_id}",
            now,
            current.generation if current else 0,
            sha256_text(store.read_source_text()),
            proposal.proposal_id,
            ApprovalEvent(
                os.environ.get("USER", "local-user"), now, proposal.proposal_id, approval_hash
            ),
        )
    )
    _json(
        {
            "status": "accepted",
            "proposal_id": proposal.proposal_id,
            "smoke_test": smoke,
            **asdict(generation),
            **_activate(
                store,
                config_path,
                generation,
                mode=args.activate,
                wait=args.wait,
                rollback_on_failure=args.rollback_on_failure,
            ),
        }
    )
    return 0


def _repo_remove(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    store = _ensure_generation(config_path)
    current = store.current()
    if current is None:
        raise ConfigError("No accepted generation")
    source = remove_source_repository(parse_source(store.read_source_text()), args.repo_id)
    source_text = render_source(source)
    document = remove_repository(parse_resolved(store.read_resolved_text()), args.repo_id)
    fingerprints = tuple(
        item for item in current.repository_fingerprints if item[0] != args.repo_id
    )
    candidate = _render_candidate(
        store,
        source_text,
        document,
        reason=f"remove repository {args.repo_id}",
        proposal_id=None,
        fingerprints=fingerprints,
    )
    generation = store.accept(
        ConfigMutation(
            source_text,
            candidate,
            fingerprints,
            f"remove repository {args.repo_id}",
            system_clock().now_iso(),
            current.generation,
            current.source_sha256,
            correlation_id=id_generator().new_hex(24),
        )
    )
    _json(
        {
            "status": "accepted_restriction",
            "removed": args.repo_id,
            **asdict(generation),
            **_activate(store, config_path, generation, mode="auto"),
        }
    )
    return 0


def _tool_surface_rediscovery(record_hash: str | None) -> dict[str, object]:
    try:
        from ..mcp.server import tool_surface_hash

        current_hash: str | None = tool_surface_hash()
    except Exception:
        current_hash = None
    changed = bool(record_hash and current_hash and record_hash != current_hash)
    return {
        "current_tool_surface_hash": current_hash,
        "plugin_rediscovery_recommended": changed,
        "plugin_rediscovery_reason": (
            f"MCP tool surface changed from {record_hash} to {current_hash}" if changed else None
        ),
    }


def _runtime_status(store: ConfigurationStore) -> dict[str, object]:
    runtime_path, _, _ = _runtime_paths(store)
    record = build_runtime_store(runtime_path).read()
    accepted = store.current()
    active = store.active()
    activation_target = store.activation_target()
    return {
        "state": record.phase.value if record else "stopped",
        "pid": record.pid if record else None,
        "child_pid": record.child_pid if record else None,
        "accepted_generation": accepted.generation if accepted else None,
        "disk_active_generation": active.generation if active else None,
        "activation_target_generation": activation_target.generation if activation_target else None,
        "active_generation": record.active_generation if record else None,
        "restart_required": bool(
            accepted and (not record or record.active_generation != accepted.generation)
        ),
        "health": list(record.health) if record else [],
        "tunnel_profile": record.tunnel_profile if record else None,
        "tunnel_profile_fingerprint": record.tunnel_profile_fingerprint if record else None,
        "tool_surface_hash": record.tool_surface_hash if record else None,
        **_tool_surface_rediscovery(record.tool_surface_hash if record else None),
        "restart_count": record.restart_count if record else 0,
        "last_error_code": record.last_error_code if record else None,
        "last_error": record.last_error if record else None,
        "correlation_id": record.correlation_id if record else None,
        "safe_next_action": (
            "Runtime is healthy."
            if record and record.phase is RuntimePhase.HEALTHY
            else "Run `rf runtime reload` after correcting the reported failure."
            if record and record.phase in {RuntimePhase.FAILED, RuntimePhase.FAIL_CLOSED}
            else "Run `rf runtime start` or inspect logs."
        ),
    }


def _runtime_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    store = _ensure_generation(config_path)
    runtime_path, supervisor_socket, mcp_socket = _runtime_paths(store)
    if args.runtime_command == "status":
        _json(_runtime_status(store))
        return 0
    if args.runtime_command == "logs":
        _json(
            {
                "path": str(store.root / "managed-runtime.log"),
                "lines": read_runtime_log(store.root / "managed-runtime.log", args.tail),
            }
        )
        return 0
    if args.runtime_command == "stop":
        runtime_store = build_runtime_store(runtime_path)
        record = runtime_store.read()
        stopped = False
        forced = False
        with contextlib.suppress(ConfigError):
            stopped = (
                build_runtime_control_client(supervisor_socket)
                .request(ControlRequest(1, ControlCommand.SHUTDOWN, id_generator().new_hex(24)))
                .ok
            )
        if (
            not stopped
            and record is not None
            and record.phase
            not in {
                RuntimePhase.STOPPED,
                RuntimePhase.FAILED,
            }
        ):
            forced = build_runtime_launcher().force_stop(record, grace_seconds=5.0)
            stopped = forced
        if stopped:
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                current = runtime_store.read()
                if current is None or current.phase in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
                    break
                time.sleep(0.1)
            else:
                raise ConfigError("RUNTIME_STOP_TIMEOUT: supervisor did not stop within 20 seconds")
        _json(
            {
                "status": "stopped" if stopped else "not_running",
                "what_happened": (
                    "Managed runtime was identity-validated and force-stopped"
                    if forced
                    else "Managed runtime stopped"
                    if stopped
                    else "No live managed runtime was found"
                ),
                "forced": forced,
                "safe_next_action": "Run `rf runtime start` when ready.",
            }
        )
        return 0
    if args.runtime_command == "start":
        previous_active = store.active()
        active = previous_active or store.current()
        if active is None:
            raise ConfigError("No accepted configuration generation")
        launcher = build_runtime_launcher()
        runtime_environment = _runtime_environment(args)
        with _locks().lock(
            "runtime-start-claim",
            timeout_seconds=0,
            metadata={"operation": "runtime-start"},
        ):
            current_record = build_runtime_store(runtime_path).read()
            if current_record and current_record.phase not in {
                RuntimePhase.STOPPED,
                RuntimePhase.FAILED,
            }:
                raise ConfigError(
                    f"ALREADY_RUNNING: managed runtime pid={current_record.pid} "
                    f"state={current_record.phase.value}"
                )
            store.stage_activation(
                active.generation,
                expected_active=previous_active.generation if previous_active else None,
            )
            if args.foreground:
                return launcher.start(config_path, foreground=True, extra_env=runtime_environment)
            pid = launcher.start(config_path, foreground=False, extra_env=runtime_environment)
            deadline = time.monotonic() + 5.0
            observed = None
            while time.monotonic() < deadline:
                observed = build_runtime_store(runtime_path).read()
                if observed is not None:
                    break
                try:
                    os.kill(pid, 0)
                except ProcessLookupError as exc:
                    raise ConfigError(
                        "RUNTIME_START_FAILED: supervisor worker exited before publishing state"
                    ) from exc
                time.sleep(0.05)
            if observed is None:
                raise ConfigError("RUNTIME_START_TIMEOUT: supervisor did not publish startup state")
        _json(
            {
                "status": observed.phase.value,
                "pid": observed.pid or pid,
                "config_generation": active.generation,
                "active_generation": observed.active_generation,
                "correlation_id": observed.correlation_id,
                "safe_next_action": "Run `rf runtime status` to observe health.",
            }
        )
        return 0
    if args.runtime_command in {"reload", "restart"}:
        target = store.current()
        if target is None:
            raise ConfigError("No accepted configuration generation")
        if args.runtime_command == "restart":
            active_target = store.active()
            if active_target is not None:
                target = active_target
        activator = GenerationActivator(
            configs=store,
            runtime=build_runtime_store(runtime_path),
            mcp_control=build_runtime_control_client(mcp_socket),
            supervisor_control=build_runtime_control_client(supervisor_socket),
            launcher=build_runtime_launcher(),
            ids=id_generator(),
            clock=system_clock(),
            config_path=config_path,
        )
        _json(asdict(activator.activate(target, extra_env=_runtime_environment(args))))
        return 0
    raise ConfigError(f"Unknown runtime command: {args.runtime_command}")


def _serve(config_path: Path) -> int:
    from ..mcp.server import create_server, tool_surface_hash

    store = _ensure_generation(config_path)
    initial_generation = store.activation_target() or store.active()
    if initial_generation is None:
        raise ConfigError("No staged or active configuration generation")

    def build_container(
        generation: int, *, allow_incompatible: bool = False
    ) -> GenerationServiceContainer:
        candidates = (store.activation_target(), store.active())
        selected = next(
            (item for item in candidates if item is not None and item.generation == generation),
            None,
        )
        if selected is None:
            selected = next(
                (item for item in store.history() if item.generation == generation),
                None,
            )
        if selected is None:
            raise ConfigError(f"Unknown configuration generation: {generation}")
        if (
            getattr(selected, "delta", CapabilityDeltaKind.EQUIVALENT)
            is CapabilityDeltaKind.INCOMPATIBLE
            and not allow_incompatible
        ):
            raise ConfigError(
                "HOT_RELOAD_RESTART_REQUIRED: incompatible generation requires supervisor restart"
            )
        config = load_config(store.resolved_path(generation))
        gate = build_operation_gate()
        app = build_application(config, overrides=AdapterOverrides(gate=gate))
        service = CodingService(config, application=app)

        def dispose() -> None:
            gate.fail_closed(
                reason=f"generation {generation} retired",
                correlation_id=f"retired-{generation}",
            )

        repositories = service.repo_list().get("repositories", [])
        repository_ids = frozenset(
            str(item["repo_id"])
            for item in repositories
            if isinstance(item, dict) and "repo_id" in item
        )
        return GenerationServiceContainer(
            generation=generation,
            service=service,
            gate=gate,
            repository_ids=repository_ids,
            dispose=dispose,
        )

    initial = build_container(initial_generation.generation, allow_incompatible=True)
    # The initial process startup performs the same self-check as a hot-reload candidate.
    initial.service.repo_list()
    router = AtomicServiceRouter(initial)
    reloader = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: build_container(generation),
        commit_activation=lambda generation, expected: store.activate(
            generation, expected_active=expected
        ),
    )
    _, _, mcp_socket = _runtime_paths(store)
    runtime_state_path = store.root / "runtime.json"
    state_holder: dict[str, object] = {}

    def record_activation(generation: int) -> None:
        state_holder["state"] = write_runtime_state(
            runtime_state_path, generation, tool_surface_hash()
        )

    host = McpRuntimeHost(
        router=router,
        reloader=reloader,
        on_activated=record_activation,
    )
    control = build_runtime_control_server(mcp_socket)
    control.start(host.handle)
    state = write_runtime_state(
        runtime_state_path, initial_generation.generation, tool_surface_hash()
    )
    state_holder["state"] = state
    try:
        create_server(router=router).run(transport="stdio")
    finally:
        router.close(timeout_seconds=30.0)
        control.close()
        latest_state = state_holder.get("state", state)
        clear_runtime_state(runtime_state_path, int(getattr(latest_state, "pid", state.pid)))
    return 0


def _normalize_global_config(argv: list[str]) -> list[str]:
    """Preserve legacy ``rf COMMAND --config PATH`` invocation order.

    ``argparse`` normally requires parent-parser options before a subcommand. Existing scripts and
    operator muscle memory use both positions, so move only the unambiguous global config option.
    Diagnostics has its own ``--output`` option and is intentionally not rewritten here.
    """
    selected: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--config":
            if index + 1 >= len(argv):
                remaining.append(value)
                index += 1
                continue
            selected = argv[index + 1]
            index += 2
            continue
        if value.startswith("--config="):
            selected = value.split("=", 1)[1]
            index += 1
            continue
        remaining.append(value)
        index += 1
    return (["--config", selected] if selected is not None else []) + remaining


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rf", description="RepoForge production repository and runtime control"
    )
    parser.add_argument(
        "--config", default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    parser.add_argument(
        "--output", choices=("json", "human"), default=os.environ.get("REPOFORGE_OUTPUT", "json")
    )
    parser.add_argument("--version", action="version", version="RepoForge 2.0.0")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    start_alias = commands.add_parser(
        "start", help="Foreground compatibility alias for runtime start"
    )
    start_alias.add_argument("--background", action="store_true")
    start_alias.add_argument("--tunnel-id")
    start_alias.add_argument("--profile")
    setup = commands.add_parser("setup")
    setup.add_argument("repos", nargs="+")
    setup.add_argument("--tunnel-id", required=True)
    setup.add_argument("--profile", default="repoforge")
    setup.add_argument("--decision", action="append", default=[])
    setup.add_argument("--policy-override", action="append", default=[])
    setup.add_argument("--approve", action="append", default=[])
    setup.add_argument(
        "--template", choices=[item.value for item in EnrollmentMode], default="standard"
    )
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--activate", choices=["auto", "always", "never"], default="auto")
    setup.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    setup.add_argument("--rollback-on-failure", action=argparse.BooleanOptionalAction, default=True)
    repo = commands.add_parser("repo")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    for name in ("inspect", "propose", "enroll", "add"):
        item = repo_sub.add_parser(name)
        item.add_argument("path")
        item.add_argument("--repo-id")
        item.add_argument("--decision", action="append", default=[])
        item.add_argument("--policy-override", action="append", default=[])
        item.add_argument(
            "--template", choices=[item.value for item in EnrollmentMode], default="standard"
        )
        item.add_argument("--non-interactive", action="store_true")
        if name in {"enroll", "add"}:
            item.add_argument("--approve")
            item.add_argument("--activate", choices=["auto", "always", "never"], default="auto")
            item.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
            item.add_argument(
                "--rollback-on-failure", action=argparse.BooleanOptionalAction, default=True
            )
    remove = repo_sub.add_parser("remove")
    remove.add_argument("repo_id")
    refresh = repo_sub.add_parser("refresh")
    refresh.add_argument("repo_id", nargs="?")
    refresh.add_argument("--decision", action="append", default=[])
    refresh.add_argument("--policy-override", action="append", default=[])
    refresh.add_argument("--approve", action="append", default=[])
    refresh.add_argument("--accept", action="store_true")
    refresh.add_argument(
        "--template", choices=[item.value for item in EnrollmentMode], default=None
    )
    refresh.add_argument("--activate", choices=["auto", "always", "never"], default="auto")
    refresh.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    refresh.add_argument(
        "--rollback-on-failure", action=argparse.BooleanOptionalAction, default=True
    )
    repo_sub.add_parser("list")
    runtime = commands.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_sub.add_parser("status")
    start = runtime_sub.add_parser("start")
    start.add_argument("--foreground", action="store_true")
    start.add_argument("--tunnel-id")
    start.add_argument("--profile")
    runtime_sub.add_parser("stop")
    reload_command = runtime_sub.add_parser("reload")
    reload_command.add_argument("--tunnel-id")
    reload_command.add_argument("--profile")
    restart_command = runtime_sub.add_parser("restart")
    restart_command.add_argument("--tunnel-id")
    restart_command.add_argument("--profile")
    logs = runtime_sub.add_parser("logs")
    logs.add_argument("--tail", type=int, default=100)
    config = commands.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("history")
    rollback = config_sub.add_parser("rollback")
    rollback.add_argument("generation", type=int)
    rollback.add_argument("--approve")
    commands.add_parser("show-config")
    commands.add_parser("doctor")
    commands.add_parser("list-workspaces")
    diagnostics = commands.add_parser("diagnostics")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    bundle = diagnostics_sub.add_parser("bundle")
    bundle.add_argument("--output", dest="bundle_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    normalized = _normalize_global_config(
        list(argv) if argv is not None else list(__import__("sys").argv[1:])
    )
    args = parser.parse_args(normalized)
    global _OUTPUT_FORMAT
    _OUTPUT_FORMAT = args.output
    config_path = Path(args.config).expanduser().resolve()
    try:
        if args.command == "serve":
            return _serve(config_path)
        if args.command == "start":
            args.runtime_command = "start"
            args.foreground = not args.background
            return _runtime_command(args)
        if args.command == "setup":
            return _setup(args)
        if args.command == "repo":
            if args.repo_command == "inspect":
                return _repo_inspect(args)
            if args.repo_command == "propose":
                return _repo_propose(args)
            if args.repo_command in {"enroll", "add"}:
                return _repo_enroll(args)
            if args.repo_command == "remove":
                return _repo_remove(args)
            if args.repo_command == "refresh":
                return _repo_refresh(args)
            if args.repo_command == "list":
                store = _ensure_generation(config_path)
                _json(
                    {
                        "repositories": [
                            asdict(item) for item in _source_for_display(store).repositories
                        ],
                        **_activation_result(
                            store,
                            current_generation.generation
                            if (current_generation := store.current())
                            else 0,
                        ),
                    }
                )
                return 0
        if args.command == "runtime":
            return _runtime_command(args)
        if args.command == "config":
            store = _ensure_generation(config_path)
            if args.config_command == "history":
                _json(
                    {
                        "accepted": asdict(accepted_generation)
                        if (accepted_generation := store.current())
                        else None,
                        "active": asdict(active_generation)
                        if (active_generation := store.active())
                        else None,
                        "generations": [asdict(item) for item in store.history()],
                    }
                )
                return 0
            active = store.active()
            restored = store.rollback(
                args.generation,
                expected_active=active.generation if active else None,
                approval_token=args.approve,
            )
            _json(
                {
                    "status": "rollback_accepted",
                    **asdict(restored),
                    **_activate(store, config_path, restored, mode="auto"),
                }
            )
            return 0
        store = _ensure_generation(config_path)
        if args.command == "diagnostics":
            runtime = _runtime_status(store)
            accepted = store.current()
            active_item = store.active()
            selected = active_item or accepted
            capabilities: dict[str, Any] | None = None
            metrics: dict[str, Any] = {"version": 1, "operations": {}}
            if selected is not None:
                try:
                    diagnostic_config = load_config(store.resolved_path(selected.generation))
                    diagnostic_service = CodingService(diagnostic_config)
                    capabilities = diagnostic_service.doctor()
                    metrics_sink = getattr(diagnostic_service, "metrics", None)
                    if metrics_sink is not None:
                        metrics = metrics_sink.snapshot()
                except Exception as exc:
                    capabilities = {
                        "status": "unavailable",
                        "error_code": _error_code(exc),
                        "detail": redact_text(str(exc)),
                    }
            else:
                metrics = build_metrics_sink(store.root).snapshot()
            payload = build_diagnostics_bundle(
                created_at=system_clock().now_iso(),
                config_path=config_path,
                accepted=asdict(accepted) if accepted else None,
                active=asdict(active_item) if active_item else None,
                runtime=runtime,
                capabilities=capabilities,
                metrics=metrics,
            )
            output = (
                Path(args.bundle_output).expanduser().resolve()
                if args.bundle_output
                else store.root / "diagnostics" / f"bundle-{int(time.time())}.json"
            )
            write_private_file(  # bounded metadata only
                output, (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode()
            )
            _json(
                {
                    "status": "created",
                    "path": str(output),
                    "included": [
                        "hashes",
                        "generation_metadata",
                        "runtime_metadata",
                        "capability_summary",
                        "operation_metrics",
                    ],
                }
            )
            return 0
        active_for_cli = store.active() or store.current()
        if active_for_cli is None:
            raise ConfigError("No accepted configuration generation")
        config = load_config(store.resolved_path(active_for_cli.generation))
        service = CodingService(config)
        if args.command == "show-config":
            _json(
                service.repo_list()
                | {
                    "source": str(config_path),
                    "generation": active_generation.generation
                    if (active_generation := store.active())
                    else None,
                }
            )
            return 0
        if args.command == "doctor":
            result = service.doctor()
            _json(result)
            return 0 if result["ok"] else 1
        if args.command == "list-workspaces":
            _json(service.workspace_list())
            return 0
        parser.error(f"Unknown command: {args.command}")
    except (PersonalCodingMCPError, ConfigError, ValueError, OSError) as exc:
        envelope = operation_error_from_exception(exc)
        _json(
            {
                "status": "failed",
                "error_code": envelope.code.value,
                "what_happened": redact_text(
                    envelope.what_happened,
                    secrets=(os.environ.get("CONTROL_PLANE_API_KEY", ""),),
                ),
                "why": envelope.why,
                "correlation_id": envelope.correlation_id or id_generator().new_hex(24),
                "unchanged_state": list(envelope.unchanged_state)
                or ["active runtime generation unless explicitly reported otherwise"],
                "safe_next_action": envelope.safe_next_action,
                "retryable": envelope.retryable,
                "automatic_retry_allowed": False,
            }
        )
        return 2
    return 0
