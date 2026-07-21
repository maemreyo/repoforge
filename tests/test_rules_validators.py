"""Coverage for the built-in rule validators and batch review runner (#204)."""

from __future__ import annotations

from pathlib import Path

from repoforge.application.rules.validators import (
    BUILTIN_VALIDATORS,
    DiffStat,
    GeneratedPathSpec,
    ReviewContext,
    run_review,
)
from repoforge.domain.generated_paths import (
    GeneratedPathRule,
    generated_paths_identity,
    valid_regenerated_paths,
)
from repoforge.domain.rules_engine import RuleResultState, parse_rule

_KNOWN = tuple(BUILTIN_VALIDATORS)


def _rule(**overrides: object):
    base: dict[str, object] = {
        "id": "test.rule",
        "enforcement": "checked",
        "validator": "file_length",
        "paths": ["**/*.py"],
    }
    base.update(overrides)
    return parse_rule(base, known_validators=_KNOWN)


def test_file_length_flags_an_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    (tmp_path / "small.py").write_text("x = 1\n", encoding="utf-8")

    rule = _rule(max_lines=400)
    context = ReviewContext(root=tmp_path)
    report = run_review((rule,), context)

    files = {f.file for f in report.findings}
    assert "big.py" in files
    assert "small.py" not in files
    finding = next(f for f in report.findings if f.file == "big.py")
    assert finding.state is RuleResultState.FAIL


def test_second_run_after_fix_reports_zero_findings(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")
    rule = _rule(max_lines=400)
    context = ReviewContext(root=tmp_path)
    assert run_review((rule,), context).findings

    (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(10)), encoding="utf-8")
    assert run_review((rule,), context).findings == ()


def test_batch_review_reports_all_findings_across_rule_kinds_in_one_pass(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")
    (tmp_path / "boundary.py").write_text("import subprocess\n", encoding="utf-8")

    length_rule = _rule(id="r.length", max_lines=400)
    boundary_rule = _rule(
        id="r.boundary", validator="import_boundary", paths=["**/*.py"], forbid=["subprocess"]
    )
    context = ReviewContext(root=tmp_path)

    report = run_review((length_rule, boundary_rule), context)
    rule_ids = {f.rule_id for f in report.findings}
    assert rule_ids == {"r.length", "r.boundary"}


def test_import_boundary_flags_forbidden_import_and_dynamic_import_as_unknown(
    tmp_path: Path,
) -> None:
    (tmp_path / "mod.py").write_text(
        "import subprocess\n"
        "from repoforge.adapters import github\n"
        "import importlib\n"
        "importlib.import_module(compute_name())\n",
        encoding="utf-8",
    )
    rule = _rule(
        id="r.boundary",
        validator="import_boundary",
        paths=["**/*.py"],
        forbid=["subprocess", "repoforge.adapters"],
    )
    context = ReviewContext(root=tmp_path)
    report = run_review((rule,), context)

    fail_lines = {f.line for f in report.findings if f.state is RuleResultState.FAIL}
    unknown_lines = {f.line for f in report.findings if f.state is RuleResultState.UNKNOWN}
    assert fail_lines == {1, 2}
    assert unknown_lines == {4}


def test_diff_size_reports_unknown_without_diff_context(tmp_path: Path) -> None:
    rule = _rule(id="r.diff", validator="diff_size", paths=["**/*.py"])
    context = ReviewContext(root=tmp_path)
    report = run_review((rule,), context)
    assert len(report.findings) == 1
    assert report.findings[0].state is RuleResultState.UNKNOWN


def test_diff_size_fails_when_over_budget(tmp_path: Path) -> None:
    rule = _rule(id="r.diff", validator="diff_size", paths=["**/*.py"], max_lines=10, max_files=1)
    context = ReviewContext(
        root=tmp_path,
        diff_stats=(
            DiffStat("a.py", added_lines=20, removed_lines=0),
            DiffStat("b.py", added_lines=5, removed_lines=0),
        ),
    )
    report = run_review((rule,), context)
    assert any("changed lines" in f.message for f in report.findings)
    assert any("files" in f.message for f in report.findings)


def test_new_dependency_reports_unknown_without_baseline_and_fails_on_addition(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2.0", "click"]\n', encoding="utf-8"
    )
    rule = _rule(id="r.dep", validator="new_dependency", paths=["**/*"])

    no_baseline = run_review((rule,), ReviewContext(root=tmp_path))
    assert no_baseline.findings[0].state is RuleResultState.UNKNOWN

    with_baseline = run_review(
        (rule,),
        ReviewContext(
            root=tmp_path,
            baseline_manifests={"pyproject.toml": '[project]\ndependencies = ["click"]\n'},
        ),
    )
    assert any(
        f.state is RuleResultState.FAIL and "requests" in f.message for f in with_baseline.findings
    )


def test_generated_no_hand_edit_is_skipped_without_config_and_fails_on_unreceipted_change(
    tmp_path: Path,
) -> None:
    (tmp_path / "generated.json").write_text("{}", encoding="utf-8")
    rule = _rule(id="r.gen", validator="generated_no_hand_edit", paths=["**/*"])

    skipped = run_review((rule,), ReviewContext(root=tmp_path))
    assert skipped.findings[0].state is RuleResultState.SKIPPED

    spec = GeneratedPathSpec(glob="generated.json", regeneration_command="make generate")
    failing = run_review(
        (rule,),
        ReviewContext(root=tmp_path, changed_paths=("generated.json",), generated_paths=(spec,)),
    )
    assert failing.findings[0].state is RuleResultState.FAIL
    assert "make generate" in failing.findings[0].fix_hint

    rules = (GeneratedPathRule("generated.json", ("make", "generate"), "fixture"),)
    output_identity = generated_paths_identity(tmp_path, ("generated.json",))
    receipts = [
        {
            "schema_version": 1,
            "commands": [["make", "generate"]],
            "generated_paths": ["generated.json"],
            "source_identity": "a" * 64,
            "output_identity": output_identity,
            "deterministic": True,
            "refresh_commit_sha": "b" * 40,
            "target_base_sha": "c" * 40,
            "plan_hash": "d" * 64,
        }
    ]
    fresh_paths = valid_regenerated_paths(tmp_path, rules, receipts)
    receipted = run_review(
        (rule,),
        ReviewContext(
            root=tmp_path,
            changed_paths=("generated.json",),
            generated_paths=(spec,),
            regenerated_paths=fresh_paths,
        ),
    )
    assert receipted.findings == ()

    (tmp_path / "generated.json").write_text('{"manual": true}', encoding="utf-8")
    stale_paths = valid_regenerated_paths(tmp_path, rules, receipts)
    stale = run_review(
        (rule,),
        ReviewContext(
            root=tmp_path,
            changed_paths=("generated.json",),
            generated_paths=(spec,),
            regenerated_paths=stale_paths,
        ),
    )
    assert stale_paths == frozenset()
    assert stale.findings[0].state is RuleResultState.FAIL
    assert "without a matching regeneration receipt" in stale.findings[0].message


def test_run_review_reports_error_for_a_missing_validator(tmp_path: Path) -> None:
    rule = _rule(validator="file_length")
    object.__setattr__(rule, "validator", "does_not_exist")
    report = run_review((rule,), ReviewContext(root=tmp_path))
    assert report.findings[0].state is RuleResultState.ERROR
