from __future__ import annotations

from types import SimpleNamespace

import pytest

from repoforge.application.workspace.verify import _auto_target


def _assessment(value: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        code_intelligence=SimpleNamespace(
            status=SimpleNamespace(value="current"),
            coverage=SimpleNamespace(value="complete"),
            value=value,
        )
    )


def _candidate() -> dict[str, object]:
    return {
        "diagnostic_id": "pytest-target",
        "selector": "tests/test_service.py",
        "confidence": 95,
        "reason": "Reviewed affected-test evidence.",
    }


def _semantic_graph(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "current",
        "coverage": {"value": 100, "reason": "Complete graph projection."},
        "confidence": {"value": 95, "reason": "Promoted semantic evidence."},
        "truncated": False,
    }
    payload.update(overrides)
    return payload


def test_auto_target_preserves_disabled_semantic_graph_behavior() -> None:
    targeted = _auto_target(_assessment({"affected_tests": [_candidate()]}))

    assert targeted is not None
    assert targeted[0] == "pytest-target"
    assert targeted[1] == ["tests/test_service.py"]


def test_auto_target_accepts_complete_current_semantic_graph() -> None:
    targeted = _auto_target(
        _assessment(
            {
                "affected_tests": [_candidate()],
                "semantic_graph": _semantic_graph(),
            }
        )
    )

    assert targeted is not None
    assert targeted[1] == ["tests/test_service.py"]


@pytest.mark.parametrize(
    "semantic_graph",
    [
        _semantic_graph(status="partial"),
        _semantic_graph(coverage={"value": 99, "reason": "Incomplete."}),
        _semantic_graph(confidence={"value": 94, "reason": "Below threshold."}),
        _semantic_graph(truncated=True),
        {},
    ],
)
def test_auto_target_rejects_semantic_uncertainty(
    semantic_graph: dict[str, object],
) -> None:
    assessment = _assessment(
        {
            "affected_tests": [_candidate()],
            "semantic_graph": semantic_graph,
        }
    )

    assert _auto_target(assessment) is None
