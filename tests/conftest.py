"""Test-wide isolation of persistent state.

Every test gets its own `ASHARE_DS_HOME`. Without this the jobs write to the
operator's real `~/.ashare-ds/` — the morning job in particular replaces the
candidate pool and appends to the journal, so running the suite would overwrite
today's actual candidates with fixture data.

Autouse and session-scoped because the leak is silent: nothing fails, the wrong
directory just quietly gains records.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ASHARE_DS_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """Keep tests off the network even when the shell has provider keys set."""
    for name in (
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
        "KIMI_API_KEY",
        "FEISHU_WEBHOOK_URL",
        "FEISHU_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
