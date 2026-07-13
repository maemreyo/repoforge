"""Frozen public CLI contract for release drift detection."""

from __future__ import annotations


def build_cli_release_contract() -> dict[str, object]:
    """Return the intentionally reviewed guided-onboarding CLI surface."""
    return {
        "commands": {
            "onboard": {
                "actions": ["run", "status", "resume", "cancel"],
                "arguments": ["ROOT"],
                "options": [
                    "--activate",
                    "--approve",
                    "--decision",
                    "--exclude",
                    "--include",
                    "--max-depth",
                    "--no-rollback-on-failure",
                    "--no-wait",
                    "--non-interactive",
                    "--plan-only",
                    "--policy-override",
                    "--profile",
                    "--repo-id",
                    "--resume",
                    "--rollback-on-failure",
                    "--template",
                    "--tunnel-id",
                    "--wait",
                ],
            },
            "repo discover": {
                "arguments": ["ROOT"],
                "options": ["--exclude", "--include", "--max-depth"],
                "read_only": True,
            },
        },
        "exit_codes": {
            "0": "completed or read-only success",
            "2": "stable validation or operation failure",
            "3": "operator decision or exact approval required",
        },
        "session_schema_version": 1,
    }
