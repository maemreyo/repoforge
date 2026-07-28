"""Typed durable work queued for operation execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

from typing_extensions import Self

from .errors import ErrorCode, RepoForgeError
from .operation_task import next_operation_timestamp, validate_operation_id

_WORK_ITEM_FIELDS = frozenset(
    {
        "operation_id",
        "request",
        "state",
        "attempt",
        "owner_id",
        "lease_expires_at",
        "child_started",
        "created_at",
        "updated_at",
        "schema_version",
    }
)
_PROFILE_REQUEST_FIELDS = frozenset(
    {
        "kind",
        "workspace_id",
        "profile_name",
        "expected_head_sha",
        "expected_fingerprint",
        "config_generation",
    }
)
_ADHOC_REQUEST_FIELDS = frozenset(
    {
        "kind",
        "workspace_id",
        "argv",
        "working_directory",
        "mutability",
        "expected_head_sha",
        "expected_fingerprint",
        "config_generation",
    }
)
_DIAGNOSTIC_REQUEST_FIELDS = frozenset(
    {
        "kind",
        "workspace_id",
        "diagnostic_id",
        "selector",
        "selector2",
        "intent",
        "expectation",
        "expected_failure_class",
        "force_rerun",
        "rerun_failed",
        "expected_head_sha",
        "expected_fingerprint",
        "config_generation",
    }
)


class OperationWorkState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"


@dataclass(frozen=True, slots=True)
class OperationWorkRequest:
    kind: Literal["profile", "adhoc", "diagnostic"]
    workspace_id: str
    expected_head_sha: str
    expected_fingerprint: str
    config_generation: int
    profile_name: str | None = None
    argv: tuple[str, ...] = ()
    working_directory: str | None = None
    mutability: str = "read_only"
    diagnostic_id: str | None = None
    selector: tuple[str, ...] | None = None
    selector2: tuple[str, ...] | None = None
    intent: str | None = None
    expectation: str | None = None
    expected_failure_class: str | None = None
    force_rerun: bool = False
    rerun_failed: bool = False

    @classmethod
    def profile(
        cls,
        *,
        workspace_id: str,
        profile_name: str,
        expected_head_sha: str,
        expected_fingerprint: str,
        config_generation: int,
    ) -> Self:
        return cls(
            kind="profile",
            workspace_id=workspace_id,
            profile_name=profile_name,
            expected_head_sha=expected_head_sha,
            expected_fingerprint=expected_fingerprint,
            config_generation=config_generation,
        )

    @classmethod
    def diagnostic(
        cls,
        *,
        workspace_id: str,
        diagnostic_id: str,
        selector: tuple[str, ...] | None,
        selector2: tuple[str, ...] | None,
        intent: str | None,
        expectation: str | None,
        expected_failure_class: str | None,
        force_rerun: bool,
        rerun_failed: bool,
        expected_head_sha: str,
        expected_fingerprint: str,
        config_generation: int,
    ) -> Self:
        return cls(
            kind="diagnostic",
            workspace_id=workspace_id,
            diagnostic_id=diagnostic_id,
            selector=selector,
            selector2=selector2,
            intent=intent,
            expectation=expectation,
            expected_failure_class=expected_failure_class,
            force_rerun=force_rerun,
            rerun_failed=rerun_failed,
            expected_head_sha=expected_head_sha,
            expected_fingerprint=expected_fingerprint,
            config_generation=config_generation,
        )

    @classmethod
    def adhoc(
        cls,
        *,
        workspace_id: str,
        argv: tuple[str, ...],
        working_directory: str | None,
        mutability: str,
        expected_head_sha: str,
        expected_fingerprint: str,
        config_generation: int,
    ) -> Self:
        return cls(
            kind="adhoc",
            workspace_id=workspace_id,
            argv=argv,
            working_directory=working_directory,
            mutability=mutability,
            expected_head_sha=expected_head_sha,
            expected_fingerprint=expected_fingerprint,
            config_generation=config_generation,
        )


@dataclass(frozen=True, slots=True)
class OperationWorkItem:
    operation_id: str
    request: OperationWorkRequest
    state: OperationWorkState
    attempt: int
    owner_id: str | None
    lease_expires_at: str | None
    child_started: bool
    created_at: str
    updated_at: str
    schema_version: int = 1


def new_work_item(
    *,
    operation_id: str,
    request: OperationWorkRequest,
    now: str,
) -> OperationWorkItem:
    validate_operation_id(operation_id)
    fingerprint = request.expected_fingerprint
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise RepoForgeError(
            "expected_fingerprint must be an exact lowercase SHA-256 digest",
            code=ErrorCode.OPERATION_INVALID,
        )
    return OperationWorkItem(
        operation_id=operation_id,
        request=request,
        state=OperationWorkState.QUEUED,
        attempt=0,
        owner_id=None,
        lease_expires_at=None,
        child_started=False,
        created_at=now,
        updated_at=now,
    )


def claim_work_item(
    item: OperationWorkItem,
    *,
    owner_id: str,
    lease_expires_at: str,
    now: str,
) -> OperationWorkItem:
    if item.state is not OperationWorkState.QUEUED:
        raise RepoForgeError(
            "only queued work can be claimed",
            code=ErrorCode.OPERATION_INVALID,
        )
    return replace(
        item,
        state=OperationWorkState.CLAIMED,
        attempt=item.attempt + 1,
        owner_id=owner_id,
        lease_expires_at=lease_expires_at,
        updated_at=next_operation_timestamp(item.updated_at, now),
    )


def requeue_unstarted_work(
    item: OperationWorkItem,
    *,
    now: str,
) -> OperationWorkItem:
    if item.child_started:
        raise RepoForgeError(
            "started work cannot be requeued",
            code=ErrorCode.OPERATION_INVALID,
        )
    return replace(
        item,
        state=OperationWorkState.QUEUED,
        owner_id=None,
        lease_expires_at=None,
        updated_at=next_operation_timestamp(item.updated_at, now),
    )


def mark_work_child_started(
    item: OperationWorkItem,
    *,
    owner_id: str,
    attempt: int,
    now: str,
) -> OperationWorkItem:
    if (
        item.state is not OperationWorkState.CLAIMED
        or item.owner_id != owner_id
        or item.attempt != attempt
    ):
        raise RepoForgeError(
            "Work ownership changed before the child spawn boundary",
            code=ErrorCode.OPERATION_STALE,
        )
    if item.child_started:
        return item
    return replace(
        item,
        child_started=True,
        updated_at=next_operation_timestamp(item.updated_at, now),
    )


def renew_work_claim(
    item: OperationWorkItem,
    *,
    owner_id: str,
    lease_expires_at: str,
    now: str,
) -> OperationWorkItem:
    if item.state is not OperationWorkState.CLAIMED or item.owner_id != owner_id:
        raise RepoForgeError(
            "only the current work owner can renew its lease",
            code=ErrorCode.OPERATION_STALE,
        )
    return replace(
        item,
        lease_expires_at=lease_expires_at,
        updated_at=next_operation_timestamp(item.updated_at, now),
    )


def work_item_payload(item: OperationWorkItem) -> dict[str, object]:
    request = item.request
    if request.kind == "profile":
        request_payload: dict[str, object] = {
            "kind": "profile",
            "workspace_id": request.workspace_id,
            "profile_name": request.profile_name,
            "expected_head_sha": request.expected_head_sha,
            "expected_fingerprint": request.expected_fingerprint,
            "config_generation": request.config_generation,
        }
    elif request.kind == "diagnostic":
        request_payload = {
            "kind": "diagnostic",
            "workspace_id": request.workspace_id,
            "diagnostic_id": request.diagnostic_id,
            "selector": None if request.selector is None else list(request.selector),
            "selector2": None if request.selector2 is None else list(request.selector2),
            "intent": request.intent,
            "expectation": request.expectation,
            "expected_failure_class": request.expected_failure_class,
            "force_rerun": request.force_rerun,
            "rerun_failed": request.rerun_failed,
            "expected_head_sha": request.expected_head_sha,
            "expected_fingerprint": request.expected_fingerprint,
            "config_generation": request.config_generation,
        }
    else:
        request_payload = {
            "kind": "adhoc",
            "workspace_id": request.workspace_id,
            "argv": list(request.argv),
            "working_directory": request.working_directory,
            "mutability": request.mutability,
            "expected_head_sha": request.expected_head_sha,
            "expected_fingerprint": request.expected_fingerprint,
            "config_generation": request.config_generation,
        }
    return {
        "operation_id": item.operation_id,
        "request": request_payload,
        "state": item.state.value,
        "attempt": item.attempt,
        "owner_id": item.owner_id,
        "lease_expires_at": item.lease_expires_at,
        "child_started": item.child_started,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "schema_version": item.schema_version,
    }


def work_item_from_payload(payload: dict[str, object]) -> OperationWorkItem:
    request_payload = payload.get("request")
    request_kind_raw = request_payload.get("kind") if isinstance(request_payload, dict) else None
    request_kind = request_kind_raw if isinstance(request_kind_raw, str) else ""
    request_fields = {
        "profile": _PROFILE_REQUEST_FIELDS,
        "adhoc": _ADHOC_REQUEST_FIELDS,
        "diagnostic": _DIAGNOSTIC_REQUEST_FIELDS,
    }.get(request_kind, frozenset())
    raw_attempt = payload.get("attempt")
    raw_schema_version = payload.get("schema_version")
    raw_child_started = payload.get("child_started")
    if (
        set(payload) != _WORK_ITEM_FIELDS
        or raw_schema_version != 1
        or not isinstance(raw_schema_version, int)
        or isinstance(raw_schema_version, bool)
        or not isinstance(raw_attempt, int)
        or isinstance(raw_attempt, bool)
        or raw_attempt < 0
        or not isinstance(raw_child_started, bool)
        or not isinstance(request_payload, dict)
        or set(request_payload) != request_fields
        or request_kind not in {"profile", "adhoc", "diagnostic"}
    ):
        raise RepoForgeError(
            "operation work payload fields do not match schema version 1",
            code=ErrorCode.OPERATION_INVALID,
        )
    if request_kind == "profile":
        request = OperationWorkRequest.profile(
            workspace_id=str(request_payload["workspace_id"]),
            profile_name=str(request_payload["profile_name"]),
            expected_head_sha=str(request_payload["expected_head_sha"]),
            expected_fingerprint=str(request_payload["expected_fingerprint"]),
            config_generation=int(request_payload["config_generation"]),
        )
    elif request_kind == "diagnostic":
        raw_selector = request_payload["selector"]
        raw_selector2 = request_payload["selector2"]
        for raw in (raw_selector, raw_selector2):
            if raw is not None and (
                not isinstance(raw, list) or not all(isinstance(item, str) for item in raw)
            ):
                raise RepoForgeError(
                    "operation work diagnostic selectors must be null or JSON string arrays",
                    code=ErrorCode.OPERATION_INVALID,
                )
        request = OperationWorkRequest.diagnostic(
            workspace_id=str(request_payload["workspace_id"]),
            diagnostic_id=str(request_payload["diagnostic_id"]),
            selector=None if raw_selector is None else tuple(raw_selector),
            selector2=None if raw_selector2 is None else tuple(raw_selector2),
            intent=None if request_payload["intent"] is None else str(request_payload["intent"]),
            expectation=(
                None
                if request_payload["expectation"] is None
                else str(request_payload["expectation"])
            ),
            expected_failure_class=(
                None
                if request_payload["expected_failure_class"] is None
                else str(request_payload["expected_failure_class"])
            ),
            force_rerun=bool(request_payload["force_rerun"]),
            rerun_failed=bool(request_payload["rerun_failed"]),
            expected_head_sha=str(request_payload["expected_head_sha"]),
            expected_fingerprint=str(request_payload["expected_fingerprint"]),
            config_generation=int(request_payload["config_generation"]),
        )
    else:
        raw_argv = request_payload["argv"]
        if not isinstance(raw_argv, list) or not all(isinstance(arg, str) for arg in raw_argv):
            raise RepoForgeError(
                "operation work adhoc argv must be a JSON string array",
                code=ErrorCode.OPERATION_INVALID,
            )
        request = OperationWorkRequest.adhoc(
            workspace_id=str(request_payload["workspace_id"]),
            argv=tuple(raw_argv),
            working_directory=(
                None
                if request_payload["working_directory"] is None
                else str(request_payload["working_directory"])
            ),
            mutability=str(request_payload["mutability"]),
            expected_head_sha=str(request_payload["expected_head_sha"]),
            expected_fingerprint=str(request_payload["expected_fingerprint"]),
            config_generation=int(request_payload["config_generation"]),
        )
    return OperationWorkItem(
        operation_id=str(payload["operation_id"]),
        request=request,
        state=OperationWorkState(str(payload["state"])),
        attempt=raw_attempt,
        owner_id=None if payload["owner_id"] is None else str(payload["owner_id"]),
        lease_expires_at=(
            None if payload["lease_expires_at"] is None else str(payload["lease_expires_at"])
        ),
        child_started=raw_child_started,
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
        schema_version=raw_schema_version,
    )
