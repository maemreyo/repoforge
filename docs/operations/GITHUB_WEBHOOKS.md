# GitHub webhook cache invalidation

RepoForge can receive signed GitHub webhooks and invalidate the short-lived ticket-graph
cache for the affected configured repository. The ingress is optional, disabled by
default, and never edits GitHub or runs repository commands.

GitHub documents separate events for
[`issues`, `sub_issues`, and `issue_dependencies`](https://docs.github.com/en/webhooks/webhook-events-and-payloads),
and a `projects_v2_item` event for Project field changes. Subscribe only to those four
events.

## Configure RepoForge

Add reviewed server and repository settings:

```toml
[server]
github_webhook_enabled = true
github_webhook_bind = "127.0.0.1"
github_webhook_port = 8766
github_webhook_secret_env = "REPOFORGE_GITHUB_WEBHOOK_SECRET"
github_webhook_max_body_bytes = 1000000

[repositories.example.ticket_graph]
root_issue = 3
repository = "owner/repository"

# Required only for projects_v2_item routing.
project_owner = "owner"
project_number = 7
```

Set the secret in the process environment, not in TOML:

```bash
export REPOFORGE_GITHUB_WEBHOOK_SECRET='replace-with-a-long-random-secret'
rf webhook serve
```

The endpoint is `POST /github/webhooks`. The default bind is loopback-only. If GitHub
must reach it over the internet, place it behind a trusted TLS reverse proxy or tunnel
that forwards only this path; do not expose the RepoForge MCP server as a public webhook
endpoint.

## Configure GitHub

Create a repository, organization, or GitHub App webhook with:

- Payload URL ending in `/github/webhooks`
- Content type `application/json`
- The same secret
- Events `issues`, `sub_issues`, `issue_dependencies`, and
  `projects_v2_item` when Project metadata is configured

Project webhook events are currently described by GitHub as public preview and may
change. Repository and organization event availability also depends on account and app
permissions.

## Security and behavior

RepoForge verifies `X-Hub-Signature-256` with HMAC-SHA256 before JSON parsing. It
bounds request size, validates repository identities, accepts only the four allowlisted
events, hashes delivery IDs, and deduplicates a bounded number of deliveries in memory.
A restart clears the deduplication window; repeated invalidation remains harmless.

A valid event removes only `kind="graph"` entries for matching configured repositories.
Issue and pull-request caches are untouched. Unknown repositories are rejected, and
unsupported events return without mutation.
