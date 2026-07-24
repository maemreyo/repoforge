"""Cross-layer control-plane fault gates and agent-efficiency metrics."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from ..domain.redaction import redact_text

ControlPlaneBoundary = Literal[
    "before_effect",
    "commit",
    "after_effect",
    "serialization",
    "result_persistence",
    "caller_disconnect",
    "stale_identity",
    "logs",
    "bounded_retrieval",
    "pr_drift",
    "graph_projection",
    "generated_refresh",
]
TimestampState = Literal["observed", "unavailable", "synthetic"]

CONTROL_PLANE_BOUNDARIES: frozenset[str] = frozenset(
    {
        "before_effect",
        "commit",
        "after_effect",
        "serialization",
        "result_persistence",
        "caller_disconnect",
        "stale_identity",
        "logs",
        "bounded_retrieval",
        "pr_drift",
        "graph_projection",
        "generated_refresh",
    }
)
CONTROL_PLANE_THRESHOLDS: dict[str, float] = {
    "max_unknown_effect_outcomes": 0.0,
    "max_synthetic_timestamp_count": 0.0,
    "max_opaque_failure_count": 0.0,
    "max_calls_per_completed_task": 6.0,
    "max_duplicate_read_rate": 0.05,
    "max_temporary_diagnostic_mutations": 0.0,
    "max_full_profile_reruns": 0.0,
    "max_actionable_failure_call": 3.0,
    "max_hidden_retries": 0.0,
}
_TRACE_FIELDS = frozenset(
    {
        "call",
        "tool",
        "kind",
        "resource",
        "state_version",
        "cursor",
        "outcome",
        "effect_key",
        "actionable_failure",
        "temporary_mutation",
        "profile",
        "rerun",
        "timestamp_state",
    }
)
_AUTHORITATIVE_EFFECT_OUTCOMES = frozenset(
    {
        "applied",
        "closed",
        "completed",
        "failed_before_effect",
        "reconciled",
        "replayed",
        "rolled_back",
        "succeeded",
    }
)
_OPAQUE_FAILURE_OUTCOMES = frozenset(
    {"opaque_failure", "transport_failure", "transport_failure_only"}
)


@dataclass(frozen=True, slots=True)
class ControlPlaneIdentity:
    git_head: str
    dirty: bool
    python_version: str
    package_version: str
    contract_version: str
    tool_count: int
    tool_surface_hash: str
    schema_bundle_digest: str


@dataclass(frozen=True, slots=True)
class ControlPlaneStep:
    call: int
    tool: str
    kind: str
    resource: str
    state_version: str
    cursor: str | None
    outcome: str
    effect_key: str | None
    actionable_failure: bool
    temporary_mutation: bool
    profile: str | None
    rerun: bool
    timestamp_state: TimestampState


@dataclass(frozen=True, slots=True)
class ControlPlaneScenario:
    scenario_id: str
    selector: str
    boundary: ControlPlaneBoundary
    completed: bool
    trace: tuple[ControlPlaneStep, ...]


@dataclass(frozen=True, slots=True)
class ControlPlaneManifest:
    schema_version: int
    thresholds: Mapping[str, float]
    scenarios: tuple[ControlPlaneScenario, ...]


@dataclass(frozen=True, slots=True)
class ScenarioExecution:
    scenario_id: str
    selector: str
    passed: bool
    duration_ms: float
    attempts: int
    output_excerpt: str


@dataclass(frozen=True, slots=True)
class ControlPlaneMetrics:
    unknown_effect_outcomes: int
    synthetic_timestamp_count: int
    opaque_failure_count: int
    calls_per_completed_task: float
    duplicate_read_rate: float
    temporary_diagnostic_mutations: int
    full_profile_reruns: int
    max_actionable_failure_call: int


@dataclass(frozen=True, slots=True)
class ControlPlaneGateReport:
    schema_version: int
    identity: ControlPlaneIdentity
    metrics: ControlPlaneMetrics
    executions: tuple[ScenarioExecution, ...]
    hidden_retry_count: int
    passed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "identity": asdict(self.identity),
            "metrics": asdict(self.metrics),
            "executions": [
                {
                    **asdict(item),
                    "output_excerpt": redact_text(item.output_excerpt, limit=8_000),
                }
                for item in self.executions
            ],
            "hidden_retry_count": self.hidden_retry_count,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneReportPaths:
    json_path: Path
    markdown_path: Path


ScenarioExecutor = Callable[[ControlPlaneScenario], ScenarioExecution]


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a JSON object")
    return dict(value)


def _string(value: object, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a boolean")
    return value


def _positive_integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _thresholds(value: object) -> dict[str, float]:
    raw = _mapping(value, "control-plane thresholds")
    normalized: dict[str, float] = {}
    for key, threshold in raw.items():
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise ValueError(f"control-plane threshold {key!r} must be numeric")
        normalized[key] = float(threshold)
    if normalized != CONTROL_PLANE_THRESHOLDS:
        raise ValueError("control-plane thresholds drifted from the reviewed release contract")
    return normalized


def _step(value: object, context: str) -> ControlPlaneStep:
    raw = _mapping(value, context)
    if set(raw) != _TRACE_FIELDS:
        raise ValueError(f"{context} must contain exactly {sorted(_TRACE_FIELDS)}")
    timestamp_state = _string(raw["timestamp_state"], f"{context}.timestamp_state")
    if timestamp_state not in {"observed", "unavailable", "synthetic"}:
        raise ValueError(f"{context}.timestamp_state is unsupported")
    return ControlPlaneStep(
        call=_positive_integer(raw["call"], f"{context}.call"),
        tool=_string(raw["tool"], f"{context}.tool"),
        kind=_string(raw["kind"], f"{context}.kind"),
        resource=_string(raw["resource"], f"{context}.resource"),
        state_version=_string(raw["state_version"], f"{context}.state_version"),
        cursor=_optional_string(raw["cursor"], f"{context}.cursor"),
        outcome=_string(raw["outcome"], f"{context}.outcome"),
        effect_key=_optional_string(raw["effect_key"], f"{context}.effect_key"),
        actionable_failure=_boolean(raw["actionable_failure"], f"{context}.actionable_failure"),
        temporary_mutation=_boolean(raw["temporary_mutation"], f"{context}.temporary_mutation"),
        profile=_optional_string(raw["profile"], f"{context}.profile"),
        rerun=_boolean(raw["rerun"], f"{context}.rerun"),
        timestamp_state=cast(TimestampState, timestamp_state),
    )


def load_control_plane_manifest(path: Path) -> ControlPlaneManifest:
    """Load one fail-closed, reviewed fault-matrix manifest."""

    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load control-plane manifest: {exc}") from exc
    if set(document) != {"schema_version", "thresholds", "scenarios"}:
        raise ValueError("control-plane manifest has unsupported top-level fields")
    if document["schema_version"] != 1:
        raise ValueError("control-plane manifest uses an unsupported schema_version")
    raw_scenarios = document["scenarios"]
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("control-plane manifest requires scenarios")

    scenario_ids: set[str] = set()
    selectors: set[str] = set()
    scenarios: list[ControlPlaneScenario] = []
    for index, item in enumerate(raw_scenarios):
        context = f"control-plane scenarios[{index}]"
        raw = _mapping(item, context)
        if set(raw) != {"id", "selector", "boundary", "completed", "trace"}:
            raise ValueError(f"{context} has unsupported fields")
        scenario_id = _string(raw["id"], f"{context}.id")
        selector = _string(raw["selector"], f"{context}.selector")
        boundary = _string(raw["boundary"], f"{context}.boundary")
        if boundary not in CONTROL_PLANE_BOUNDARIES:
            raise ValueError(f"{context}.boundary is unsupported")
        if scenario_id in scenario_ids:
            raise ValueError(f"duplicate control-plane scenario id: {scenario_id}")
        if selector in selectors:
            raise ValueError(f"duplicate control-plane selector: {selector}")
        if not selector.startswith("tests/") or "::" not in selector:
            raise ValueError(f"{context}.selector must be one exact pytest test selector")
        scenario_ids.add(scenario_id)
        selectors.add(selector)
        raw_trace = raw["trace"]
        if not isinstance(raw_trace, list) or not raw_trace:
            raise ValueError(f"{context}.trace must be a non-empty array")
        trace = tuple(
            _step(step, f"{context}.trace[{offset}]") for offset, step in enumerate(raw_trace)
        )
        calls = [step.call for step in trace]
        if calls != sorted(calls) or len(calls) != len(set(calls)):
            raise ValueError(f"{context}.trace calls must be unique and increasing")
        scenarios.append(
            ControlPlaneScenario(
                scenario_id=scenario_id,
                selector=selector,
                boundary=cast(ControlPlaneBoundary, boundary),
                completed=_boolean(raw["completed"], f"{context}.completed"),
                trace=trace,
            )
        )
    return ControlPlaneManifest(
        schema_version=1,
        thresholds=_thresholds(document["thresholds"]),
        scenarios=tuple(scenarios),
    )


def _metrics(manifest: ControlPlaneManifest) -> ControlPlaneMetrics:
    steps = tuple(step for scenario in manifest.scenarios for step in scenario.trace)
    unresolved_effects: set[str] = set()
    read_count = 0
    duplicate_reads = 0
    read_identities: set[tuple[str, str, str, str | None]] = set()
    for step in steps:
        if step.effect_key is not None:
            if step.outcome == "effect_outcome_unknown":
                unresolved_effects.add(step.effect_key)
            elif step.outcome in _AUTHORITATIVE_EFFECT_OUTCOMES:
                unresolved_effects.discard(step.effect_key)
        if step.kind == "read":
            read_count += 1
            identity = (step.tool, step.resource, step.state_version, step.cursor)
            if identity in read_identities:
                duplicate_reads += 1
            read_identities.add(identity)
    completed_count = sum(scenario.completed for scenario in manifest.scenarios)
    return ControlPlaneMetrics(
        unknown_effect_outcomes=len(unresolved_effects),
        synthetic_timestamp_count=sum(step.timestamp_state == "synthetic" for step in steps),
        opaque_failure_count=sum(step.outcome in _OPAQUE_FAILURE_OUTCOMES for step in steps),
        calls_per_completed_task=(
            round(len(steps) / completed_count, 3) if completed_count else 0.0
        ),
        duplicate_read_rate=(round(duplicate_reads / read_count, 6) if read_count else 0.0),
        temporary_diagnostic_mutations=sum(step.temporary_mutation for step in steps),
        full_profile_reruns=sum(step.profile == "full" and step.rerun for step in steps),
        max_actionable_failure_call=max(
            (step.call for step in steps if step.actionable_failure), default=0
        ),
    )


def _metrics_pass(metrics: ControlPlaneMetrics, hidden_retry_count: int) -> bool:
    return (
        metrics.unknown_effect_outcomes <= CONTROL_PLANE_THRESHOLDS["max_unknown_effect_outcomes"]
        and metrics.synthetic_timestamp_count
        <= CONTROL_PLANE_THRESHOLDS["max_synthetic_timestamp_count"]
        and metrics.opaque_failure_count <= CONTROL_PLANE_THRESHOLDS["max_opaque_failure_count"]
        and metrics.calls_per_completed_task
        <= CONTROL_PLANE_THRESHOLDS["max_calls_per_completed_task"]
        and metrics.duplicate_read_rate <= CONTROL_PLANE_THRESHOLDS["max_duplicate_read_rate"]
        and metrics.temporary_diagnostic_mutations
        <= CONTROL_PLANE_THRESHOLDS["max_temporary_diagnostic_mutations"]
        and metrics.full_profile_reruns <= CONTROL_PLANE_THRESHOLDS["max_full_profile_reruns"]
        and metrics.max_actionable_failure_call
        <= CONTROL_PLANE_THRESHOLDS["max_actionable_failure_call"]
        and hidden_retry_count <= CONTROL_PLANE_THRESHOLDS["max_hidden_retries"]
    )


def evaluate_control_plane_gates(
    manifest: ControlPlaneManifest,
    executions: Sequence[ScenarioExecution],
    *,
    identity: ControlPlaneIdentity,
) -> ControlPlaneGateReport:
    """Evaluate exact scenario executions and recorded traces against release thresholds."""

    expected = {scenario.scenario_id: scenario for scenario in manifest.scenarios}
    observed: dict[str, ScenarioExecution] = {}
    for execution in executions:
        if execution.scenario_id in observed:
            raise ValueError(f"duplicate scenario execution: {execution.scenario_id}")
        scenario = expected.get(execution.scenario_id)
        if scenario is None:
            raise ValueError(f"unknown scenario execution: {execution.scenario_id}")
        if execution.selector != scenario.selector:
            raise ValueError(f"selector drift for scenario {execution.scenario_id}")
        if execution.attempts < 1:
            raise ValueError("scenario execution attempts must be positive")
        if execution.duration_ms < 0:
            raise ValueError("scenario execution duration cannot be negative")
        observed[execution.scenario_id] = execution
    missing = sorted(set(expected) - set(observed))
    if missing:
        raise ValueError(f"missing control-plane scenario executions: {missing}")

    frozen_executions = tuple(observed[scenario.scenario_id] for scenario in manifest.scenarios)
    hidden_retry_count = sum(item.attempts - 1 for item in frozen_executions)
    metrics = _metrics(manifest)
    passed = (
        all(scenario.completed for scenario in manifest.scenarios)
        and all(item.passed for item in frozen_executions)
        and _metrics_pass(metrics, hidden_retry_count)
    )
    return ControlPlaneGateReport(
        schema_version=1,
        identity=identity,
        metrics=metrics,
        executions=frozen_executions,
        hidden_retry_count=hidden_retry_count,
        passed=passed,
    )


def run_control_plane_scenarios(
    manifest: ControlPlaneManifest,
    executor: ScenarioExecutor,
) -> tuple[ScenarioExecution, ...]:
    """Execute every reviewed scenario exactly once, without hidden retries."""

    executions: list[ScenarioExecution] = []
    for scenario in manifest.scenarios:
        execution = executor(scenario)
        if execution.scenario_id != scenario.scenario_id:
            raise ValueError("control-plane executor returned the wrong scenario id")
        if execution.selector != scenario.selector:
            raise ValueError("control-plane executor returned the wrong selector")
        executions.append(execution)
    return tuple(executions)


def _markdown(report: ControlPlaneGateReport) -> str:
    metrics = report.metrics
    lines = [
        "# Control-plane fault gates",
        "",
        f"Overall: **{'PASS' if report.passed else 'FAIL'}**",
        "",
        f"- Git HEAD: `{report.identity.git_head}`",
        f"- Package: `{report.identity.package_version}`",
        f"- Contract: `{report.identity.contract_version}` / {report.identity.tool_count} tools",
        f"- Tool surface: `{report.identity.tool_surface_hash}`",
        f"- Unknown effect outcomes: {metrics.unknown_effect_outcomes}",
        f"- Synthetic timestamps: {metrics.synthetic_timestamp_count}",
        f"- Opaque failures: {metrics.opaque_failure_count}",
        f"- Calls per completed task: {metrics.calls_per_completed_task:.3f}",
        f"- Duplicate read rate: {metrics.duplicate_read_rate:.3%}",
        f"- Hidden retries: {report.hidden_retry_count}",
        "",
        "| Scenario | Selector | Result | Attempts | Duration ms |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for execution in report.executions:
        lines.append(
            f"| {execution.scenario_id} | `{execution.selector}` | "
            f"{'passed' if execution.passed else 'failed'} | {execution.attempts} | "
            f"{execution.duration_ms:.3f} |"
        )
    return "\n".join(lines) + "\n"


def publish_control_plane_report(
    report: ControlPlaneGateReport, output_dir: Path
) -> ControlPlaneReportPaths:
    """Publish stable machine and human-readable fault-gate reports."""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "control-plane-fault-gates.json"
    markdown_path = output_dir / "control-plane-fault-gates.md"
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return ControlPlaneReportPaths(json_path=json_path, markdown_path=markdown_path)


__all__ = [
    "CONTROL_PLANE_BOUNDARIES",
    "CONTROL_PLANE_THRESHOLDS",
    "ControlPlaneGateReport",
    "ControlPlaneIdentity",
    "ControlPlaneManifest",
    "ControlPlaneMetrics",
    "ControlPlaneReportPaths",
    "ControlPlaneScenario",
    "ControlPlaneStep",
    "ScenarioExecution",
    "evaluate_control_plane_gates",
    "load_control_plane_manifest",
    "publish_control_plane_report",
    "run_control_plane_scenarios",
]
