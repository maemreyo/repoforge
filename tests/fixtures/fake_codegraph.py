from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.5.0"


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "version":
        print(VERSION)
        return 0
    if "__sleep__" in sys.argv:
        time.sleep(30)
        return 0
    if "__invalid_utf8__" in sys.argv:
        sys.stdout.buffer.write(b'{"value":"\xff"}\n')
        return 0
    spawn_argument = next(
        (argument for argument in sys.argv if argument.startswith("__spawn__:")),
        None,
    )
    if spawn_argument is not None:
        marker = Path(spawn_argument.split(":", 1)[1])
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=False,
        )
        marker.write_text(str(child.pid), encoding="utf-8")
        time.sleep(30)
        return 0
    if "__fail__" in sys.argv:
        print("provider-private-detail", file=sys.stderr)
        return 7
    if "__stale_lock__" in sys.argv:
        print("private stale lock path", file=sys.stderr)
        return 9
    payload = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "environment": dict(os.environ),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
