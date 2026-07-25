#!/usr/bin/env bash
# Drive a REAL `rf upgrade --activate` inside a fully isolated sandbox.
#
# Every unit test in tests/test_activation_*.py stops at some fake boundary, and four
# review rounds each found a defect that only a live run would expose. This harness runs
# the production path for real: a real wheel build, a real per-release venv install, the
# real smoke tester, real shim provisioning, a real supervisor process, real runtime
# records and a real health probe.
#
# Isolation (nothing here may touch the operator's real installation):
#   HOME                     -> sandbox/home
#   REPOFORGE_CONFIG         -> sandbox/config.toml   (own [server].state_root)
#   REPOFORGE_RELEASE_ROOT   -> sandbox/release-root
#   PATH                     -> sandbox/bin first, providing a STUB tunnel-client so no
#                               tunnel is ever registered with a real control plane
#   LaunchAgents             -> never installed (the agent path is exercised by unit tests)
#
# STATUS (honest scope): this harness reaches the *supervisor start* boundary and has
# already earned its keep -- it found a production defect no unit test caught (the tunnel
# identity was silently dropped when the CLI re-entered itself through the stable shim,
# because REPOFORGE_TUNNEL_ID was missing from the launcher's inherited env allowlist).
# It does NOT yet demonstrate a fully converged activation: the enrollment step below
# still fails, so the resolved generation the worker loads is incomplete. Finishing that
# is tracked as a follow-up; until it passes, "live activation converged" remains
# unproven.
#
# Usage: scripts/live-activation-sandbox.sh [--keep]
set -euo pipefail

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/rf-live-activation.XXXXXX")"
cleanup() {
  # Always stop anything the sandbox started before removing its state.
  if [[ -f "$SANDBOX/state/supervisor.pid" ]]; then
    pkill -g "$(cat "$SANDBOX/state/supervisor.pid")" 2>/dev/null || true
  fi
  pkill -f "$SANDBOX" 2>/dev/null || true
  if [[ "$KEEP" == "1" ]]; then
    echo "sandbox kept at $SANDBOX"
  else
    rm -rf "$SANDBOX"
  fi
}
trap cleanup EXIT INT TERM

say() { printf '\n\033[36m══> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$1"; exit 1; }
ok() { printf '\033[32m✓ %s\033[0m\n' "$1"; }

mkdir -p "$SANDBOX"/{home,bin,release-root,state,workspaces}

# --------------------------------------------------------------------- stub tunnel-client
# Implements only what adapters/runtime/tunnel_cli.py requires: --version, init, doctor,
# and `run` (which spawns the recorded MCP command and advertises a local health URL).
cat > "$SANDBOX/bin/tunnel-client" <<'STUB'
#!/usr/bin/env python3
"""Local stand-in for tunnel-client: never contacts a control plane."""
import http.server
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path

STATE = Path(os.environ["RF_SANDBOX"]) / "tunnel-stub"
STATE.mkdir(parents=True, exist_ok=True)


def _serve_health() -> int:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_port


argv = sys.argv[1:]
if argv and argv[0] == "--version":
    print("tunnel-client 0.0.0-sandbox-stub")
    raise SystemExit(0)
if argv and argv[0] == "init":
    command = argv[argv.index("--mcp-command") + 1] if "--mcp-command" in argv else ""
    (STATE / "mcp-command").write_text(command, encoding="utf-8")
    print("initialized")
    raise SystemExit(0)
if argv and argv[0] == "doctor":
    print("doctor ok")
    raise SystemExit(0)
if argv and argv[0] == "run":
    port = _serve_health()
    # tunnel_cli parses stdout JSON lines for a 127.0.0.1 health_url.
    print(json.dumps({"msg": "tunnel ready", "health_url": f"http://127.0.0.1:{port}/healthz"}))
    sys.stdout.flush()
    command = (STATE / "mcp-command").read_text(encoding="utf-8").strip()
    if not command:
        raise SystemExit("stub: no MCP command recorded by init")
    # Spawn the serve child exactly as the real tunnel-client does, and outlive it only
    # if it exits; the supervisor treats our exit as the child dying.
    child = subprocess.Popen(shlex.split(command), stdin=subprocess.DEVNULL)
    raise SystemExit(child.wait())
raise SystemExit(f"stub: unsupported argv {argv}")
STUB
chmod +x "$SANDBOX/bin/tunnel-client"

# ------------------------------------------------------------------ clean source worktree
# `rf upgrade` refuses a dirty worktree, so build from a pristine clone of HEAD.
say "Cloning HEAD into the sandbox (a clean worktree is required)"
CLONE="$SANDBOX/source"
git -C "$REPO_ROOT" clone --quiet --local --no-hardlinks . "$CLONE"
git -C "$CLONE" checkout --quiet "$(git -C "$REPO_ROOT" rev-parse HEAD)"
HEAD_SHA="$(git -C "$CLONE" rev-parse HEAD)"
ok "clone at ${HEAD_SHA:0:12}, clean=$([[ -z "$(git -C "$CLONE" status --porcelain)" ]] && echo yes || echo no)"

# --------------------------------------------------------------------------- demo repo
DEMO="$SANDBOX/demo"
REMOTE="$SANDBOX/demo-remote.git"
git init --quiet --bare -b main "$REMOTE"
mkdir -p "$DEMO"
git -C "$DEMO" init --quiet -b main
printf 'demo\n' > "$DEMO/README.md"
git -C "$DEMO" add README.md
git -C "$DEMO" -c user.email=s@x -c user.name=s commit --quiet -m "init"
# Enrollment requires a remote; a local bare repo keeps everything inside the sandbox.
git -C "$DEMO" remote add origin "$REMOTE"
git -C "$DEMO" push --quiet origin main

# ------------------------------------------------------------------------------ config
# `rf setup` creates the configuration at this path.
export HOME="$SANDBOX/home"
export XDG_DATA_HOME="$SANDBOX/home/.local/share"
export XDG_STATE_HOME="$SANDBOX/home/.local/state"
export REPOFORGE_CONFIG="$SANDBOX/config.toml"
export REPOFORGE_RELEASE_ROOT="$SANDBOX/release-root"
export REPOFORGE_TUNNEL_ID="sandbox-tunnel"
export REPOFORGE_TUNNEL_PROFILE="sandbox"
export RF_SANDBOX="$SANDBOX"
export PATH="$SANDBOX/bin:$PATH"

say "Sandbox environment"
printf '  HOME=%s\n  REPOFORGE_CONFIG=%s\n  REPOFORGE_RELEASE_ROOT=%s\n  tunnel-client=%s\n' \
  "$HOME" "$REPOFORGE_CONFIG" "$REPOFORGE_RELEASE_ROOT" "$(command -v tunnel-client)"

RF=(uv run --directory "$REPO_ROOT" --extra dev rf --config "$SANDBOX/config.toml")

# `rf setup` is the real bootstrap: it enrolls the repository and writes a MODERN
# configuration generation. A hand-written config would be imported as *legacy*, which
# refuses repository mutation and renders a resolved config with no [repositories] table,
# so the supervisor worker could never load it.
say "Bootstrapping the sandbox installation with rf setup (enrolls the demo repo)"
SETUP=("${RF[@]}" setup "$DEMO" --tunnel-id sandbox-tunnel --profile sandbox
       --template read_only --activate never)
# `rf setup` without --approve exits non-zero by design (approval required), so the
# probe must not trip `set -e`.
set +e
SETUP_OUT="$("${SETUP[@]}" 2>&1)"
set -e
TOKEN="$(printf '%s' "$SETUP_OUT" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["required_approval_tokens"][0])
except Exception:
    print("")')"
if [[ -z "$TOKEN" ]]; then
  printf '%s\n' "$SETUP_OUT" | head -40
  fail "rf setup did not offer an approval token (see output above)"
fi
"${SETUP[@]}" --approve "$TOKEN" >/dev/null || fail "rf setup --approve failed"
RESOLVED="$(find "$HOME/.local/state/repoforge" -name resolved.toml | head -1)"
[[ -n "$RESOLVED" ]] && grep -q '^\[repositories' "$RESOLVED" \
  || fail "the resolved generation has no [repositories] table"
ok "sandbox installation bootstrapped (modern generation with an enrolled repository)"

say "rf version status (before any activation)"
"${RF[@]}" version status || true

say "rf upgrade --from-worktree <clone> --activate  [THE REAL THING]"
set +e
"${RF[@]}" upgrade --from-worktree "$CLONE" --activate --keep 3
UPGRADE_EXIT=$?
set -e
echo "upgrade exit=$UPGRADE_EXIT"

say "Post-activation evidence"
echo "--- release root tree ---"
find "$SANDBOX/release-root" -maxdepth 2 -mindepth 1 | sed "s|$SANDBOX|\$SANDBOX|" | sort
echo "--- current symlink ---"
readlink "$SANDBOX/release-root/current" 2>/dev/null || echo "(none)"
echo "--- receipts ---"
ls "$SANDBOX/release-root/runtime/activation-receipts" 2>/dev/null || echo "(none)"
echo "--- runtime record ---"
find "$SANDBOX/state" -name 'managed-runtime-v3.json' -exec cat {} \; 2>/dev/null | head -30 || true

say "rf version status (after activation)"
"${RF[@]}" version status || true

say "The operator's REAL installation must be untouched"
[[ ! -e "$HOME/.local/bin/rf" ]] && ok "no PATH shim inside the sandbox HOME" \
  || fail "sandbox created $HOME/.local/bin/rf unexpectedly"

if [[ "$UPGRADE_EXIT" != "0" ]]; then
  fail "live activation did not succeed (exit $UPGRADE_EXIT) -- see evidence above"
fi
ok "live activation converged"
