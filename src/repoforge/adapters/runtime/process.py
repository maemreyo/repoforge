from .state_store import process_identity


class SystemProcessInspector:
    def identity(self, pid: int) -> str | None:
        return process_identity(pid)
