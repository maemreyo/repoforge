"""Deterministic inspect -> decide -> approve repository proposal model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .config_generation import CapabilityDeltaKind
from .repository_detection import RepositoryFacts


class ProposalConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    BLOCKED = "blocked"


class EnrollmentMode(str, Enum):
    READ_ONLY = "read_only"
    STANDARD = "standard"
    STRICT = "strict"


@dataclass(frozen=True, slots=True)
class DetectionFinding:
    code: str
    severity: str
    message: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequiredDecision:
    code: str
    prompt: str
    choices: tuple[str, ...]
    security_relevant: bool = True


@dataclass(frozen=True, slots=True)
class ProposedProfile:
    name: str
    description: str
    verification: bool
    commands: tuple[tuple[str, ...], ...]
    confidence: ProposalConfidence
    source: str
    working_directory: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class RepositoryPolicyProposal:
    mode: EnrollmentMode
    remote: str | None
    default_base: str | None
    allowed_base_branches: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    profiles: tuple[ProposedProfile, ...]
    publish_enabled: bool
    max_changed_files: int
    max_diff_lines: int
    max_total_changed_bytes: int


@dataclass(frozen=True, slots=True)
class RepositoryProposal:
    proposal_id: str
    facts_fingerprint: str
    repo_id: str
    path: str
    confidence: ProposalConfidence
    findings: tuple[DetectionFinding, ...]
    required_decisions: tuple[RequiredDecision, ...]
    policy: RepositoryPolicyProposal
    capability_delta: CapabilityDeltaKind = CapabilityDeltaKind.EXPANSION

    def canonical_dict(self) -> dict[str, Any]:
        return asdict(self)


SAFE_DENIED_PATHS = (
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*secret*",
    "**/*credential*",
    ".github/workflows/**",
)

_LOCK_MANAGERS = {
    "pnpm-lock.yaml": "pnpm",
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "Pipfile.lock": "pipenv",
}


def _manager_for_lock(path: str) -> str | None:
    return _LOCK_MANAGERS.get(path.rsplit("/", 1)[-1])


def _profile_candidates(
    facts: RepositoryFacts,
    *,
    package_manager: str | None = None,
    working_directory: str | None = None,
    include_dependency_setup: bool = False,
    include_autofix: bool = False,
) -> tuple[ProposedProfile, ...]:
    commands: list[ProposedProfile] = []
    scripts = set(facts.scripts)
    manager = package_manager
    if working_directory is not None:
        scoped_scripts: set[str] = set()
        scoped_managers: set[str] = set()
        prefix = working_directory.rstrip("/") + "/"
        for manifest in facts.manifests:
            parent = manifest.path.rsplit("/", 1)[0] if "/" in manifest.path else "."
            if parent == working_directory or manifest.path.startswith(prefix):
                scoped_scripts.update(manifest.scripts)
                if manifest.package_manager:
                    scoped_managers.add(manifest.package_manager)
        if scoped_scripts:
            scripts = scoped_scripts
        if manager is None and len(scoped_managers) == 1:
            manager = next(iter(scoped_managers))
    if manager is None:
        manifest_managers = {
            item.package_manager for item in facts.manifests if item.package_manager
        }
        lock_managers = {item for path in facts.lockfiles if (item := _manager_for_lock(path))}
        candidates = manifest_managers | lock_managers
        if len(candidates) == 1:
            manager = next(iter(candidates))
    if manager and scripts:

        def cmd(name: str) -> tuple[str, ...]:
            return (manager, "run", name)

        lock_managers = {item for path in facts.lockfiles if (item := _manager_for_lock(path))}
        if include_dependency_setup and manager in lock_managers:
            install_commands = {
                "pnpm": ("pnpm", "install", "--frozen-lockfile"),
                "npm": ("npm", "ci"),
                "yarn": ("yarn", "install", "--immutable"),
                "bun": ("bun", "install", "--frozen-lockfile"),
            }
            install = install_commands.get(manager)
            if install is not None:
                commands.append(
                    ProposedProfile(
                        "setup",
                        "Install dependencies exactly from the reviewed lockfile",
                        False,
                        (install,),
                        ProposalConfidence.HIGH,
                        "package-manager lockfile",
                        working_directory,
                    )
                )
        if include_autofix and "fix" in scripts:
            commands.append(
                ProposedProfile(
                    "fix",
                    "Run the repository's explicitly approved autofix script",
                    False,
                    (cmd("fix"),),
                    ProposalConfidence.MEDIUM,
                    "package.json script",
                    working_directory,
                )
            )

        quick_names = (
            ("check",)
            if "check" in scripts
            else tuple(x for x in ("lint", "typecheck") if x in scripts)
        )
        if quick_names:
            commands.append(
                ProposedProfile(
                    "quick",
                    "Fast static checks",
                    True,
                    tuple(cmd(x) for x in quick_names),
                    ProposalConfidence.HIGH,
                    "package.json scripts",
                    working_directory,
                )
            )
        if "test" in scripts:
            commands.append(
                ProposedProfile(
                    "test",
                    "Repository test suite",
                    True,
                    (cmd("test"),),
                    ProposalConfidence.HIGH,
                    "package.json scripts",
                    working_directory,
                )
            )
        full = tuple(
            dict.fromkeys(
                [*quick_names, *(x for x in ("test", "test:preflight", "build") if x in scripts)]
            )
        )
        if full:
            commands.append(
                ProposedProfile(
                    "full",
                    "Full verification gate",
                    True,
                    tuple(cmd(x) for x in full),
                    ProposalConfidence.HIGH,
                    "package.json scripts",
                    working_directory,
                )
            )
    targets = set(facts.make_targets)
    if targets:
        if include_dependency_setup:
            setup_target = next(
                (name for name in ("bootstrap", "setup", "install") if name in targets), None
            )
            if setup_target:
                commands.append(
                    ProposedProfile(
                        "setup",
                        f"Run the explicitly approved make {setup_target} target",
                        False,
                        (("make", setup_target),),
                        ProposalConfidence.MEDIUM,
                        "Makefile target",
                        working_directory,
                    )
                )
        if include_autofix and "fix" in targets:
            commands.append(
                ProposedProfile(
                    "fix",
                    "Run the explicitly approved make fix target",
                    False,
                    (("make", "fix"),),
                    ProposalConfidence.MEDIUM,
                    "Makefile target",
                    working_directory,
                )
            )
        if "check" in targets or "verify" in targets:
            target = "verify" if "verify" in targets else "check"
            commands.append(
                ProposedProfile(
                    "full",
                    f"Run make {target}",
                    True,
                    (("make", target),),
                    ProposalConfidence.HIGH,
                    "Makefile target",
                    working_directory,
                )
            )
        elif "test" in targets:
            commands.append(
                ProposedProfile(
                    "test",
                    "Run make test",
                    True,
                    (("make", "test"),),
                    ProposalConfidence.MEDIUM,
                    "Makefile target",
                    working_directory,
                )
            )
    ecosystems = {item.ecosystem for item in facts.manifests}
    if "python" in ecosystems and not commands:
        commands.append(
            ProposedProfile(
                "test",
                "Run Python tests",
                True,
                (("python", "-m", "pytest", "-q"),),
                ProposalConfidence.MEDIUM,
                "pyproject.toml",
                working_directory,
            )
        )
        commands.append(
            ProposedProfile(
                "full",
                "Run Python tests",
                True,
                (("python", "-m", "pytest", "-q"),),
                ProposalConfidence.MEDIUM,
                "pyproject.toml",
                working_directory,
            )
        )
    if "rust" in ecosystems and not commands:
        commands.append(
            ProposedProfile(
                "full",
                "Rust full verification",
                True,
                (
                    ("cargo", "fmt", "--check"),
                    ("cargo", "clippy", "--all-targets", "--", "-D", "warnings"),
                    ("cargo", "test"),
                ),
                ProposalConfidence.HIGH,
                "Cargo.toml",
                working_directory,
            )
        )
    if "go" in ecosystems and not commands:
        commands.append(
            ProposedProfile(
                "full",
                "Go full verification",
                True,
                (("go", "vet", "./..."), ("go", "test", "./...")),
                ProposalConfidence.HIGH,
                "go.mod",
                working_directory,
            )
        )
    unique: dict[str, ProposedProfile] = {}
    for profile in commands:
        current = unique.get(profile.name)
        if current is None or profile.confidence is ProposalConfidence.HIGH:
            unique[profile.name] = profile
    return tuple(unique[name] for name in sorted(unique))


def _decision(decisions: dict[str, str], code: str, choices: tuple[str, ...]) -> str | None:
    value = decisions.get(code)
    if value is not None and value not in choices:
        raise ValueError(f"Invalid decision {code}={value!r}; choose one of {', '.join(choices)}")
    return value


def _path_list(value: str, *, key: str) -> tuple[str, ...]:
    paths: list[str] = []
    for raw in value.split(","):
        item = raw.strip().replace("\\", "/")
        if not item or item.startswith(("/", "-")) or ".." in item.split("/"):
            raise ValueError(f"Invalid {key} path override: {raw!r}")
        paths.append(item)
    return tuple(dict.fromkeys(paths))


def _positive_override(overrides: dict[str, str], key: str, default: int, *, maximum: int) -> int:
    raw = overrides.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Policy override {key} must be an integer") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"Policy override {key} must be between 1 and {maximum}")
    return value


def build_repository_proposal(
    facts: RepositoryFacts,
    *,
    decisions: dict[str, str] | None = None,
    template: EnrollmentMode = EnrollmentMode.STANDARD,
    overrides: dict[str, str] | None = None,
    detected_profiles: tuple[ProposedProfile, ...] | None = None,
) -> RepositoryProposal:
    decisions = decisions or {}
    overrides = overrides or {}
    known_decisions = {
        "publish_remote",
        "default_base",
        "package_manager",
        "monorepo_scope",
        "submodules",
        "lfs",
        "repository_budget",
        "existing_policy",
        "existing_worktrees",
        "publishing_access",
        "dependency_install",
        "autofix",
        "risky_commands",
    }
    unknown_decisions = sorted(set(decisions) - known_decisions)
    if unknown_decisions:
        raise ValueError(f"Unknown repository decisions: {unknown_decisions}")
    known_overrides = {
        "allowed_paths",
        "denied_paths_add",
        "max_changed_files",
        "max_diff_lines",
        "max_total_changed_bytes",
        "read_only",
        "working_directory",
    }
    unknown_overrides = sorted(set(overrides) - known_overrides)
    if unknown_overrides:
        raise ValueError(f"Unknown policy overrides: {unknown_overrides}")
    findings: list[DetectionFinding] = []
    required: list[RequiredDecision] = []
    remote_names = tuple(item.name for item in facts.remotes)
    remote_choice = decisions.get("publish_remote")
    if remote_choice is not None and remote_choice not in (*remote_names, "read_only"):
        raise ValueError(
            f"Invalid publish_remote={remote_choice!r}; choose one of {(*remote_names, 'read_only')}"
        )
    remote: str | None = None if remote_choice == "read_only" else remote_choice
    if remote_choice is None:
        if len(remote_names) == 1:
            remote = remote_names[0]
        elif remote_names:
            required.append(
                RequiredDecision(
                    "publish_remote",
                    "Choose the only remote RepoForge may push to.",
                    (*remote_names, "read_only"),
                )
            )
        else:
            required.append(
                RequiredDecision(
                    "publish_remote",
                    "No Git remote is configured. Enroll read-only or configure a remote first.",
                    ("read_only",),
                )
            )
            findings.append(DetectionFinding("NO_REMOTE", "warning", "No Git remote was detected."))
    publish_enabled = remote is not None
    github_remote = any(
        "github.com" in (value or "").lower()
        for remote_fact in facts.remotes
        for value in (remote_fact.fetch_url, remote_fact.push_url)
    )
    publishing_choice = _decision(
        decisions,
        "publishing_access",
        ("local_only", "read_only", "block"),
    )
    if github_remote and facts.github_authenticated is not True:
        findings.append(
            DetectionFinding(
                "GITHUB_AUTH_UNAVAILABLE",
                "warning",
                "GitHub publishing authentication is unavailable or unverified.",
            )
        )
        if publishing_choice is None:
            required.append(
                RequiredDecision(
                    "publishing_access",
                    "Choose local-only work, full read-only enrollment, or block until GitHub authentication is configured.",
                    ("local_only", "read_only", "block"),
                )
            )
        publish_enabled = False
    base_choices = tuple(dict.fromkeys((*facts.default_branch_candidates, "read_only")))
    base_choice = decisions.get("default_base")
    if base_choice is not None and base_choice not in base_choices:
        raise ValueError(f"Invalid default_base={base_choice!r}; choose one of {base_choices}")
    base: str | None = None if base_choice == "read_only" else base_choice
    if base_choice is None:
        if len(facts.default_branch_candidates) == 1:
            base = facts.default_branch_candidates[0]
        else:
            required.append(
                RequiredDecision(
                    "default_base",
                    "Choose the allowlisted base branch or enroll read-only.",
                    base_choices or ("read_only",),
                )
            )

    declared_managers = {value.split("@", 1)[0] for value in facts.toolchain_declarations if value}
    lock_managers = {manager for path in facts.lockfiles if (manager := _manager_for_lock(path))}
    package_managers = tuple(sorted(lock_managers | declared_managers))
    package_manager_choice = (
        _decision(decisions, "package_manager", package_managers) if package_managers else None
    )
    if len(package_managers) > 1 and package_manager_choice is None:
        required.append(
            RequiredDecision(
                "package_manager",
                "Multiple package managers were found. Choose the authoritative toolchain.",
                package_managers,
            )
        )
        findings.append(
            DetectionFinding(
                "MULTIPLE_LOCKFILES",
                "warning",
                "Conflicting package-manager evidence requires review.",
                tuple(sorted((*facts.lockfiles, *facts.toolchain_declarations))),
            )
        )
    elif len(package_managers) == 1 and package_manager_choice is None:
        package_manager_choice = package_managers[0]

    dependency_install = _decision(
        decisions,
        "dependency_install",
        ("include_non_verification", "exclude", "block"),
    )
    has_install_candidate = bool(
        package_manager_choice
        and package_manager_choice
        in {item for path in facts.lockfiles if (item := _manager_for_lock(path))}
    ) or bool(set(facts.make_targets).intersection({"bootstrap", "setup", "install"}))
    if has_install_candidate and dependency_install is None:
        required.append(
            RequiredDecision(
                "dependency_install",
                "Dependency setup may access the network. Include it only as a non-verification action, exclude it, or block enrollment.",
                ("include_non_verification", "exclude", "block"),
            )
        )

    autofix = _decision(
        decisions,
        "autofix",
        ("include_non_verification", "exclude", "block"),
    )
    has_autofix = "fix" in facts.scripts or "fix" in facts.make_targets
    if has_autofix and autofix is None:
        required.append(
            RequiredDecision(
                "autofix",
                "Autofix commands mutate repository content. Include as a non-verification action, exclude, or block enrollment.",
                ("include_non_verification", "exclude", "block"),
            )
        )

    risky_names = tuple(
        sorted(
            name
            for name in set((*facts.scripts, *facts.make_targets))
            if any(
                token in name.lower().replace(":", "-").replace("_", "-").split("-")
                for token in (
                    "deploy",
                    "release",
                    "publish",
                    "migrate",
                    "database",
                    "db",
                    "seed",
                    "destroy",
                    "delete",
                    "production",
                    "prod",
                )
            )
        )
    )
    risky_commands = _decision(decisions, "risky_commands", ("exclude", "block"))
    if risky_names:
        findings.append(
            DetectionFinding(
                "RISKY_COMMANDS_EXCLUDED",
                "warning",
                "Potential deploy/release/database/destructive commands were detected and are never inferred as profiles.",
                risky_names,
            )
        )
        if risky_commands is None:
            required.append(
                RequiredDecision(
                    "risky_commands",
                    "Confirm that risky discovered commands remain excluded, or block enrollment.",
                    ("exclude", "block"),
                )
            )

    monorepo_scope = _decision(decisions, "monorepo_scope", ("root", "scoped", "read_only"))
    if len(facts.manifests) > 1 or facts.workspace_packages:
        if monorepo_scope is None:
            required.append(
                RequiredDecision(
                    "monorepo_scope",
                    "Choose root-wide or scoped verification for this repository.",
                    ("root", "scoped", "read_only"),
                )
            )
        findings.append(
            DetectionFinding(
                "MONOREPO",
                "info",
                "Multiple manifests or workspace packages were detected.",
                tuple(item.path for item in facts.manifests),
            )
        )
    submodule_choice = _decision(decisions, "submodules", ("block", "read_only_parent"))
    if facts.submodules and submodule_choice is None:
        required.append(
            RequiredDecision(
                "submodules",
                "Submodules are not writable through RepoForge. Choose enrollment behavior.",
                ("block", "read_only_parent"),
            )
        )
    lfs_choice = _decision(decisions, "lfs", ("read_only", "block"))
    if facts.lfs_tracked and lfs_choice is None:
        required.append(
            RequiredDecision(
                "lfs",
                "Git LFS content was detected. Choose bounded read-only or block enrollment.",
                ("read_only", "block"),
            )
        )
    budget_choice = _decision(
        decisions, "repository_budget", ("keep_defaults", "scoped_paths", "read_only")
    )
    if (
        facts.scan_truncated or facts.large_file_count or facts.total_tracked_bytes > 1_000_000_000
    ) and budget_choice is None:
        required.append(
            RequiredDecision(
                "repository_budget",
                "The repository exceeds default scan budgets. Choose a safe scope.",
                ("keep_defaults", "scoped_paths", "read_only"),
            )
        )
        findings.append(
            DetectionFinding(
                "LARGE_REPOSITORY",
                "warning",
                "Large repository requires explicit scope/budget review.",
                (str(facts.tracked_file_count), str(facts.total_tracked_bytes)),
            )
        )
    if facts.detached:
        findings.append(
            DetectionFinding(
                "DETACHED_HEAD",
                "warning",
                "Source checkout is detached; a base branch must be selected.",
            )
        )
    if facts.shallow:
        findings.append(
            DetectionFinding(
                "SHALLOW_CLONE", "warning", "Shallow history may limit base and commit analysis."
            )
        )
    existing_policy_choice = _decision(
        decisions,
        "existing_policy",
        ("preserve_read_only", "replace", "block"),
    )
    if facts.policy_files:
        findings.append(
            DetectionFinding(
                "EXISTING_REPOFORGE_POLICY",
                "warning",
                "Existing RepoForge policy metadata requires an explicit preserve/replace decision; automatic merge is intentionally unsupported.",
                facts.policy_files,
            )
        )
        if existing_policy_choice is None:
            required.append(
                RequiredDecision(
                    "existing_policy",
                    "Choose whether to preserve the existing policy read-only, replace it with this reviewed proposal, or block enrollment.",
                    ("preserve_read_only", "replace", "block"),
                )
            )
    worktree_choice = _decision(
        decisions,
        "existing_worktrees",
        ("use_new_isolated", "read_only", "block"),
    )
    if len(facts.existing_worktrees) > 1:
        findings.append(
            DetectionFinding(
                "EXISTING_WORKTREES",
                "warning",
                "Additional Git worktrees already exist; RepoForge will never reuse or mutate them.",
                facts.existing_worktrees,
            )
        )
        if worktree_choice is None:
            required.append(
                RequiredDecision(
                    "existing_worktrees",
                    "Choose whether RepoForge may create a new isolated worktree, enroll read-only, or block.",
                    ("use_new_isolated", "read_only", "block"),
                )
            )
    if facts.symlink_count:
        findings.append(
            DetectionFinding(
                "TRACKED_SYMLINKS",
                "warning",
                "Tracked symlinks were detected; reads and mutations through symlinks remain blocked.",
                (str(facts.symlink_count),),
            )
        )
    if facts.binary_file_count:
        findings.append(
            DetectionFinding(
                "BINARY_CONTENT",
                "info",
                "Binary files were detected and remain unavailable to text mutation tools.",
                (str(facts.binary_file_count),),
            )
        )
    working_directory = overrides.get("working_directory")
    if working_directory is not None:
        working_directory = _path_list(working_directory, key="working_directory")[0]
        if "," in overrides["working_directory"]:
            raise ValueError("working_directory accepts exactly one relative directory")
    if monorepo_scope == "scoped" and working_directory is None:
        required.append(
            RequiredDecision(
                "working_directory_override",
                "Provide --policy-override working_directory=relative/path for scoped profiles.",
                ("required",),
            )
        )
    profiles = detected_profiles or _profile_candidates(
        facts,
        package_manager=package_manager_choice,
        working_directory=working_directory if monorepo_scope == "scoped" else None,
        include_dependency_setup=dependency_install == "include_non_verification",
        include_autofix=autofix == "include_non_verification",
    )
    mode = template
    if not facts.manifests or not profiles:
        mode = EnrollmentMode.READ_ONLY
        findings.append(
            DetectionFinding(
                "UNSUPPORTED_ECOSYSTEM",
                "warning",
                "No safe executable verification profile was inferred; enrollment is "
                "read-only unless writable is explicitly confirmed with the read_only "
                "policy override.",
            )
        )
    if (
        any(
            value == "read_only"
            for value in (remote_choice, base_choice, monorepo_scope, lfs_choice, budget_choice)
        )
        or submodule_choice == "read_only_parent"
        or existing_policy_choice == "preserve_read_only"
        or worktree_choice == "read_only"
        or publishing_choice == "read_only"
    ):
        mode = EnrollmentMode.READ_ONLY
    read_only_override = overrides.get("read_only")
    if read_only_override is not None:
        if read_only_override not in {"true", "false"}:
            raise ValueError("Policy override read_only must be true or false")
        if read_only_override == "true":
            mode = EnrollmentMode.READ_ONLY
        elif read_only_override == "false":
            # An explicit operator choice to stay writable overrides every
            # automatic read-only trigger above (unsupported ecosystem or a
            # read-only-leaning decision on remote/base/monorepo/etc.).
            mode = template
    if any(
        decisions.get(code) == "block"
        for code in (
            "submodules",
            "lfs",
            "existing_policy",
            "existing_worktrees",
            "publishing_access",
            "dependency_install",
            "autofix",
            "risky_commands",
        )
    ):
        findings.append(
            DetectionFinding(
                "ENROLLMENT_BLOCKED",
                "error",
                "Operator selected a blocking repository feature policy.",
            )
        )
    if mode is EnrollmentMode.READ_ONLY:
        profiles = ()
    default_allowed_paths: tuple[str, ...] = ()
    if monorepo_scope == "scoped" or budget_choice == "scoped_paths":
        if "allowed_paths" not in overrides:
            required.append(
                RequiredDecision(
                    "allowed_paths_override",
                    "Provide --policy-override allowed_paths=path[,path] for scoped enrollment.",
                    ("required",),
                )
            )
        else:
            default_allowed_paths = _path_list(overrides["allowed_paths"], key="allowed_paths")
    elif "allowed_paths" in overrides:
        default_allowed_paths = _path_list(overrides["allowed_paths"], key="allowed_paths")
    if working_directory is not None and default_allowed_paths:
        normalized_workdir = working_directory.rstrip("/")
        compatible = any(
            normalized_workdir == item.rstrip("/").removesuffix("/**")
            or normalized_workdir.startswith(item.rstrip("/").removesuffix("/**") + "/")
            or item.rstrip("/").removesuffix("/**").startswith(normalized_workdir + "/")
            for item in default_allowed_paths
        )
        if not compatible:
            raise ValueError(
                "working_directory must be contained by at least one allowed_paths override"
            )
    denied_paths: list[str] = list(SAFE_DENIED_PATHS)
    if "denied_paths_add" in overrides:
        denied_paths.extend(_path_list(overrides["denied_paths_add"], key="denied_paths_add"))
    strict = template is EnrollmentMode.STRICT
    policy = RepositoryPolicyProposal(
        mode=mode,
        remote=remote,
        default_base=base,
        allowed_base_branches=(base,) if base else (),
        allowed_paths=default_allowed_paths,
        denied_paths=tuple(dict.fromkeys(denied_paths)),
        profiles=profiles,
        publish_enabled=publish_enabled and mode is not EnrollmentMode.READ_ONLY,
        max_changed_files=_positive_override(
            overrides, "max_changed_files", 50 if strict else 150, maximum=10_000
        ),
        max_diff_lines=_positive_override(
            overrides, "max_diff_lines", 4_000 if strict else 12_000, maximum=1_000_000
        ),
        max_total_changed_bytes=_positive_override(
            overrides,
            "max_total_changed_bytes",
            10 * 1024 * 1024 if strict else 25 * 1024 * 1024,
            maximum=10 * 1024 * 1024 * 1024,
        ),
    )
    confidence = ProposalConfidence.HIGH
    if required:
        confidence = ProposalConfidence.LOW
    elif any(item.severity == "warning" for item in findings):
        confidence = ProposalConfidence.MEDIUM
    if any(item.severity == "error" for item in findings):
        confidence = ProposalConfidence.BLOCKED

    facts_payload = asdict(facts)
    facts_payload["root"] = str(facts.root)
    facts_payload["common_dir"] = str(facts.common_dir)
    facts_fingerprint = hashlib.sha256(
        json.dumps(facts_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "facts_fingerprint": facts_fingerprint,
        "repo_id": facts.repo_id,
        "path": str(facts.root),
        "findings": [asdict(x) for x in findings],
        "required_decisions": [asdict(x) for x in required],
        "policy": asdict(policy),
    }
    proposal_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return RepositoryProposal(
        proposal_id,
        facts_fingerprint,
        facts.repo_id,
        str(facts.root),
        confidence,
        tuple(findings),
        tuple(required),
        policy,
    )
