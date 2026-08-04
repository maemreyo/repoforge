"""Process lease roles - which subsystem a lease's process belongs to."""

from enum import Enum

from typing_extensions import assert_never


class ProcessLeaseRole(str, Enum):
    """Which subsystem a lease's process belongs to.

    One registry serves every managed process kind so completeness, pagination,
    quarantine, and reconciliation use a common substrate (F-008): the execution
    daemon, short-lived operation workers, and the serve-child of a generation all
    live in the same lease table distinguished by role.
    """

    EXECUTION_DAEMON = "execution_daemon"
    OPERATION_WORKER = "operation_worker"
    TUNNEL_CHILD = "tunnel_child"


def _validate_role(role: ProcessLeaseRole) -> ProcessLeaseRole:
    match role:
        case (
            ProcessLeaseRole.EXECUTION_DAEMON
            | ProcessLeaseRole.OPERATION_WORKER
            | ProcessLeaseRole.TUNNEL_CHILD
        ):
            return role
        case _ as unreachable:
            assert_never(unreachable)
