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

import json
import os
from pathlib import Path

from ...domain.activation import ActivationReceipt, ReleaseManifest
from ...domain.errors import ConfigError

_RELEASES = "releases"
_CURRENT = "current"
_PREVIOUS = "previous"
_MANIFEST = ".manifest.json"
_VENV_BIN = "venv/bin/rf"
_PATH_BIN_DIR = "~/.local/bin"


class RuntimeReleaseStore:
    """Own the on-disk release directory, symlinks, manifests, and receipts."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._releases = root / _RELEASES
        self._current = root / _CURRENT
        self._previous = root / _PREVIOUS
        self._receipts = root / "runtime" / "activation-receipts"

    # -- paths --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def release_path(self, commit_sha: str) -> Path:
        return self._releases / commit_sha

    def bin_launcher(self) -> Path:
        """The stable launcher shim path; resolves through ``current`` at run time."""
        return self._root / "bin" / "rf"

    def path_launcher(self) -> Path:
        """The PATH-visible stable launcher (``~/.local/bin/rf``)."""
        return Path(_PATH_BIN_DIR).expanduser() / "rf"

    def write_launcher_shim(self, *, force: bool = False) -> tuple[Path, ...]:
        """Provision the stable launcher(s) **once**; a no-op when already present.

        The shim is independent of any single commit -- it resolves through the
        ``current`` symlink -- so upgrades must not rewrite it. It is written to the
        release root's ``bin/rf`` and to the PATH location ``~/.local/bin/rf`` so
        ``rf`` on the user's PATH always runs the active release.
        """
        written: list[Path] = []
        for shim, target in (
            (self.bin_launcher(), self._root / _CURRENT / _VENV_BIN),
            (self.path_launcher(), self._root / _CURRENT / _VENV_BIN),
        ):
            if shim.exists() and not force:
                kind = _classify_shim(shim)
                if kind == "repoforge_stable":
                    # Already ours and release-independent: leave it untouched.
                    continue
                if kind == "unknown":
                    raise ConfigError(
                        f"LAUNCHER_PATH_OCCUPIED: {shim} exists and is not a RepoForge "
                        "launcher. Move it aside (or re-run with force) so `rf` on PATH "
                        "resolves to the active release."
                    )
                # A legacy uv-tool entry point: migrate it, otherwise the user keeps
                # invoking the old install instead of the active release.
            shim.parent.mkdir(parents=True, exist_ok=True)
            script = (
                "#!/bin/sh\n"
                "# RepoForge stable launcher. Provisioned once; resolves the active\n"
                "# release through the `current` symlink. Do not edit.\n"
                f'exec "{target}" "$@"\n'
            )
            tmp = shim.with_name(f".{shim.name}.tmp-{os.getpid()}")
            tmp.write_text(script, encoding="utf-8")
            tmp.chmod(0o755)
            os.replace(tmp, shim)
            written.append(shim)
        return tuple(written)

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

    # -- receipts -----------------------------------------------------------

    def write_receipt(self, receipt: ActivationReceipt) -> Path:
        """Persist a receipt with exclusive create, so one can never overwrite another."""
        path = self._receipts / f"{receipt.receipt_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n"
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ConfigError(
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
        if not self._receipts.is_dir():
            return []
        receipts: list[ActivationReceipt] = []
        for entry in sorted(self._receipts.glob("*.json")):
            try:
                raw = _read_json(entry)
            except ConfigError:
                # One unreadable receipt must not make the whole history -- and therefore
                # `rf version status` and receipt allocation -- fail.
                continue
            if raw is None:
                continue
            try:
                receipts.append(ActivationReceipt.from_dict(raw))
            except ValueError:
                continue
        return sorted(receipts, key=lambda receipt: receipt.receipt_id, reverse=True)

    def allocate_receipt_id(self, *, date_stamp: str) -> str:
        """Return the next ``act-<date_stamp>-NNN`` id not already on disk.

        Allocation is advisory: two concurrent activations can pick the same id, so
        ``write_receipt`` creates exclusively and callers retry (see ``next_receipt_id``).
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


_STABLE_MARKER = "RepoForge stable launcher"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


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
