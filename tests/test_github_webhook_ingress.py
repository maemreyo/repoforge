from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from repoforge.application.webhooks.github import (
    affected_repository,
    project_owner,
    verify_github_signature,
)
from repoforge.config import (
    AppConfig,
    GitHubTicketGraphConfig,
    RepositoryConfig,
    ServerConfig,
    load_config,
)
from repoforge.interfaces.http.github_webhooks import GitHubWebhookApplication


class RecordingCache:
    def __init__(self) -> None:
        self.invalidations: list[tuple[str, Path, str | None]] = []

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        del args, kwargs
        return None

    def put(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def invalidate(self, repo_id: str, repo_path: Path, *, kind: str | None = None) -> int:
        self.invalidations.append((repo_id, repo_path, kind))
        return 1


def _signature(body: bytes, secret: bytes = b"secret") -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _config(tmp_path: Path, *, max_body: int = 1_000_000) -> AppConfig:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    return AppConfig(
        tmp_path / "config.toml",
        ServerConfig(
            tmp_path / "workspaces",
            tmp_path / "state",
            github_webhook_max_body_bytes=max_body,
        ),
        {
            "demo": RepositoryConfig(
                "demo",
                repo_path,
                ticket_graph=GitHubTicketGraphConfig(
                    root_issue=3,
                    repository="acme/widgets",
                    project_owner="acme",
                    project_number=7,
                ),
            )
        },
    )


def _handle(
    app: GitHubWebhookApplication,
    payload: object,
    *,
    event: str = "issues",
    delivery: str = "delivery-1",
    secret: bytes = b"secret",
) -> tuple[int, dict[str, Any] | None]:
    body = json.dumps(payload).encode()
    return app.handle(
        event=event,
        delivery=delivery,
        signature=_signature(body, secret),
        body=body,
    )


def test_signature_and_routing_helpers() -> None:
    body = b'{"ok":true}'
    signature = _signature(body)

    assert verify_github_signature(body, signature, b"secret") is True
    assert verify_github_signature(body, signature, b"wrong") is False
    assert verify_github_signature(body, "sha1=bad", b"secret") is False
    assert affected_repository("issues", {"repository": {"full_name": "acme/widgets"}}) == (
        "acme/widgets"
    )
    assert (
        affected_repository("sub_issues", {"repository": {"full_name": "acme/widgets"}})
        == "acme/widgets"
    )
    assert affected_repository("push", {"repository": {"full_name": "acme/widgets"}}) is None
    assert project_owner({"organization": {"login": "acme"}}) == "acme"
    assert project_owner({"sender": {"login": "octocat"}}) == "octocat"


def test_issue_event_invalidates_only_graph_cache_and_deduplicates(tmp_path: Path) -> None:
    cache = RecordingCache()
    app = GitHubWebhookApplication(_config(tmp_path), cache, b"secret")
    payload = {"repository": {"full_name": "acme/widgets"}}

    status, result = _handle(app, payload)

    assert status == 202
    assert result == {
        "duplicate": False,
        "invalidated": 1,
        "repositories": ["demo"],
    }
    assert cache.invalidations == [("demo", tmp_path / "repo", "graph")]

    duplicate_status, duplicate = _handle(app, payload)
    assert duplicate_status == 202
    assert duplicate == {"duplicate": True, "invalidated": 0}
    assert len(cache.invalidations) == 1


def test_project_event_routes_by_configured_owner(tmp_path: Path) -> None:
    cache = RecordingCache()
    app = GitHubWebhookApplication(_config(tmp_path), cache, b"secret")

    status, result = _handle(
        app,
        {"organization": {"login": "acme"}},
        event="projects_v2_item",
    )

    assert status == 202
    assert result is not None
    assert result["repositories"] == ["demo"]


def test_webhook_rejects_bad_inputs_without_invalidating(tmp_path: Path) -> None:
    cache = RecordingCache()
    app = GitHubWebhookApplication(_config(tmp_path, max_body=200), cache, b"secret")

    oversized = b"x" * 201
    assert (
        app.handle(
            event="issues",
            delivery="large",
            signature=_signature(oversized),
            body=oversized,
        )[0]
        == 413
    )

    body = b"{}"
    assert (
        app.handle(
            event="issues",
            delivery="bad-signature",
            signature=_signature(body, b"wrong"),
            body=body,
        )[0]
        == 401
    )
    assert app.handle(
        event="push",
        delivery="ignored",
        signature=_signature(body),
        body=body,
    ) == (204, None)
    assert (
        app.handle(
            event="issues",
            delivery="invalid-json",
            signature=_signature(b"{"),
            body=b"{",
        )[0]
        == 400
    )
    assert _handle(app, [], delivery="not-an-object")[0] == 400
    assert _handle(app, {}, delivery="")[0] == 400
    assert (
        _handle(
            app,
            {"repository": {"full_name": "other/repo"}},
            delivery="unknown",
        )[0]
        == 422
    )
    assert cache.invalidations == []


def test_config_loads_ticket_graph_and_opt_in_webhook(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
github_webhook_enabled = true
github_webhook_bind = "127.0.0.1"
github_webhook_port = 9001
github_webhook_secret_env = "FORGE_WEBHOOK_SECRET"
github_webhook_max_body_bytes = 4096

[repositories.demo]
path = "{repo}"

[repositories.demo.ticket_graph]
root_issue = 3
repository = "acme/widgets"
project_owner = "acme"
project_number = 7
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.server.github_webhook_enabled is True
    assert config.server.github_webhook_port == 9001
    assert config.server.github_webhook_secret_env == "FORGE_WEBHOOK_SECRET"
    assert config.server.github_webhook_max_body_bytes == 4096
    assert config.repositories["demo"].ticket_graph == GitHubTicketGraphConfig(
        root_issue=3,
        repository="acme/widgets",
        project_owner="acme",
        project_number=7,
    )
