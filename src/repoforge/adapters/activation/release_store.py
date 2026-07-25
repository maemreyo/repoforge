"""Immutable, A/B release layout with atomic symlink activation and retention GC.

Layout under the release root (default ``~/.local/share/repoforge``)::

    releases/<commit-sha>/        one immutable install per commit
    releases/<commit-sha>/.manifest.json
    current  -> releases/<commit-sha>   active generation (relative symlink)
    previous -> releases/<commit-sha>   last-good generation, kept warm for rollback
    runtime/activation-receipts/  append-only activation history

The `current`/`previous` swap is atomic: a temporary symlink is created and then
``os.replace``d over the existing link, which is a single atomic ``rename(2)`` on
POSIX. We never use ``ln -sf`` (unlink + symlink), which leaves a window with no
`current` at all.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ...domain.activation import (
    AGENT_SECRET_FILE_ENV_VAR,
    AGENT_SECRET_KEY,
    SUPERVISOR_AGENT_LABEL,
    ActivationReceipt,
    ReleaseManifest,
)
from ...domain.errors import ConfigError
from ..filesystem.atomic import atomic_write_bytes, atomic_write_text

_RELEASES = "releases"
_CURRENT = "current"
_PREVIOUS = "previous"
_MANIFEST = ".manifest.json"
_VENV_BIN = "venv/bin/rf"
_VENV_PYTHON = "venv/bin/python"
_WORKER_MODULE = "repoforge.interfaces.runtime.worker"
_PATH_BIN_DIR = "~/.local/bin"
_DEFAULT_AGENT_LABEL = SUPERVISOR_AGENT_LABEL
_AGENT_SECRET_ENV_VAR = AGENT_SECRET_FILE_ENV_VAR
# A credential file is one short line. A cap keeps a hostile or corrupt file from being
# read into memory, and any file over it is grammar-invalid by definition.
_AGENT_SECRET_MAX_BYTES = 8192


def _receipt_sort_key(receipt_id: str) -> tuple[int, int, str]:
    """Order receipts numerically: ``act-20260725-1000`` is newer than ``...-999``.

    A lexicographic compare would rank "999" above "1000" because it compares '9' to
    '1', which would pick the wrong latest receipt after the 999th activation in a day.
    """
    parts = receipt_id.split("-")
    if len(parts) != 3:
        return (0, 0, receipt_id)
    try:
        return (int(parts[1]), int(parts[2]), receipt_id)
    except ValueError:
        return (0, 0, receipt_id)


_AGENT_SECRET_KEY = AGENT_SECRET_KEY


@dataclass(frozen=True, slots=True)
class AgentSecretStatus:
    """Whether a durable agent secret exists AND is safe AND is usable."""

    exists: bool = False
    regular_file: bool = False
    owner_matches: bool = False
    mode_secure: bool = False
    key_present: bool = False
    value_nonempty: bool = False
    parse_valid: bool = False
    keys: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return all(
            (
                self.exists,
                self.regular_file,
                self.owner_matches,
                self.mode_secure,
                self.key_present,
                self.value_nonempty,
                self.parse_valid,
            )
        )

    def problems(self) -> tuple[str, ...]:
        """Human-readable reasons the secret cannot be trusted, for a refusal message."""
        if not self.exists:
            return ("no durable secret file exists",)
        reasons = []
        if not self.regular_file:
            reasons.append("the path is not a readable regular file (a symlink is never followed)")
        if not self.owner_matches:
            reasons.append("the file is owned by another user")
        if not self.mode_secure:
            reasons.append("the file is not mode 0600")
        if not self.key_present:
            reasons.append(f"{_AGENT_SECRET_KEY} is absent")
        elif not self.value_nonempty:
            reasons.append(f"{_AGENT_SECRET_KEY} is empty")
        if not self.parse_valid:
            reasons.append(
                f"the file is not exactly one `{_AGENT_SECRET_KEY}=<value>` line "
                "(it is read as data, never evaluated as shell)"
            )
        return tuple(reasons)


def inspect_agent_secret(path: Path) -> tuple[AgentSecretStatus, str]:
    """Validate and parse the credential file, returning its status and value.

    Every check runs against ONE file descriptor obtained with ``O_NOFOLLOW``, and the
    bytes are read from that same descriptor: a file swapped between the check and the
    read cannot be the file we validated. This is the *only* reader of the credential
    file, so the grammar the validator enforces is exactly the grammar the runtime
    consumes -- the two contracts cannot diverge.

    The grammar is deliberately a data format, not a shell fragment: exactly one
    ``CONTROL_PLANE_API_KEY=<value>`` line, with no quoting, substitution, or extra
    statements. Nothing here evaluates the content.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return AgentSecretStatus(), ""
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            # O_NOFOLLOW refused a symlink. It exists, and we will not follow it.
            return AgentSecretStatus(exists=True, regular_file=False), ""
        return AgentSecretStatus(exists=True), ""
    try:
        info = os.fstat(descriptor)
        owned = info.st_uid == os.getuid()
        if not stat.S_ISREG(info.st_mode):
            return AgentSecretStatus(exists=True, regular_file=False, owner_matches=owned), ""
        secure_mode = stat.S_IMODE(info.st_mode) == 0o600
        raw = _read_all(descriptor, limit=_AGENT_SECRET_MAX_BYTES + 1)
    except OSError:
        return AgentSecretStatus(exists=True, regular_file=True), ""
    finally:
        os.close(descriptor)
    key_present, secret, parse_valid = _parse_agent_secret(raw)
    return (
        AgentSecretStatus(
            exists=True,
            regular_file=True,
            owner_matches=owned,
            mode_secure=secure_mode,
            key_present=key_present,
            value_nonempty=bool(secret),
            parse_valid=parse_valid,
            # A name from the allowlist contract, never a name parsed out of the file:
            # `agent-status` prints this, so arbitrary file content must never reach it.
            keys=(_AGENT_SECRET_KEY,) if key_present else (),
        ),
        secret,
    )


def _read_all(descriptor: int, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = os.read(descriptor, min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _parse_agent_secret(raw: bytes) -> tuple[bool, str, bool]:
    """Parse the one-line data grammar. Returns ``(key_present, value, parse_valid)``.

    ``parse_valid`` is False for anything the grammar does not accept: extra lines, an
    extra or foreign key, a quoted or padded value, or non-UTF-8 bytes. An empty value is
    grammatically fine but not usable, and is reported that way so the operator gets
    "the key is empty" rather than "the file is malformed".
    """
    if len(raw) > _AGENT_SECRET_MAX_BYTES:
        return False, "", False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, "", False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        # Exactly one trailing newline is the canonical form this store writes.
        lines.pop()
    if len(lines) != 1:
        return False, "", False
    name, separator, value = lines[0].partition("=")
    if not separator or name != _AGENT_SECRET_KEY:
        return False, "", False
    if not value:
        return True, "", True
    # No quote stripping and no whitespace trimming: the value is taken verbatim, so what
    # the validator approves is byte-for-byte what the worker exports.
    if not _valid_secret_value(value):
        return True, "", False
    return True, value, True


def _valid_secret_value(value: str) -> bool:
    """A credential is one printable, whitespace-free token.

    Refusing whitespace is defence in depth rather than the primary protection (nothing
    evaluates this file any more): it also rejects shell-shaped content such as
    ``''; touch /tmp/x``, so a file hand-edited into a command sequence is reported as
    malformed instead of being carried anywhere as a "credential".
    """
    return bool(value) and value.isprintable() and not any(char.isspace() for char in value)


class ReceiptExistsError(ConfigError):
    """Raised when a receipt id is already taken, so the caller can re-allocate."""


@dataclass(frozen=True, slots=True)
class ReceiptHistory:
    """Readable activation receipts plus the ids that could not be parsed."""

    valid: tuple[ActivationReceipt, ...]
    unreadable: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.unreadable)

    @property
    def latest(self) -> ActivationReceipt | None:
        """The newest *readable* receipt, or None when a newer one is unreadable.

        Returns None when an unreadable receipt sorts above every readable one: the
        truthful answer is "unknown", never the older receipt.
        """
        if not self.valid:
            return None
        newest_valid = self.valid[0].receipt_id
        if any(
            _receipt_sort_key(candidate) > _receipt_sort_key(newest_valid)
            for candidate in self.unreadable
        ):
            return None
        return self.valid[0]


class RuntimeReleaseStore:
    """Own the on-disk release directory, symlinks, manifests, and receipts."""

    def __init__(self, root: Path, *, path_launcher: Path | None = None) -> None:
        self._root = root
        self._releases = root / _RELEASES
        self._current = root / _CURRENT
        self._previous = root / _PREVIOUS
        self._receipts = root / "runtime" / "activation-receipts"
        # The PATH-visible launcher lives OUTSIDE the release root, so it is only
        # provisioned when a caller explicitly opts in. A temporary release root must
        # never be able to rewrite the user's real ``~/.local/bin/rf``.
        self._path_launcher = path_launcher

    # -- paths --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def release_path(self, commit_sha: str) -> Path:
        return self._releases / commit_sha

    def bin_launcher(self) -> Path:
        """The stable launcher shim path; resolves through ``current`` at run time."""
        return self._root / "bin" / "rf"

    def path_launcher(self) -> Path | None:
        """The PATH-visible stable launcher, when this store is allowed to manage one."""
        return self._path_launcher

    @staticmethod
    def default_path_launcher() -> Path:
        """The conventional PATH launcher location (``~/.local/bin/rf``)."""
        return Path(_PATH_BIN_DIR).expanduser() / "rf"

    def agent_env_path(self) -> Path:
        """A ``0600`` DATA file the OS-resident supervisor reads before it starts.

        launchd starts jobs with no shell environment, so a secret exported in the
        operator's terminal is simply absent after logout or reboot. Putting it in the
        plist would leave it in a world-readable property list, so the durable source is
        an owner-only file.

        The file is never sourced by a shell. It holds exactly one
        ``CONTROL_PLANE_API_KEY=<value>`` line and is parsed as data by
        :func:`inspect_agent_secret` -- sourcing it would make every byte of a credential
        file executable code, so a preflight that merely *parsed* it would be validating a
        different contract from the one the runtime actually honours.
        """
        return self._root / "runtime" / "agent.env"

    def write_agent_env(self, values: dict[str, str]) -> Path:
        """Persist agent credentials, owner-only from the first byte and durable.

        Only the allowlisted key is accepted, and the value is written verbatim: the
        format carries no quoting, so there is nothing for a reader to unquote and nothing
        an operator can smuggle in that would change how it is interpreted.
        """
        path = self.agent_env_path()
        unsupported = sorted(set(values) - {_AGENT_SECRET_KEY})
        if unsupported:
            raise ConfigError(
                f"AGENT_ENV_KEY_UNSUPPORTED: the agent credential file carries only "
                f"{_AGENT_SECRET_KEY}; refusing {', '.join(unsupported)}"
            )
        if _AGENT_SECRET_KEY not in values:
            raise ConfigError(f"AGENT_ENV_KEY_MISSING: {_AGENT_SECRET_KEY} is required")
        value = values[_AGENT_SECRET_KEY]
        if not _valid_secret_value(value):
            raise ConfigError(
                f"AGENT_ENV_VALUE_INVALID: {_AGENT_SECRET_KEY} must be a single "
                "non-empty printable token containing no whitespace"
            )
        atomic_write_text(path, f"{_AGENT_SECRET_KEY}={value}\n", mode=0o600)
        return path

    def agent_secret_status(self) -> AgentSecretStatus:
        """Inspect the durable secret without ever returning its value.

        Every field is a separate invariant because "the name appears in the file" is not
        the same as "a usable secret is stored safely": a world-readable file, a symlink to
        somewhere else, another user's file, or an empty value all satisfy a name check
        while still being unsafe or guaranteed to fail at boot.
        """
        status, _ = inspect_agent_secret(self.agent_env_path())
        return status

    def load_agent_secret(self) -> str:
        """Return the credential, re-proving every security invariant right now.

        Called on each process start, so ``0600``/owner/regular-file/grammar are invariants
        at the trust boundary rather than metadata from whenever ``install-agent`` last
        ran. A file chmodded to 0644 or replaced by a symlink months after installation is
        refused at the next boot instead of being trusted forever.
        """
        path = self.agent_env_path()
        status, secret = inspect_agent_secret(path)
        if not status.usable:
            raise ConfigError(
                "AGENT_SECRET_UNUSABLE: refusing to start with an untrusted credential "
                f"file at {path} (" + "; ".join(status.problems()) + ")"
            )
        return secret

    def supervisor_agent_label(self) -> str:
        """The launchd label for THIS release root.

        Namespacing by root is a safety boundary, not cosmetics: with one fixed label a
        sandbox or dev release root would resolve to the operator's production job and
        ``launchctl kickstart -k`` would restart it. A namespaced label simply does not
        exist for a foreign root, so ``available()`` is False and the caller falls back to
        its own launcher instead of touching production. The default root keeps the plain
        label so existing installations are unaffected.
        """
        if self._root == self.default_release_root():
            return _DEFAULT_AGENT_LABEL
        digest = hashlib.sha256(str(self._root).encode("utf-8")).hexdigest()[:12]
        return f"{_DEFAULT_AGENT_LABEL}.{digest}"

    @staticmethod
    def default_release_root() -> Path:
        from ...domain.user_paths import default_release_root

        return default_release_root()

    def supervisor_launcher(self) -> Path:
        """The shim an OS process manager runs to *become* the supervisor worker."""
        return self._root / "bin" / "rf-supervisor"

    def write_supervisor_shim(self, *, force: bool = False) -> Path:
        """Provision a shim that ``exec``s the worker instead of spawning it.

        Running ``rf ... start`` under launchd would leave launchd owning a CLI wrapper
        whose *child* is the supervisor, so the pid launchd supervises is not the pid in
        the runtime record. Exec-ing the worker directly makes them the same process.
        """
        shim = self.supervisor_launcher()
        # Resolve `current` ONCE, here, and hand the concrete release down: the runtime
        # must publish an immutable identity. Exec-ing through the mutable `current`
        # symlink without capturing it would let a later swap re-attribute this process.
        script = (
            "#!/bin/sh\n"
            f"# {_STABLE_MARKER} RepoForge supervisor launcher. Execs the worker so the\n"
            "# process manager owns the supervisor pid directly. Do not edit.\n"
            f'root="{self._root}"\n'
            'target="$(readlink "$root/current")" || exit 1\n'
            'sha="${target##*/}"\n'
            '[ -n "$sha" ] || { echo "no active release" >&2; exit 1; }\n'
            "# launchd provides no shell environment, so the durable credential file is\n"
            "# the only place an OS-resident supervisor can obtain its secret. The file is\n"
            "# NOT sourced: sourcing it would execute whatever it contains, so the worker\n"
            "# is handed the PATH and opens, re-validates and parses it as data instead.\n"
            f'exec env {_AGENT_SECRET_ENV_VAR}="$root/runtime/agent.env" \\\n'
            '  REPOFORGE_RUNNING_RELEASE_SHA="$sha" \\\n'
            f'  "$root/{_RELEASES}/$sha/{_VENV_PYTHON}" -m {_WORKER_MODULE} "$@"\n'
        )
        self._write_shim_script(shim, script, force=force)
        return shim

    def write_internal_launcher_shim(self, *, force: bool = False) -> Path:
        """Provision ``<release-root>/bin/rf`` -- required *before* a restart can run.

        This is the launcher the activation restarter execs, so it must exist before the
        first restart, not after a successful one. It is release-independent (it resolves
        through the ``current`` symlink), so it is written once and left alone afterwards.
        """
        shim = self.bin_launcher()
        self._provision_shim(shim, force=force)
        return shim

    def install_path_launcher(self, *, force: bool = False) -> Path | None:
        """Provision the PATH-visible launcher, if this store manages one.

        Deliberately separate from the internal shim: it mutates a location outside the
        release root, so it only happens for a store constructed with an explicit
        ``path_launcher`` and only after an activation has converged.
        """
        shim = self._path_launcher
        if shim is None:
            return None
        self._provision_shim(shim, force=force)
        return shim

    def _provision_shim(self, shim: Path, *, force: bool) -> None:
        # The CLI shim also captures the release it resolved, so a runtime started through
        # `rf start` publishes the same immutable identity as one started by launchd.
        script = (
            "#!/bin/sh\n"
            f"# {_STABLE_MARKER} RepoForge stable launcher. Resolves the active release\n"
            "# through the `current` symlink. Provisioned once; do not edit.\n"
            f'root="{self._root}"\n'
            'target="$(readlink "$root/current")" || exit 1\n'
            'sha="${target##*/}"\n'
            '[ -n "$sha" ] || { echo "no active release" >&2; exit 1; }\n'
            'exec env REPOFORGE_RUNNING_RELEASE_SHA="$sha" \\\n'
            f'  "$root/{_RELEASES}/$sha/{_VENV_BIN}" "$@"\n'
        )
        self._write_shim_script(shim, script, force=force)

    def _write_shim_script(self, shim: Path, script: str, *, force: bool) -> None:
        """Write a shim once, migrating a legacy one and refusing unknown occupants."""
        if shim.exists() and not force:
            kind = _classify_shim(shim)
            if kind == "repoforge_stable":
                if shim.read_text(encoding="utf-8", errors="replace") == script:
                    # Already ours and pointing at THIS release root: leave it alone.
                    return
                # Marker present but the embedded target is stale (the release root was
                # moved, copied, or the shim was written for a different root), so the
                # shim would exec a path that is no longer correct. Rewrite it.
            elif kind == "unknown":
                raise ConfigError(
                    f"LAUNCHER_PATH_OCCUPIED: {shim} exists and is not a RepoForge "
                    "launcher. Move it aside (or re-run with force) so `rf` resolves to "
                    "the active release."
                )
            # A legacy uv-tool entry point: migrate it, otherwise the user keeps
            # invoking the old install instead of the active release.
        shim.parent.mkdir(parents=True, exist_ok=True)
        tmp = shim.with_name(f".{shim.name}.tmp-{os.getpid()}")
        tmp.write_text(script, encoding="utf-8")
        tmp.chmod(0o755)
        os.replace(tmp, shim)

    # -- manifests ----------------------------------------------------------

    def reserve_release(self, commit_sha: str, *, build_fingerprint: str) -> bool:
        """Claim ``releases/<sha>`` for a fresh install; ``False`` if already installed.

        Releases are immutable: an existing directory is never written over. When the
        same commit is already installed we only accept it if its manifest records the
        identical build fingerprint, so "already installed" can never silently mean
        "different bits under the same name".
        """
        release = self.release_path(commit_sha)
        if not release.exists():
            return True
        existing = self.read_manifest(commit_sha)
        if existing is None:
            raise ConfigError(
                f"RELEASE_DIR_UNCLAIMABLE: {release} exists without a manifest; "
                "remove it before reinstalling this commit"
            )
        if existing.build_fingerprint != build_fingerprint:
            raise ConfigError(
                f"RELEASE_FINGERPRINT_CONFLICT: {commit_sha} is already installed with a "
                f"different build fingerprint ({existing.build_fingerprint} != {build_fingerprint})"
            )
        return False

    def write_manifest(self, manifest: ReleaseManifest) -> None:
        release = self.release_path(manifest.commit_sha)
        if not release.is_dir():
            raise ConfigError(
                f"RELEASE_NOT_INSTALLED: cannot write manifest for missing release "
                f"{manifest.commit_sha}"
            )
        _atomic_write_json(release / _MANIFEST, manifest.to_dict())

    def read_manifest(self, commit_sha: str) -> ReleaseManifest | None:
        path = self.release_path(commit_sha) / _MANIFEST
        raw = _read_json(path)
        if raw is None:
            return None
        try:
            return ReleaseManifest.from_dict(raw)
        except ValueError as exc:
            raise ConfigError(f"RELEASE_MANIFEST_INVALID: {path}: {exc}") from exc

    def installed_shas(self) -> list[str]:
        if not self._releases.is_dir():
            return []
        return sorted(
            entry.name for entry in self._releases.iterdir() if (entry / _MANIFEST).is_file()
        )

    def list_releases(self) -> list[ReleaseManifest]:
        manifests = [self.read_manifest(sha) for sha in self.installed_shas()]
        found = [manifest for manifest in manifests if manifest is not None]
        return sorted(found, key=lambda manifest: manifest.built_at, reverse=True)

    # -- symlink state ------------------------------------------------------

    def current_sha(self) -> str | None:
        return self._read_link(self._current)

    def previous_sha(self) -> str | None:
        return self._read_link(self._previous)

    def _read_link(self, link: Path) -> str | None:
        if not link.is_symlink():
            return None
        target = os.readlink(link)
        return Path(target).name or None

    # -- activation ---------------------------------------------------------

    def swap_current(self, commit_sha: str) -> str | None:
        """Point `current` at ``commit_sha``; demote the old `current` to `previous`.

        Returns the commit `current` pointed at before the swap (``None`` if unset).
        """
        if not self.release_path(commit_sha).is_dir():
            raise ConfigError(f"RELEASE_NOT_INSTALLED: {commit_sha}")
        old = self.current_sha()
        if old is not None and old != commit_sha:
            self._atomic_symlink(old, self._previous)
        self._atomic_symlink(commit_sha, self._current)
        return old

    def rollback(self) -> str:
        """Swap `current` and `previous`. Returns the commit now active."""
        previous = self.previous_sha()
        if previous is None:
            raise ConfigError("NO_PREVIOUS_RELEASE: nothing to roll back to")
        if not self.release_path(previous).is_dir():
            raise ConfigError(f"PREVIOUS_RELEASE_MISSING: {previous}")
        current = self.current_sha()
        self._atomic_symlink(previous, self._current)
        if current is not None:
            self._atomic_symlink(current, self._previous)
        return previous

    def _atomic_symlink(self, commit_sha: str, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        # Relative target keeps the whole release root relocatable.
        target = Path(_RELEASES) / commit_sha
        tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(target)
        os.replace(tmp, link)

    # -- retention ----------------------------------------------------------

    def prune(self, *, keep: int) -> list[str]:
        """Remove old releases, retaining `current`, `previous`, and the newest ``keep``.

        Returns the commit shas that were removed.
        """
        if keep < 0:
            raise ConfigError("RETENTION_INVALID: keep must be >= 0")
        protected: set[str] = set()
        for pinned in (self.current_sha(), self.previous_sha()):
            if pinned is not None:
                protected.add(pinned)
        ordered = [manifest.commit_sha for manifest in self.list_releases()]
        protected.update(ordered[:keep])
        removed: list[str] = []
        for sha in self.installed_shas():
            if sha in protected:
                continue
            _remove_tree(self.release_path(sha))
            removed.append(sha)
        return removed

    # -- in-flight journal --------------------------------------------------

    def journal_path(self) -> Path:
        return self._root / "runtime" / "activation-in-flight.json"

    def begin_activation(self, *, receipt_id: str, from_sha: str | None, to_sha: str) -> None:
        """Record an activation attempt BEFORE any side effect, so a crash is detectable.

        Receipts are immutable and only written at a terminal outcome, so a process that
        dies between the symlink swap and the receipt would leave `current` moved with no
        evidence that an activation was ever in progress. This journal closes that gap:
        it is written first and cleared only when a terminal receipt exists.
        """
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"receipt_id": receipt_id, "from_sha": from_sha, "to_sha": to_sha, "stage": "prepared"},
            sort_keys=True,
            indent=2,
        )
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            # Exclusive create, never replace: overwriting would destroy the record of an
            # earlier activation that never terminalized, which is the only evidence of
            # what the last-known-good release was before the crash.
            raise ConfigError(
                "ACTIVATION_IN_FLIGHT: an earlier activation has not terminalized; "
                f"reconcile {path} before starting another"
            ) from exc
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record_activation_stage(self, stage: str) -> None:
        """Advance the in-flight journal; a no-op when no activation is in flight."""
        raw = _read_json(self.journal_path())
        if not isinstance(raw, dict):
            return
        _atomic_write_json(self.journal_path(), {**raw, "stage": stage})

    def read_in_flight_activation(self) -> dict[str, object] | None:
        """Return the in-flight activation, if a previous attempt did not terminalize."""
        raw = _read_json(self.journal_path())
        return raw if isinstance(raw, dict) else None

    def end_activation(self) -> None:
        """Clear the journal once a terminal receipt has been durably written."""
        self.journal_path().unlink(missing_ok=True)

    # -- receipts -----------------------------------------------------------

    def write_receipt(self, receipt: ActivationReceipt) -> Path:
        """Persist a receipt with exclusive create, so one can never overwrite another."""
        path = self._receipts / f"{receipt.receipt_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n"
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ReceiptExistsError(
                f"RECEIPT_EXISTS: {receipt.receipt_id} already exists; receipts are immutable"
            ) from exc
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def read_receipt(self, receipt_id: str) -> ActivationReceipt | None:
        raw = _read_json(self._receipts / f"{receipt_id}.json")
        if raw is None:
            return None
        try:
            return ActivationReceipt.from_dict(raw)
        except ValueError as exc:
            raise ConfigError(f"RECEIPT_INVALID: {receipt_id}: {exc}") from exc

    def list_receipts(self) -> list[ActivationReceipt]:
        return list(self.receipt_history().valid)

    def receipt_history(self) -> ReceiptHistory:
        """Return readable receipts plus the ids that could not be parsed.

        Skipping a bad file keeps one corrupt receipt from breaking every command, but
        callers must know when history is incomplete: if the *newest* receipt is the
        unreadable one, reporting the previous receipt as "latest" would be a lie.
        """
        if not self._receipts.is_dir():
            return ReceiptHistory(valid=(), unreadable=())
        valid: list[ActivationReceipt] = []
        unreadable: list[str] = []
        for entry in sorted(self._receipts.glob("*.json")):
            try:
                raw = _read_json(entry)
            except ConfigError:
                unreadable.append(entry.stem)
                continue
            if raw is None:
                continue
            try:
                valid.append(ActivationReceipt.from_dict(raw))
            except ValueError:
                unreadable.append(entry.stem)
        return ReceiptHistory(
            valid=tuple(
                sorted(
                    valid, key=lambda receipt: _receipt_sort_key(receipt.receipt_id), reverse=True
                )
            ),
            unreadable=tuple(sorted(unreadable, key=_receipt_sort_key, reverse=True)),
        )

    def allocate_receipt_id(self, *, date_stamp: str) -> str:
        """Return the next ``act-<date_stamp>-NNN`` id not already on disk.

        Allocation is advisory: two concurrent activations can pick the same id, so
        ``write_receipt`` creates exclusively and raises :class:`ReceiptExistsError`,
        which callers retry against a freshly allocated id.
        """
        taken = self._taken_receipt_ids()
        index = 1
        while True:
            candidate = f"act-{date_stamp}-{index:03d}"
            if candidate not in taken:
                return candidate
            index += 1

    def _taken_receipt_ids(self) -> set[str]:
        """Every id present on disk, including receipts this version cannot parse."""
        if not self._receipts.is_dir():
            return set()
        return {entry.stem for entry in self._receipts.glob("*.json")}


_STABLE_MARKER = "repoforge-launcher:v1"


def _classify_shim(path: Path) -> str:
    """Classify an existing launcher: ours, a legacy uv-tool shim, or unknown."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    if _STABLE_MARKER in text:
        return "repoforge_stable"
    # uv tool installs write a console-script wrapper importing the CLI entry point.
    if "repoforge" in text and ("interfaces.cli" in text or "console_scripts" in text):
        return "legacy_uv_tool"
    if not text.startswith("#!"):
        # A binary or unrecognized file: never clobber it silently.
        return "unknown"
    return "unknown"


def _atomic_write_json(path: Path, payload: object) -> None:
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    # Release metadata lives in directories that are readable by design, so only the file
    # mode is tightened here -- the durability rules are the shared writer's.
    atomic_write_bytes(path, data, mode=0o600, dir_mode=0o755)


def _read_json(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    return parsed


def _remove_tree(path: Path) -> None:
    import shutil

    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
