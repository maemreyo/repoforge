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
# STATUS: this harness demonstrates a CONVERGED live activation and asserts it, so a
# regression that merely returns exit 0 cannot pass. It found four production defects no
# unit test caught: the tunnel identity dropped across the shim hop; a staged install
# breaking every activation because venv console scripts hard-code the interpreter path;
# runtime identity being underivable from `sys.executable` (relocatable venvs symlink out
# of the release tree); and the captured release identity dropped again when the inner CLI
# spawned the worker.
#
# Usage: scripts/live-activation-sandbox.sh [--keep]
set -euo pipefail

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Captured BEFORE HOME is overridden: the "real installation untouched" assertion must
# check the operator's actual home, not the sandbox one.
ORIGINAL_HOME="$HOME"
# `env -i` strips PATH, so uv must be addressed absolutely.
UV_BIN="$(command -v uv)" || { echo "uv is required" >&2; exit 1; }
# `uv` is also invoked as a *subprocess* by the builder/installer, so its directory must
# be on the scrubbed PATH. It is a build tool, not a credential.
UV_DIR="$(dirname "$UV_BIN")"
snapshot_real_home() {
  # Content-addressed, not name-only: hashing directory listings would miss a modified
  # file *inside* existing state, which is exactly the mutation worth catching.
  {
    for target in \
      "$ORIGINAL_HOME/.local/bin/rf" \
      "$ORIGINAL_HOME/Library/LaunchAgents/dev.repoforge.supervisor.plist" \
      "$ORIGINAL_HOME/.local/share/repoforge" \
      "$ORIGINAL_HOME/.local/state/repoforge"; do
      if [[ ! -e "$target" && ! -L "$target" ]]; then
        echo "absent ${target#"$ORIGINAL_HOME"}"
        continue
      fi
      find "$target" \( -type f -o -type d -o -type l \) -print0 2>/dev/null | sort -z |
        while IFS= read -r -d "" path; do
          rel="${path#"$ORIGINAL_HOME"}"
          if [[ -L "$path" ]]; then
            printf 'L %s -> %s\n' "$rel" "$(readlink "$path")"
          elif [[ -d "$path" ]]; then
            printf 'D %s %s\n' "$rel" "$(stat -f '%Lp' "$path" 2>/dev/null)"
          else
            printf 'F %s %s %s %s\n' "$rel" "$(stat -f '%Lp' "$path" 2>/dev/null)" \
              "$(stat -f '%z' "$path" 2>/dev/null)" \
              "$(shasum -a 256 "$path" 2>/dev/null | cut -d' ' -f1)"
          fi
        done
    done
  } | shasum -a 256 | cut -d" " -f1
}
REAL_HOME_BEFORE="$(snapshot_real_home)"
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
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

# Derive state from our own location: the supervisor runs us with a restricted env
# allowlist, so depending on an ambient variable here would crash the worker.
STATE = Path(__file__).resolve().parent.parent / "tunnel-stub"
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
    # The MCP child serves over STDIO, so it must be given a stdin pipe that stays open.
    # With DEVNULL it reads EOF and exits immediately, and the supervisor then respawns
    # the tunnel in a loop until it gives up -- which is exactly what happened here.
    child = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE)

    def _heartbeat() -> None:
        # tunnel_cli treats this exact phrase as a successful control-plane round trip;
        # without it the supervisor never reaches a fully healthy phase.
        while child.poll() is None:
            print("dispatcher acknowledged notification with control plane", flush=True)
            time.sleep(2)

    threading.Thread(target=_heartbeat, daemon=True).start()
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

# P1-5: `env -i` so no ambient CONTROL_PLANE_API_KEY, and cwd inside the sandbox so the
# repository's own .env can never be picked up. The key is a local-only placeholder: the
# stub tunnel-client never contacts a control plane.
RF=(env -i
    HOME="$SANDBOX/home"
    PATH="$SANDBOX/bin:$UV_DIR:/usr/local/bin:/usr/bin:/bin"
    TMPDIR="${TMPDIR:-/tmp}"
    XDG_DATA_HOME="$SANDBOX/home/.local/share"
    XDG_STATE_HOME="$SANDBOX/home/.local/state"
    REPOFORGE_CONFIG="$SANDBOX/config.toml"
    REPOFORGE_RELEASE_ROOT="$SANDBOX/release-root"
    REPOFORGE_TUNNEL_ID="sandbox-tunnel"
    REPOFORGE_TUNNEL_PROFILE="sandbox"
    CONTROL_PLANE_API_KEY="sandbox-only-never-networked"
    "$UV_BIN" run --directory "$SANDBOX" --project "$REPO_ROOT" --extra dev
    rf --config "$SANDBOX/config.toml")

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
"${RF[@]}" upgrade --from-worktree "$CLONE" --activate --keep 3 | tee "$SANDBOX/upgrade.json"
UPGRADE_EXIT=${PIPESTATUS[0]}
set -e
echo "upgrade exit=$UPGRADE_EXIT"

# Distinguish "the supervisor never started" from "it started later than the CLI was
# willing to wait" -- those are very different defects and the error message is the same.
say "Late-arrival probe: did a runtime record appear AFTER the command gave up?"
RECORD=""
for _ in $(seq 1 60); do
  RECORD="$(find "$HOME/.local/state/repoforge" -name managed-runtime-v3.json 2>/dev/null | head -1)"
  [[ -n "$RECORD" ]] && break
  sleep 1
done
if [[ -n "$RECORD" ]]; then
  printf '  \033[33m! a runtime record appeared late at %s\033[0m\n' "${RECORD#"$HOME"}"
  python3 -c "import json,sys; d=json.load(open('$RECORD')); print('  phase=',d.get('phase'),'pid=',d.get('pid'),'gen=',d.get('active_generation'))"
else
  echo "  no runtime record appeared within 60s"
fi

say "Post-activation evidence"
echo "--- release root tree ---"
find "$SANDBOX/release-root" -maxdepth 2 -mindepth 1 | sed "s|$SANDBOX|\$SANDBOX|" | sort
echo "--- current symlink ---"
readlink "$SANDBOX/release-root/current" 2>/dev/null || echo "(none)"
echo "--- receipts ---"
ls "$SANDBOX/release-root/runtime/activation-receipts" 2>/dev/null || echo "(none)"
echo "--- runtime record (the exact path the probe located) ---"
if [[ -n "$RECORD" ]]; then
  python3 -c "import json;d=json.load(open('$RECORD'));print(json.dumps({k:d.get(k) for k in ('phase','pid','running_release_sha','active_generation','executable')}, indent=2))"
else
  echo "(none)"
fi

say "rf version status (immediately after activation 1)"
set +e
"${RF[@]}" version status | tee "$SANDBOX/status1.json"
STATUS1_EXIT=${PIPESTATUS[0]}
set -e

say "SECOND activation: a new candidate must take over and demote the first"
# A second commit in the clone gives a genuinely different release sha.
printf 'second candidate\n' >> "$CLONE/README.md"
git -C "$CLONE" -c user.email=s@x -c user.name=s commit --quiet -am "second candidate"
SECOND_SHA="$(git -C "$CLONE" rev-parse HEAD)"
ok "second candidate at ${SECOND_SHA:0:12}"
set +e
"${RF[@]}" upgrade --from-worktree "$CLONE" --activate --keep 3 | tee "$SANDBOX/upgrade2.json"
UPGRADE2_EXIT=${PIPESTATUS[0]}
set -e
set +e
"${RF[@]}" version status > "$SANDBOX/status2.json"
STATUS2_EXIT=$?
set -e
RECEIPT2="$(python3 -c "import json;print(json.load(open('$SANDBOX/upgrade2.json')).get('activation_receipt') or '')")"

say "ROLLBACK: the receipted rollback must restore the first candidate"
set +e
"${RF[@]}" upgrade rollback "$RECEIPT2" | tee "$SANDBOX/rollback.json"
ROLLBACK_EXIT=${PIPESTATUS[0]}
set -e
set +e
"${RF[@]}" version status > "$SANDBOX/status3.json"
STATUS3_EXIT=$?
set -e

say "The operator's REAL installation must be untouched"
if [[ "$(snapshot_real_home)" == "$REAL_HOME_BEFORE" ]]; then
  ok "real home unchanged (${ORIGINAL_HOME})"
else
  fail "the sandbox modified the operator's real installation under $ORIGINAL_HOME"
fi

say "Acceptance assertions (#274)"
python3 - "$HEAD_SHA" "$UPGRADE_EXIT" "$STATUS1_EXIT" "$SANDBOX/upgrade.json" "$SANDBOX/status1.json" \
        "$SECOND_SHA" "$UPGRADE2_EXIT" "$ROLLBACK_EXIT" "$SANDBOX/upgrade2.json" \
        "$SANDBOX/status2.json" "$SANDBOX/rollback.json" "$SANDBOX/status3.json" \
        "$STATUS2_EXIT" "$STATUS3_EXIT" <<'ASSERT'
import json
import sys

head, upgrade_exit, status_exit, upgrade_path, status_path = sys.argv[1:6]
second, upgrade2_exit, rollback_exit = sys.argv[6:9]
upgrade2_path, status2_path, rollback_path, status3_path = sys.argv[9:13]
status2_exit, status3_exit = sys.argv[13:15]
failures = []


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:  # noqa: BLE001 - report, do not mask
        failures.append(f"could not parse {path}: {exc}")
        return {}


upgrade, status = load(upgrade_path), load(status_path)


def want(label, actual, expected):
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


want("upgrade exit", upgrade_exit, "0")
want("upgrade.status", upgrade.get("status"), "activated")
want("upgrade.converged", upgrade.get("converged"), True)
want("upgrade.active_sha", upgrade.get("active_sha"), head)
want("upgrade.observed_sha", upgrade.get("observed_sha"), head)
want("status exit", status_exit, "0")
want("status.activation_converged", status.get("activation_converged"), True)
want("status.desired_commit", status.get("desired_commit"), head)
want("status.observed_commit", status.get("observed_commit"), head)
want("status.incomplete_activation", status.get("incomplete_activation"), None)

# --- second activation: the new candidate takes over, the first becomes `previous`
upgrade2, status2 = load(upgrade2_path), load(status2_path)
want("second upgrade exit", upgrade2_exit, "0")
want("upgrade2.status", upgrade2.get("status"), "activated")
want("upgrade2.converged", upgrade2.get("converged"), True)
want("upgrade2.active_sha", upgrade2.get("active_sha"), second)
want("upgrade2.observed_sha", upgrade2.get("observed_sha"), second)
want("upgrade2.previous_sha", upgrade2.get("previous_sha"), head)
want("status2 exit", status2_exit, "0")
want("status2.activation_converged", status2.get("activation_converged"), True)
want("status2.incomplete_activation", status2.get("incomplete_activation"), None)
want("status2.observed_commit", status2.get("observed_commit"), second)
want("status2.previous_commit", status2.get("previous_commit"), head)

# --- receipted rollback: the FIRST candidate is restored, and observed follows
rollback, status3 = load(rollback_path), load(status3_path)
want("rollback exit", rollback_exit, "0")
want("rollback.status", rollback.get("status"), "rolled_back")
want("rollback.converged", rollback.get("converged"), True)
want("rollback.active_sha", rollback.get("active_sha"), head)
want("rollback.observed_sha", rollback.get("observed_sha"), head)
want("status3 exit", status3_exit, "0")
want("status3.desired_commit", status3.get("desired_commit"), head)
want("status3.observed_commit", status3.get("observed_commit"), head)
want("status3.activation_converged", status3.get("activation_converged"), True)
want("status3.incomplete_activation", status3.get("incomplete_activation"), None)

if failures:
    print("\033[31m✗ acceptance assertions failed:\033[0m")
    for line in failures:
        print(f"    - {line}")
    raise SystemExit(1)
print("\033[32m✓ every acceptance assertion holds\033[0m")
ASSERT
