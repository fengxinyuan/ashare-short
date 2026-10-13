"""调度入口必须遵守 CLI 的模型开关、新闻预热范围与通知配置。"""

from __future__ import annotations

import pytest

from ashare_decision_support import cli
from ashare_decision_support.jobs import daily_cycle
from ashare_decision_support.state import Positions
from tests.test_daily_cycle_wiring import _tasks


@pytest.mark.parametrize("disabled", [False, True])
def test_daemon_cli_forwards_the_news_model_switch(monkeypatch, tmp_path, disabled):
    seen = {}

    def run(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(cli, "run_daily_cycle", run)
    argv = ["daemon", "--dry-run", "--quiet", "--output-dir", str(tmp_path)]
    if disabled:
        argv.append("--no-llm-news")
    args = cli.build_parser().parse_args(argv)
    cli.COMMANDS["daemon"](args)

    assert seen["use_llm_news"] is (not disabled)


@pytest.mark.parametrize("enabled", [False, True])
def test_scheduled_morning_honours_the_news_model_switch(monkeypatch, tmp_path, enabled):
    seen = {}

    def morning(sources, output_dir, **kwargs):
        seen["config"] = kwargs["config"]
        return {}

    monkeypatch.setattr(daily_cycle, "run_morning", morning)
    tasks = _tasks(monkeypatch, tmp_path, use_llm_news=enabled)["tasks"]
    tasks["morning"].run()

    assert seen["config"].use_llm_news is enabled


def test_scheduled_postclose_uses_the_configured_notification_channels(monkeypatch, tmp_path):
    Positions().open("600519", 100)
    channels = [object()]
    seen = {}

    def postclose(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(daily_cycle, "run_post_close", postclose)
    tasks = _tasks(monkeypatch, tmp_path, notifier_factory=lambda slug: channels)["tasks"]
    tasks["postclose"].run()

    assert seen["notifiers"] is channels
    assert seen["output_dir"] == str(tmp_path)


def test_scheduled_news_prewarms_the_same_composite_used_by_morning(monkeypatch, tmp_path):
    calls = []

    class Composite:
        def fetch_market(self, day, limit=200):
            calls.append((day, limit))
            return list(range(7)), []

        def fetch_announcements(self, day):
            return [{"symbol": "600519"}], []

    class AkOnly(Composite):
        def __init__(self, **kwargs):
            pass

        def fetch_market(self, day, limit=200):
            return [], []

    monkeypatch.setattr(daily_cycle, "AkshareNewsSource", AkOnly, raising=False)
    monkeypatch.setattr(
        daily_cycle, "default_news_source", lambda output: Composite(), raising=False
    )
    tasks = _tasks(monkeypatch, tmp_path)["tasks"]
    result = tasks["news"].run()

    assert result["docs"] == 7
    assert result["announcements"] == 1
    assert calls[0][1] == 600
