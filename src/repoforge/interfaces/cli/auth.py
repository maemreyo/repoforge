"""`rf auth` -- inspect, bind, diagnose, import, and migrate repository identities.

Parsing and command mapping live here rather than in the large `main.py`; only the parser
registration and one dispatch call are wired at the CLI composition seam.

Two conventions run through every command. Reads default to the deterministic selector
(`--auth-profile auto --actor-class human`), so an installation with exactly one eligible
profile needs no prompt and no flags. Writes require the operator to name the exact state they
reviewed -- a binding revision, a lease revision, or a plan hash -- so nothing mutates against
state that changed after it was read. Failures propagate as `RepoForgeError` and are rendered by
the standard typed envelope in `main.py`.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable

from ...application.auth_migration import AuthMigrationService
from ...application.auth_ux import AUTH_SURFACE_ORDER, AuthSurface, AuthUxService
from ...domain.auth_profile import AuthProfileSelector, RequestedActorClass
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.auth_discovery import NamedAccountDiscovery, SshAliasDiscovery

Renderer = Callable[[object], None]

_ACTOR_CLASSES = tuple(item.value for item in RequestedActorClass)
_SURFACE_CHOICES = ("all", *(surface.value for surface in AUTH_SURFACE_ORDER))


def _selector(args: argparse.Namespace) -> AuthProfileSelector:
    try:
        return AuthProfileSelector(
            auth_profile=args.auth_profile,
            actor_class=RequestedActorClass(args.actor_class),
        )
    except ValueError as exc:
        raise RepoForgeError(
            f"The requested identity selector is not usable: {exc}",
            code=ErrorCode.CONFIG_INVALID,
            retryable=False,
            unchanged_state=("No identity was resolved, bound, or used for a write.",),
            safe_next_action="Pass `--auth-profile auto` or a declared profile id.",
        ) from exc


def _add_selector_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auth-profile",
        default="auto",
        help="Declared profile id, or 'auto' for the single eligible profile (default: auto)",
    )
    parser.add_argument(
        "--actor-class",
        default=RequestedActorClass.HUMAN.value,
        choices=_ACTOR_CLASSES,
        help="Actor class the identity is selected for (default: human)",
    )


def add_auth_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register `rf auth` and its subcommands on the top-level command parser."""

    auth = commands.add_parser("auth", help="Inspect and manage repository identities")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    profile = auth_sub.add_parser("profile", help="Reviewed auth profile declarations")
    profile_sub = profile.add_subparsers(dest="auth_profile_command", required=True)
    profile_list = profile_sub.add_parser("list", help="List declared auth profiles")
    profile_list.add_argument(
        "--enabled-only", action="store_true", help="Omit profiles that are declared but disabled"
    )
    profile_list.add_argument(
        "--actor-class",
        choices=_ACTOR_CLASSES,
        help="Only profiles usable for this actor class",
    )
    profile_inspect = profile_sub.add_parser("inspect", help="Inspect one declared auth profile")
    profile_inspect.add_argument("profile_id")

    resolve = auth_sub.add_parser(
        "resolve", help="Show which identity would be selected, without binding it"
    )
    resolve.add_argument("repo_id")
    _add_selector_flags(resolve)

    bind = auth_sub.add_parser("bind", help="Bind a repository to a reviewed auth profile")
    bind.add_argument("repo_id")
    _add_selector_flags(bind)
    bind.add_argument(
        "--expected-revision",
        type=int,
        help="Binding revision this change was reviewed against (required to change one)",
    )

    unbind = auth_sub.add_parser("unbind", help="Clear one actor role from a repository binding")
    unbind.add_argument("repo_id")
    unbind.add_argument(
        "--actor-class", default=RequestedActorClass.HUMAN.value, choices=_ACTOR_CLASSES
    )
    unbind.add_argument("--expected-revision", type=int, required=True)

    whoami = auth_sub.add_parser("whoami", help="Report each identity surface independently")
    whoami.add_argument("repo_id")
    whoami.add_argument(
        "--check",
        action="append",
        choices=_SURFACE_CHOICES,
        help="Surfaces to report (repeatable; default: all)",
    )

    doctor = auth_sub.add_parser("doctor", help="Report why identity is not usable, with recovery")
    doctor.add_argument("repo_id")

    lease = auth_sub.add_parser("lease", help="Operation-scoped auth leases")
    lease_sub = lease.add_subparsers(dest="auth_lease_command", required=True)
    lease_inspect = lease_sub.add_parser("inspect", help="Inspect one operation's identity")
    lease_inspect.add_argument("operation_id")
    lease_revoke = lease_sub.add_parser("revoke", help="Revoke leases on one operation")
    lease_revoke.add_argument("operation_id")
    lease_revoke.add_argument("--expected-revision", type=int, required=True)
    lease_revoke.add_argument("--lease-id", help="Revoke exactly this lease")
    lease_revoke.add_argument("--profile-id", help="Revoke every lease for this profile")

    importer = auth_sub.add_parser("import", help="Discover identities already configured locally")
    import_sub = importer.add_subparsers(dest="auth_import_command", required=True)
    import_gh = import_sub.add_parser("gh", help="List or verify locally configured gh accounts")
    import_gh.add_argument("--host", default="github.com")
    import_gh.add_argument("--login", help="Prove this named account's live actor")
    import_ssh = import_sub.add_parser("ssh", help="Resolve one SSH alias to a pinnable identity")
    import_ssh.add_argument("alias")

    migrate = auth_sub.add_parser("migrate", help="Adopt an existing setup as reviewed profiles")
    migrate_sub = migrate.add_subparsers(dest="auth_migrate_command", required=True)
    migrate_inspect = migrate_sub.add_parser("inspect", help="Build a migration plan")
    migrate_inspect.add_argument("repo_id")
    migrate_apply = migrate_sub.add_parser("apply", help="Apply a reviewed migration plan")
    migrate_apply.add_argument("repo_id")
    migrate_apply.add_argument("--plan-id", required=True)
    migrate_apply.add_argument("--plan-hash", required=True)


def _surfaces(args: argparse.Namespace) -> tuple[AuthSurface, ...] | None:
    requested = getattr(args, "check", None)
    if not requested or "all" in requested:
        return None
    return tuple(AuthSurface(value) for value in requested)


def run_auth_command(
    args: argparse.Namespace,
    *,
    service: AuthUxService,
    migration: AuthMigrationService,
    accounts: NamedAccountDiscovery,
    ssh: SshAliasDiscovery,
    render: Renderer,
) -> int:
    """Map one parsed `rf auth` invocation onto the identity facade."""

    command = args.auth_command
    if command == "profile":
        return _run_profile(args, service=service, render=render)
    if command == "resolve":
        render(service.resolve(repo_id=args.repo_id, selector=_selector(args)))
        return 0
    if command == "bind":
        render(
            service.bind(
                repo_id=args.repo_id,
                selector=_selector(args),
                expected_binding_revision=args.expected_revision,
            )
        )
        return 0
    if command == "unbind":
        render(
            service.unbind(
                repo_id=args.repo_id,
                role=RequestedActorClass(args.actor_class).role,
                expected_binding_revision=args.expected_revision,
            )
        )
        return 0
    if command == "whoami":
        result = service.whoami(repo_id=args.repo_id, checks=_surfaces(args))
        render(result.safe_payload())
        # A report that is not ready is a finding, not a crash: exit 3 is the repository's
        # "input required" code, so a script can branch on it without parsing the payload.
        return 0 if result.ready else 3
    if command == "doctor":
        findings = service.doctor(repo_id=args.repo_id)
        render(
            {
                "repo_id": args.repo_id,
                "findings": [item.safe_payload() for item in findings],
                "blocking": sum(1 for item in findings if item.severity.value == "blocking"),
            }
        )
        return 0 if not any(item.severity.value == "blocking" for item in findings) else 3
    if command == "lease":
        return _run_lease(args, service=service, render=render)
    if command == "import":
        return _run_import(args, accounts=accounts, ssh=ssh, render=render)
    if command == "migrate":
        return _run_migrate(args, migration=migration, render=render)
    raise RepoForgeError(
        f"Unknown auth command: {command}",
        code=ErrorCode.CONFIG_INVALID,
        retryable=False,
        unchanged_state=("No identity was resolved, bound, or used for a write.",),
        safe_next_action="Run `rf auth --help` to list the available commands.",
    )


def _run_profile(args: argparse.Namespace, *, service: AuthUxService, render: Renderer) -> int:
    if args.auth_profile_command == "inspect":
        render(service.profile_inspect(args.profile_id))
        return 0
    role = (
        RequestedActorClass(args.actor_class).role if getattr(args, "actor_class", None) else None
    )
    render({"profiles": service.profile_list(enabled_only=args.enabled_only, role=role)})
    return 0


def _run_lease(args: argparse.Namespace, *, service: AuthUxService, render: Renderer) -> int:
    if args.auth_lease_command == "inspect":
        render(service.lease_inspect(operation_id=args.operation_id))
        return 0
    render(
        service.lease_revoke(
            operation_id=args.operation_id,
            expected_revision=args.expected_revision,
            lease_id=args.lease_id,
            profile_id=args.profile_id,
        )
    )
    return 0


def _run_import(
    args: argparse.Namespace,
    *,
    accounts: NamedAccountDiscovery,
    ssh: SshAliasDiscovery,
    render: Renderer,
) -> int:
    if args.auth_import_command == "gh":
        if args.login:
            render({"verified": accounts.verify(host=args.host, login=args.login).payload()})
            return 0
        render(
            {
                "host": args.host,
                "candidates": [item.payload() for item in accounts.candidates(host=args.host)],
            }
        )
        return 0
    render({"alias": ssh.inspect(args.alias).payload()})
    return 0


def _run_migrate(
    args: argparse.Namespace, *, migration: AuthMigrationService, render: Renderer
) -> int:
    if args.auth_migrate_command == "inspect":
        plan = migration.inspect(repo_id=args.repo_id)
        render(
            {
                **plan.payload(),
                "safe_next_action": (
                    "Apply it with `rf auth migrate apply "
                    f"{args.repo_id} --plan-id {plan.plan_id} --plan-hash {plan.plan_hash}`."
                    if plan.ready
                    else "Resolve the blocking findings above, then inspect again."
                ),
            }
        )
        return 0 if plan.ready else 3
    render(
        migration.apply(
            repo_id=args.repo_id,
            plan_id=args.plan_id,
            plan_hash=args.plan_hash,
            # Adopting an identity is a capability expansion, so the change is recorded
            # against the operator who ran the command in their own terminal.
            actor=os.environ.get("USER", "local-operator"),
        )
    )
    return 0
