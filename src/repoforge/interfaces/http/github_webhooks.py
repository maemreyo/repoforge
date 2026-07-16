"""Opt-in loopback GitHub webhook ingress for graph-cache invalidation only."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ...application.webhooks.github import (
    SUPPORTED_GITHUB_EVENTS,
    affected_repository,
    project_owner,
    verify_github_signature,
)
from ...config import AppConfig
from ...ports.github_read_cache import GitHubReadCache


class GitHubWebhookApplication:
    """Authenticate, deduplicate, route, and invalidate without executing commands."""

    def __init__(
        self,
        config: AppConfig,
        cache: GitHubReadCache,
        secret: bytes,
        *,
        max_deliveries: int = 2_000,
    ) -> None:
        self._config = config
        self._cache = cache
        self._secret = secret
        self._max_deliveries = max(1, max_deliveries)
        self._deliveries: OrderedDict[str, None] = OrderedDict()

    def handle(
        self,
        *,
        event: str,
        delivery: str,
        signature: str,
        body: bytes,
    ) -> tuple[int, dict[str, Any] | None]:
        if len(body) > self._config.server.github_webhook_max_body_bytes:
            return 413, {"error": "payload_too_large"}
        if not verify_github_signature(body, signature, self._secret):
            return 401, {"error": "invalid_signature"}
        if event not in SUPPORTED_GITHUB_EVENTS:
            return 204, None
        try:
            payload: Any = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "invalid_json"}
        if not isinstance(payload, dict):
            return 400, {"error": "invalid_payload"}
        if not delivery or len(delivery) > 200:
            return 400, {"error": "invalid_delivery"}
        delivery_key = hashlib.sha256(delivery.encode("utf-8")).hexdigest()
        if delivery_key in self._deliveries:
            self._deliveries.move_to_end(delivery_key)
            return 202, {"duplicate": True, "invalidated": 0}

        repository = affected_repository(event, payload)
        owner = project_owner(payload) if event == "projects_v2_item" else None
        targets = []
        for repo in self._config.repositories.values():
            source = repo.ticket_graph
            if source is None:
                continue
            if (repository is not None and source.repository == repository) or (
                repository is None and owner is not None and source.project_owner == owner
            ):
                targets.append(repo)
        if not targets:
            return 422, {"error": "unknown_repository"}

        invalidated = sum(
            self._cache.invalidate(repo.repo_id, repo.path, kind="graph") for repo in targets
        )
        self._deliveries[delivery_key] = None
        while len(self._deliveries) > self._max_deliveries:
            self._deliveries.popitem(last=False)
        return 202, {
            "duplicate": False,
            "invalidated": invalidated,
            "repositories": [repo.repo_id for repo in targets],
        }


def _handler(application: GitHubWebhookApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/github/webhooks":
                self.send_error(404)
                return
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError:
                self.send_error(400)
                return
            maximum = application._config.server.github_webhook_max_body_bytes
            if length < 0 or length > maximum:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            status, payload = application.handle(
                event=self.headers.get("X-GitHub-Event", ""),
                delivery=self.headers.get("X-GitHub-Delivery", ""),
                signature=self.headers.get("X-Hub-Signature-256", ""),
                body=body,
            )
            encoded = (
                b"" if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
            )
            self.send_response(status)
            if encoded:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            if encoded:
                self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


def serve_github_webhooks(config: AppConfig, cache: GitHubReadCache, secret: bytes) -> None:
    server = ThreadingHTTPServer(
        (config.server.github_webhook_bind, config.server.github_webhook_port),
        _handler(GitHubWebhookApplication(config, cache, secret)),
    )
    server.serve_forever()
