"""A context pack's hash must change with every input, and with nothing else.

Slice of #206: the binding half. The acceptance criteria this file encodes come from the
ticket verbatim -- same inputs give an identical `pack_hash`, and *any single* binding input
change (including `constitution_sha` alone) gives a different one.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from repoforge.application.rules.pack import COMPILER_SCHEMA_VERSION, ContextPackBinding


def _binding(**overrides: object) -> ContextPackBinding:
    defaults: dict[str, object] = {
        "code_snapshot_sha": "a" * 64,
        "constitution_sha": "b" * 64,
        "config_generation": 7,
        "task_revision": 3,
        "policy_digest": "c" * 64,
        "focus_paths": ("src/app.py", "src/models.py"),
    }
    defaults.update(overrides)
    return ContextPackBinding(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------- determinism


def test_the_same_inputs_give_the_same_pack_hash() -> None:
    assert _binding().pack_hash == _binding().pack_hash


def test_the_hash_does_not_depend_on_the_order_focus_paths_were_supplied() -> None:
    """`focus_paths` selects scope: it is a set, not a sequence."""

    forward = _binding(focus_paths=("src/app.py", "src/models.py"))
    reversed_order = _binding(focus_paths=("src/models.py", "src/app.py"))
    duplicated = _binding(focus_paths=("src/app.py", "src/models.py", "src/app.py"))

    assert forward.pack_hash == reversed_order.pack_hash == duplicated.pack_hash
    assert forward.focus_paths == ("src/app.py", "src/models.py")


# -------------------------------------------------------------------- sensitivity


@pytest.mark.parametrize(
    ("field_name", "changed"),
    [
        ("code_snapshot_sha", "d" * 64),
        ("constitution_sha", "e" * 64),
        ("config_generation", 8),
        ("task_revision", 4),
        ("policy_digest", "f" * 64),
        ("focus_paths", ("src/app.py",)),
        ("compiler_schema_version", COMPILER_SCHEMA_VERSION + 1),
    ],
)
def test_changing_any_single_binding_input_changes_the_pack_hash(
    field_name: str,
    changed: object,
) -> None:
    baseline = _binding()
    mutated = replace(baseline, **{field_name: changed})  # type: ignore[arg-type]

    assert getattr(mutated, field_name) != getattr(baseline, field_name)
    assert mutated.pack_hash != baseline.pack_hash, (
        f"{field_name} can change the compiled pack, so it must change the hash"
    )


def test_every_declared_binding_input_is_actually_hashed() -> None:
    """Guards the failure mode a per-field test cannot: a field added but never bound.

    `as_dict` is what `pack_hash` hashes, so a new field that never reaches it would be
    invisible to the hash while still changing the pack.
    """

    hashed = set(_binding().as_dict())
    declared = {
        name for name in ContextPackBinding.__dataclass_fields__ if not name.startswith("_")
    }

    assert hashed == declared


# --------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_snapshot_sha": "not-a-sha"},
        {"constitution_sha": "B" * 64},  # uppercase is not the canonical form
        {"policy_digest": "a" * 63},
        {"config_generation": 0},
        {"task_revision": -1},
        {"config_generation": True},  # bool is not an acceptable int here
    ],
)
def test_an_unusable_binding_input_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _binding(**overrides)


def test_an_out_of_bounds_focus_path_set_is_refused() -> None:
    with pytest.raises(ValueError, match="focus_paths"):
        _binding(focus_paths=tuple(f"src/file{index}.py" for index in range(101)))

    with pytest.raises(ValueError, match="focus_paths"):
        _binding(focus_paths=("",))


def test_the_schema_version_defaults_to_the_current_compiler() -> None:
    assert _binding().compiler_schema_version == COMPILER_SCHEMA_VERSION
