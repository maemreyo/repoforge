from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_config_import_succeeds_in_a_fresh_python_process() -> None:
    root = Path(__file__).parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from repoforge.config import RepositoryConfig, load_config; "
            "assert RepositoryConfig is not None; assert load_config is not None",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
