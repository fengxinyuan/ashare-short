"""Where persistent state lives.

Everything the automation layer remembers between runs goes under one root, so
"what does the system currently think" is answerable by listing one directory.
`ASHARE_DS_HOME` overrides it, which is what the tests use.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "ASHARE_DS_HOME"
DEFAULT_HOME = Path.home() / ".ashare-ds"


def state_home() -> Path:
    """Root of the state directory, created on first use."""
    raw = os.environ.get(ENV_HOME)
    root = Path(raw).expanduser() if raw else DEFAULT_HOME
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_path(*parts: str) -> Path:
    """Path under the state root, with parent directories created."""
    path = state_home().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
