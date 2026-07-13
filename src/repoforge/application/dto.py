from __future__ import annotations
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast


def to_data(value: Any) -> Any:
    if is_dataclass(value):
        return to_data(asdict(cast(Any, value)))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_data(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_data(v) for v in value]
    return value
