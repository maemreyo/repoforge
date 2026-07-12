"""Command-line entry point and setup ergonomics for RepoForge."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_PATH, load_config
from .discovery import detect_repository, render_config
from .errors import PersonalCodingMCPError
from .server import create_server
from .service import CodingService


def _write_text(destination: Path, content: str, force: bool) -> None:
    if destination.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _doctor_fix() -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    gh = shutil.which("gh")
    if gh:
        auth = subprocess.run(
            [gh, "auth", "status"], capture_output=True, text=True, check=False, timeout=30
        )
        if auth.returncode == 0:
            setup = subprocess.run(
                [gh, "auth", "setup-git"], capture_output=True, text=True, check=False, timeout=30
            )
            actions.append(
                {
                    "action": "gh auth setup-git",
                    "ok": setup.returncode == 0,
                    "detail": (setup.stdout + setup.stderr).strip(),
                }
            )
        else:
            actions.append(
                {
                    "action": "gh auth setup-git",
                    "ok": False,
                    "detail": "Skipped because gh is not authenticated. Run `gh auth login`.",
                }
            )
    else:
        actions.append(
            {"action": "gh auth setup-git", "ok": False, "detail": "gh is not installed"}
        )
    return {"actions": actions}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoforge",
        description="Safe local coding workspaces for ChatGPT, backed by git and gh",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH)),
        help="Path to config.toml (default: %(default)s; env: REPOFORGE_CONFIG)",
    )
    parser.add_argument("--version", action="version", version="RepoForge 2.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Detect a repository and generate a ready-to-use configuration"
    )
    init_parser.add_argument(
        "--repo",
        default="/Users/trung.ngo/Documents/zaob-dev/work-frontier",
        help="Local Git repository to configure",
    )
    init_parser.add_argument("--repo-id", default=None, help="Short model-facing repository id")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config")

    inspect_parser = subparsers.add_parser(
        "inspect-repo", help="Preview detected ecosystem, scripts, profiles, and instructions"
    )
    inspect_parser.add_argument("repo", nargs="?", default=".")
    inspect_parser.add_argument("--repo-id", default=None)
    inspect_parser.add_argument("--render-config", action="store_true")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Validate tools, versions, auth, paths, remotes, and profiles"
    )
    doctor_parser.add_argument(
        "--fix", action="store_true", help="Apply safe local fixes such as gh auth setup-git"
    )

    subparsers.add_parser("serve", help="Run the MCP server over stdio")
    subparsers.add_parser("show-config", help="Print the parsed configuration as JSON")
    subparsers.add_parser("list-workspaces", help="Print registered workspaces as JSON")

    smoke_parser = subparsers.add_parser(
        "smoke-test", help="Exercise repository and isolated-worktree operations without editing code"
    )
    smoke_parser.add_argument("--repo-id", default=None)

    remove_parser = subparsers.add_parser("remove-workspace", help="Remove a clean local worktree")
    remove_parser.add_argument("workspace_id")
    remove_parser.add_argument("--delete-local-branch", action="store_true")

    audit_parser = subparsers.add_parser("audit", help="Show recent local audit events")
    audit_parser.add_argument("--tail", type=int, default=50)

    tunnel_parser = subparsers.add_parser(
        "tunnel-command", help="Print a tunnel-client command for this installed RepoForge"
    )
    tunnel_parser.add_argument("--tunnel-id", required=True)
    tunnel_parser.add_argument("--profile", default="repoforge")
    tunnel_parser.add_argument("--tunnel-client", default="tunnel-client")
    return parser


def _config_as_dict(service: CodingService) -> dict[str, Any]:
    return service.repo_list() | {
        "config_path": str(service.config.source_path),
        "workspace_root": str(service.config.server.workspace_root),
        "state_root": str(service.config.server.state_root),
    }


def _smoke_test(service: CodingService, repo_id: str | None) -> dict[str, Any]:
    ids = sorted(service.config.repositories)
    selected = repo_id or (ids[0] if len(ids) == 1 else None)
    if selected is None:
        raise ValueError(f"Select --repo-id from: {ids}")
    results: list[dict[str, Any]] = []

    def step(name: str, function: Any) -> Any:
        try:
            value = function()
        except Exception as exc:
            results.append({"step": name, "ok": False, "error": str(exc)})
            raise
        results.append({"step": name, "ok": True})
        return value

    step("repo_list", service.repo_list)
    step("repo_status", lambda: service.repo_status(selected))
    step("repo_context", lambda: service.repo_context(selected))
    step("repo_recent_commits", lambda: service.repo_recent_commits(selected, 3))
    workspace = step(
        "workspace_create", lambda: service.workspace_create(selected, "repoforge-smoke-test")
    )
    workspace_id = workspace["workspace_id"]
    try:
        step("workspace_status", lambda: service.workspace_status(workspace_id))
        step("workspace_tree", lambda: service.workspace_tree(workspace_id, 50))
        step("workspace_diff", lambda: service.workspace_diff(workspace_id))
    finally:
        step(
            "workspace_remove",
            lambda: service.workspace_remove(workspace_id, delete_local_branch=True),
        )
    return {"ok": all(item["ok"] for item in results), "repo_id": selected, "steps": results}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    try:
        if args.command == "init":
            detection = detect_repository(args.repo, args.repo_id)
            _write_text(config_path, render_config(detection), args.force)
            _json(
                {
                    "created": str(config_path),
                    "repo_id": detection.repo_id,
                    "repo_path": str(detection.path),
                    "ecosystem": detection.ecosystem,
                    "package_manager": detection.package_manager,
                    "profiles": [profile.name for profile in detection.profiles],
                    "warnings": list(detection.warnings),
                    "next": [
                        f"repoforge --config {config_path} doctor",
                        f"repoforge --config {config_path} smoke-test --repo-id {detection.repo_id}",
                    ],
                }
            )
            return 0

        if args.command == "inspect-repo":
            detection = detect_repository(args.repo, args.repo_id)
            if args.render_config:
                print(render_config(detection), end="")
            else:
                _json(
                    {
                        "path": str(detection.path),
                        "repo_id": detection.repo_id,
                        "display_name": detection.display_name,
                        "remote": detection.remote,
                        "default_base": detection.default_base,
                        "ecosystem": detection.ecosystem,
                        "package_manager": detection.package_manager,
                        "package_manager_version": detection.package_manager_version,
                        "scripts": list(detection.scripts),
                        "instruction_files": list(detection.instruction_files),
                        "profiles": [
                            {
                                "name": profile.name,
                                "description": profile.description,
                                "verification": profile.verification,
                                "commands": [list(command) for command in profile.commands],
                            }
                            for profile in detection.profiles
                        ],
                        "warnings": list(detection.warnings),
                    }
                )
            return 0

        if args.command == "serve":
            # stdio transport reserves stdout for JSON-RPC protocol messages.
            create_server(config_path).run(transport="stdio")
            return 0

        service = CodingService(load_config(config_path))
        if args.command == "doctor":
            result = service.doctor()
            if args.fix:
                result["fixes"] = _doctor_fix()
                result["after_fix"] = service.doctor()
                result["ok"] = result["after_fix"]["ok"]
            _json(result)
            return 0 if result["ok"] else 1
        if args.command == "show-config":
            _json(_config_as_dict(service))
            return 0
        if args.command == "list-workspaces":
            _json(service.workspace_list())
            return 0
        if args.command == "smoke-test":
            result = _smoke_test(service, args.repo_id)
            _json(result)
            return 0 if result["ok"] else 1
        if args.command == "remove-workspace":
            _json(service.workspace_remove(args.workspace_id, args.delete_local_branch))
            return 0
        if args.command == "audit":
            tail = max(1, min(args.tail, 10_000))
            path = service.audit.path
            lines = path.read_text(encoding="utf-8").splitlines()[-tail:] if path.exists() else []
            _json({"path": str(path), "events": [json.loads(line) for line in lines]})
            return 0
        if args.command == "tunnel-command":
            executable = shutil.which("repoforge") or shutil.which("rf") or "repoforge"
            mcp_command = f"{executable} --config {config_path} serve"
            _json(
                {
                    "init": [
                        args.tunnel_client,
                        "init",
                        "--sample",
                        "sample_mcp_stdio_local",
                        "--profile",
                        args.profile,
                        "--tunnel-id",
                        args.tunnel_id,
                        "--mcp-command",
                        mcp_command,
                    ],
                    "doctor": [args.tunnel_client, "doctor", "--profile", args.profile, "--explain"],
                    "run": [args.tunnel_client, "run", "--profile", args.profile],
                }
            )
            return 0
        parser.error(f"Unknown command: {args.command}")
    except (PersonalCodingMCPError, FileExistsError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
