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
