"""Read-only repository toolchain detection for onboarding profile proposals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..domain.repository_proposal import ProposalConfidence, ProposedProfile


@dataclass(frozen=True, slots=True)
class DetectedVerificationProfile:
    profile_id: str
    argv: tuple[str, ...]
    provenance: tuple[str, ...]
    verification: bool
    timeout_seconds: int
    network_policy: str
    mutability: str
    requires_network_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class _Marker:
    path: str
    exists: bool


class VerificationProfileDetector:
    def detect(self, root: Path) -> tuple[DetectedVerificationProfile, ...]:
        markers = self._markers(root)
        candidates = [
            *self._python_profiles(markers),
            *self._node_profiles(root, markers),
            *self._go_profiles(markers),
            *self._cargo_profiles(markers),
            *self._make_profiles(root, markers),
        ]
        return tuple(candidates)

    def proposed_profiles(
        self, root: Path, *, include_dependency_setup: bool
    ) -> tuple[ProposedProfile, ...]:
        return tuple(
            ProposedProfile(
                candidate.profile_id,
                f"Detected from {', '.join(candidate.provenance)}",
                candidate.verification,
                (candidate.argv,),
                ProposalConfidence.HIGH,
                ", ".join(candidate.provenance),
                timeout_seconds=candidate.timeout_seconds,
            )
            for candidate in self.detect(root)
            if include_dependency_setup or not candidate.requires_network_confirmation
        )

    @staticmethod
    def _markers(root: Path) -> dict[str, _Marker]:
        return {
            name: _Marker(name, (root / name).is_file())
            for name in (
                "pyproject.toml",
                "uv.lock",
                "package.json",
                "package-lock.json",
                "pnpm-lock.yaml",
                "yarn.lock",
                "go.mod",
                "Cargo.toml",
                "Makefile",
            )
        }

    @staticmethod
    def _profile(
        profile_id: str,
        argv: tuple[str, ...],
        provenance: tuple[str, ...],
        *,
        verification: bool,
        network_policy: str = "local_only",
        mutability: str = "read_only",
        requires_network_confirmation: bool = False,
    ) -> DetectedVerificationProfile:
        return DetectedVerificationProfile(
            profile_id,
            argv,
            provenance,
            verification,
            1_800,
            network_policy,
            mutability,
            requires_network_confirmation,
        )

    def _python_profiles(
        self, markers: dict[str, _Marker]
    ) -> tuple[DetectedVerificationProfile, ...]:
        if not markers["pyproject.toml"].exists or not markers["uv.lock"].exists:
            return ()
        provenance = ("pyproject.toml", "uv.lock")
        return (
            self._profile(
                "python-setup",
                ("uv", "sync", "--extra", "dev"),
                provenance,
                verification=False,
                network_policy="restricted",
                mutability="workspace_write",
                requires_network_confirmation=True,
            ),
            self._profile(
                "python-test",
                ("uv", "run", "--extra", "dev", "pytest", "-q"),
                provenance,
                verification=True,
            ),
        )

    def _node_profiles(
        self, root: Path, markers: dict[str, _Marker]
    ) -> tuple[DetectedVerificationProfile, ...]:
        if not markers["package.json"].exists:
            return ()
        manager, lockfile = self._node_manager(markers)
        package = self._package_json(root / "package.json")
        scripts = package.get("scripts")
        test_script = scripts.get("test") if isinstance(scripts, dict) else None
        test_argv = (manager, "run", "test") if isinstance(test_script, str) else (manager, "test")
        profiles: list[DetectedVerificationProfile] = []
        provenance = ("package.json", lockfile) if lockfile else ("package.json",)
        if lockfile:
            install = {
                "npm": ("npm", "ci"),
                "pnpm": ("pnpm", "install", "--frozen-lockfile"),
                "yarn": ("yarn", "install", "--immutable"),
            }[manager]
            profiles.append(
                self._profile(
                    "node-setup",
                    install,
                    provenance,
                    verification=False,
                    network_policy="restricted",
                    mutability="workspace_write",
                    requires_network_confirmation=True,
                )
            )
        profiles.append(self._profile("node-test", test_argv, provenance, verification=True))
        return tuple(profiles)

    @staticmethod
    def _node_manager(markers: dict[str, _Marker]) -> tuple[str, str | None]:
        for manager, lockfile in (
            ("pnpm", "pnpm-lock.yaml"),
            ("yarn", "yarn.lock"),
            ("npm", "package-lock.json"),
        ):
            if markers[lockfile].exists:
                return manager, lockfile
        return "npm", None

    @staticmethod
    def _package_json(path: Path) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _go_profiles(self, markers: dict[str, _Marker]) -> tuple[DetectedVerificationProfile, ...]:
        if not markers["go.mod"].exists:
            return ()
        provenance = ("go.mod",)
        return (
            self._profile("go-build", ("go", "build", "./..."), provenance, verification=True),
            self._profile("go-test", ("go", "test", "./..."), provenance, verification=True),
        )

    def _cargo_profiles(
        self, markers: dict[str, _Marker]
    ) -> tuple[DetectedVerificationProfile, ...]:
        if not markers["Cargo.toml"].exists:
            return ()
        provenance = ("Cargo.toml",)
        return (
            self._profile("cargo-build", ("cargo", "build"), provenance, verification=True),
            self._profile("cargo-test", ("cargo", "test"), provenance, verification=True),
        )

    def _make_profiles(
        self, root: Path, markers: dict[str, _Marker]
    ) -> tuple[DetectedVerificationProfile, ...]:
        if not markers["Makefile"].exists:
            return ()
        try:
            lines = (root / "Makefile").read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        targets = {
            line.split(":", 1)[0] for line in lines if ":" in line and not line.startswith("\t")
        }
        return tuple(
            self._profile(f"make-{target}", ("make", target), ("Makefile",), verification=True)
            for target in ("check", "test")
            if target in targets
        )
