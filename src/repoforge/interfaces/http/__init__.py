"""Optional loopback-only HTTP interfaces."""

from .github_webhooks import GitHubWebhookApplication, serve_github_webhooks

__all__ = ["GitHubWebhookApplication", "serve_github_webhooks"]
