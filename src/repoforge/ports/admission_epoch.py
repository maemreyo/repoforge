"""Durable worker-admission epoch boundary (P1-3).

A restarter must never stop the incumbent while a spawn is in flight, and a spawn
must never begin after the restarter has fenced admission. Both sides coordinate
through one durable epoch record:

* OPEN epoch N   -- ``begin_spawn`` is allowed; spawns stamp their intent with N.
* CLOSING epoch N -- a restarter that is about to stop the incumbent durably
  closes admission: new spawns are refused with a typed fail-closed error, and
  already-stamped intents (REGISTERED/READY leases) are the fence members the
  restarter waits on (bounded) to settle or terminalize.
* OPEN epoch N+1 -- after the incumbent is stopped and reclaimed, the restarter
  opens the next epoch so the replacement can spawn.

The epoch record is durable so the registrar (supervisor process) and the
restarter (launcher/CLI process) observe the same fence across processes.
"""

from __future__ import annotations

from typing import Protocol

#: The open state: new spawns are admitted and stamped with the current epoch.
ADMISSION_OPEN = "open"
#: The closing state: new spawns are refused; in-flight intents may still settle.
ADMISSION_CLOSING = "closing"
#: Env var carrying the replacement-scoped admission permit to the supervisor being
#: launched in a handoff (F-012). The restarter issues a single-use permit bound to
#: the current (CLOSING) epoch and the target release, and passes it to the
#: replacement via this env var; ``WorkerRegistrar.create_intent`` claims it
#: atomically so the replacement's first worker spawn succeeds while every
#: unrelated spawn stays refused.
ADMISSION_PERMIT_ENV = "REPOFORGE_ADMISSION_PERMIT"


class AdmissionEpochStore(Protocol):
    """Durable epoch state shared by the registrar and the restarter."""

    def read(self) -> tuple[int, str]:
        """Return ``(epoch, state)`` where state is ADMISSION_OPEN or ADMISSION_CLOSING."""
        ...

    def open_next(self) -> int:
        """Advance to and durably open epoch N+1; return the new epoch."""
        ...

    def close(self) -> int:
        """Durably close the current epoch; return the (unchanged) epoch number."""
        ...

    def issue_permit(self, *, target: str | None) -> str:
        """Issue a single-use replacement permit for the current epoch (F-012).

        Called by the restarter while admission is CLOSING, after the incumbent is
        drained, bound to the replacement's target release (or transition). The
        returned token is transported to the replacement, which must present it to
        ``claim_permit`` before it may spawn. Issuing rotates the slot: a previous
        handoff's permit is invalidated.
        """
        ...

    def claim_permit(self, epoch: int, *, token: str, target: str | None) -> bool:
        """Atomically claim the current epoch's permit; ``False`` if absent/used/wrong.

        Only valid while the epoch is CLOSING and the permit matches ``epoch``,
        ``token``, and (when bound) ``target``; a used permit is never reusable.
        A successful claim marks the permit used before returning.
        """
        ...
