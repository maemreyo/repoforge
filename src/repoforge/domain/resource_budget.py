"""Typed resource-budget policy shared by configuration and delta classification."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    max_cpu_seconds_per_operation: int = 900
    max_memory_bytes: int = 2 * 1024 * 1024 * 1024
    max_disk_bytes: int = 10 * 1024 * 1024 * 1024
    max_subprocesses: int = 8
    max_concurrent_operations: int = 4
    max_queued_operations: int = 32
    max_network_bytes: int = 100 * 1024 * 1024
    max_output_bytes: int = 120_000
    task_ttl_seconds: int = 86_400
    max_cache_bytes: int = 2 * 1024 * 1024 * 1024
    max_index_bytes: int = 2 * 1024 * 1024 * 1024
    max_provider_requests: int = 100


DEFAULT_RESOURCE_BUDGET = ResourceBudget()
RESOURCE_BUDGET_FIELDS = (
    "max_cpu_seconds_per_operation",
    "max_memory_bytes",
    "max_disk_bytes",
    "max_subprocesses",
    "max_concurrent_operations",
    "max_queued_operations",
    "max_network_bytes",
    "max_output_bytes",
    "task_ttl_seconds",
    "max_cache_bytes",
    "max_index_bytes",
    "max_provider_requests",
)


def resource_budget_values(budget: ResourceBudget) -> dict[str, int]:
    return {field: getattr(budget, field) for field in RESOURCE_BUDGET_FIELDS}
