"""Zero-config rule loading (#204): `.repoforge/rules/*.yaml` overrides conservative defaults.

Ingestion is untrusted-input safe: `yaml.safe_load` only, bounded file count/size, and a rule
whose `id` collides with a default is a deliberate override (repo wins), never a silent merge
of two conflicting definitions under one id.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ...domain.rules_engine import Rule, RuleValidationError, parse_rule
from .validators import BUILTIN_VALIDATORS

_MAX_RULE_FILES = 200
_MAX_RULE_FILE_BYTES = 1_000_000
_KNOWN_VALIDATORS = tuple(BUILTIN_VALIDATORS)

DEFAULT_RULES: tuple[Rule, ...] = (
    parse_rule(
        {
            "id": "default.file-length",
            "enforcement": "checked",
            "validator": "file_length",
            "paths": ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js"],
            "max_lines": 400,
            "delivery": "on_entry",
        },
        known_validators=_KNOWN_VALIDATORS,
        source="<default>",
    ),
)


def load_rules(repo_root: Path) -> tuple[Rule, ...]:
    """Load `.repoforge/rules/*.yaml`; an empty/absent directory yields DEFAULT_RULES.
    Raises RuleValidationError (including UnsupportedEnforcementError/UnknownValidatorError)
    on the first invalid entry -- never silently drops a bad rule."""

    rules_dir = repo_root / ".repoforge" / "rules"
    if not rules_dir.is_dir():
        return DEFAULT_RULES

    files = sorted(p for p in rules_dir.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())
    if not files:
        return DEFAULT_RULES
    if len(files) > _MAX_RULE_FILES:
        raise RuleValidationError(f".repoforge/rules contains more than {_MAX_RULE_FILES} files")

    by_id: dict[str, Rule] = {rule.id: rule for rule in DEFAULT_RULES}
    for path in files:
        if path.stat().st_size > _MAX_RULE_FILE_BYTES:
            raise RuleValidationError(f"{path} exceeds the {_MAX_RULE_FILE_BYTES}-byte bound")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RuleValidationError(f"{path} is not valid YAML") from exc
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise RuleValidationError(f"{path} must contain a YAML list of rule entries")
        for entry in raw:
            rule = parse_rule(entry, known_validators=_KNOWN_VALIDATORS, source=str(path))
            by_id[rule.id] = rule  # repo-declared entries deliberately override defaults

    return tuple(by_id.values())
