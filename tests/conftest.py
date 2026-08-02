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
    """Keep tests off the network even when the shell has provider keys set.

    `TICKFLOW_API_KEY` belongs here for a second reason, and it is the more
    dangerous one. `TickFlowAdapter.__init__` falls back to the environment
    (`api_key or os.getenv(...)`), so a test that passes `api_key=None` to build
    a **free-tier** adapter silently gets a full-tier one on any machine that has
    a key exported — which, since the Pro tier was activated, is every machine
    that actually runs this system.

    The tests keep their names and keep passing on a laptop with no key, so the
    free-tier degradation path — the one that has to work when the subscription
    lapses — quietly stops being covered. Three `..._free_mode_...` tests were
    asserting on paid-mode behaviour before this line existed.

    Tests that *want* full mode pass `api_key="key"` explicitly and are unaffected.
    """
    for name in (
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
        "KIMI_API_KEY",
        "FEISHU_WEBHOOK_URL",
        "FEISHU_SECRET",
        "TICKFLOW_API_KEY",
        "TICKFLOW_BASE_URL",
        "TICKFLOW_RATE_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
