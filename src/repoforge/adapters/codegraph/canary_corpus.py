"""Embedded, content-addressed semantic canary corpus."""

from __future__ import annotations

import hashlib

CANARY_FILES: tuple[tuple[str, bytes], ...] = (
    (
        "excluded/blocked_input.py",
        b"BLOCKED = 'must-not-project'\n",
    ),
    (
        "src/alpha.py",
        b"from src.beta import beta\n\n\ndef alpha() -> int:\n    return beta()\n",
    ),
    ("src/base.py", b"class Base:\n    def value(self) -> int:\n        return 1\n"),
    ("src/beta.py", b"def beta() -> int:\n    return 1\n"),
    ("src/duplicate_a.py", b"def duplicate() -> int:\n    return 1\n"),
    ("src/duplicate_b.py", b"def duplicate() -> int:\n    return 2\n"),
    ("src/service.py", b"from src.base import Base\n\n\nclass Service(Base):\n    pass\n"),
    (
        "tests/test_alpha.py",
        b"from src.alpha import alpha\n\n\ndef test_alpha() -> None:\n    assert alpha() == 1\n",
    ),
    ("tests/test_unrelated.py", b"def test_unrelated() -> None:\n    assert True\n"),
    ("unsupported/readme.txt", b"unsupported canary input\n"),
    ("web/leaf.ts", b"export function leaf(): number { return 1; }\n"),
    (
        "web/root.test.ts",
        b"import { root } from './root';\nroot();\n",
    ),
    (
        "web/root.ts",
        b"import { leaf } from './leaf';\nexport function root(): number { return leaf(); }\n",
    ),
)


def embedded_canary_digest() -> str:
    digest = hashlib.sha256()
    for path, data in CANARY_FILES:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


__all__ = ["CANARY_FILES", "embedded_canary_digest"]
