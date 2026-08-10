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
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_RECEIVE_ID_TYPE",
        "FEISHU_RECEIVE_ID",
        "TICKFLOW_API_KEY",
        "TICKFLOW_BASE_URL",
        "TICKFLOW_RATE_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_implicit_snapshot_adapter(monkeypatch):
    """五档快照默认源在测试里必须显式注入，不能自己长出来。

    `OrderBookAgent` 在没拿到 adapter 时会构造一个真的 `SnapshotAdapter`——这在
    生产里是对的（要五档就得去取），在测试里是一次静默的外网请求：`run_advise`
    的每个测试都会打一次 `qt.gtimg.cn`。实测确实在打，只是网络快，看起来像没打。

    这类泄漏的坏处和上面那条凭据泄漏同源：**不会失败，只会悄悄变成另一件事**。
    断网的机器上它变成几秒钟的超时等待，网络好的机器上它让测试依赖一个第三方
    站点的可用性，而没有任何一条断言是关于那个站点的。

    要测五档行为的测试自己传 adapter（`OrderBookAgent(adapter=...)` 或
    `run_advise(snapshot_adapter=...)` / `run_monitor(snapshot_adapter=...)`），
    不受这条影响。
    """
    for target in (
        "ashare_decision_support.agents.collectors.default_snapshot_adapter",
        "ashare_decision_support.jobs.monitor.default_snapshot_adapter",
    ):
        monkeypatch.setattr(target, lambda: None)


@pytest.fixture(autouse=True)
def no_implicit_earnings_adapter(monkeypatch):
    """业绩源同样必须显式注入。

    和上面五档那条同源，但后果更重一级：五档泄漏只是多打一次外网、断言不受影响，
    业绩泄漏会**改变候选名单**——真实的业绩预告会把 fixture 里的标的按真实亏损
    情况否决掉。于是同一份 fixture 的结果开始取决于外网是否可达、以及今天恰好是
    哪个报告期的披露窗口。

    这类失败最难查的地方在于它**不稳定**：单跑那个文件时网络快、数据取到了是一
    种结果，整套跑时超时了又是另一种结果。要测业绩行为的测试自己传
    `run_morning(earnings_adapter=...)` / `run_advise(earnings_adapter=...)`，
    不受这条影响。

    逐个消费模块打补丁（而不是补 `adapters.earnings` 里的原件），是因为消费方用的
    是 `from ... import default_earnings_adapter`——那是一次值绑定，补原件补不到。
    与上面 `default_snapshot_adapter` 列两条目标是同一个道理。
    """
    # `morning` 那一路的消费方 2026-08-10 从 `jobs.morning` 搬到了 `jobs.sources`
    # （六路可选源收在一起）。这里跟着改的是**绑定点**，不是判据。
    for target in (
        "ashare_decision_support.jobs.sources.default_earnings_adapter",
        "ashare_decision_support.jobs.advise.default_earnings_adapter",
    ):
        monkeypatch.setattr(target, lambda: None)


@pytest.fixture(autouse=True)
def no_implicit_broad_index_adapter(monkeypatch):
    """宽基指数源同样必须显式注入。

    与上面五档、业绩两条同源。这一层单独说一句的理由是它**取数最像没在取数**：
    一次请求、几十毫秒、失败了也只是简报里少一节，任何断言都不会红。于是
    `run_morning` 的每个测试都会去打一次 `hq.sinajs.cn`，而没有一条断言是关于
    那个站点的。

    要测背离判据的测试直接调 `read_divergence(...)`（`tests/test_broad_index.py`），
    要测取数与解析的传 `BroadIndexAdapter(opener=...)`，都不受这条影响。
    """
    monkeypatch.setattr(
        "ashare_decision_support.jobs.sources.default_broad_index_adapter", lambda: None
    )


@pytest.fixture(autouse=True)
def no_implicit_sector_session(monkeypatch):
    """盘中板块监控同样必须显式注入。

    与上面几条同源，但这一条最贵也最像没在取数：`run_monitor(once=True)` 默认
    开着板块那一半，而板块的一轮是 **56 页并发的新浪全市场扫描**。它在
    `SectorSession.poll` 里被完整地 try/except 包着（板块是旁证，不该让个股监控
    那一轮跟着停），所以取数失败只落一条 note——**没有任何断言会红**。

    结果就是每个跑 `run_monitor` 的测试都在打一个第三方站点，网络好的时候看起来
    像没打。要测板块行为的测试自己传 `run_monitor(sector_session=...)`，或者直接
    调 `SectorSession(taxonomies=..., adapter=...)`，都不受这条影响。
    """
    monkeypatch.setattr(
        "ashare_decision_support.jobs.monitor.default_sector_session",
        lambda today, symbols: None,
    )


@pytest.fixture(autouse=True)
def no_implicit_trade_flow_adapter(monkeypatch):
    """逐笔资金源同样必须显式注入，而且这一条的代价最贵。

    一只标的要翻约 67 页明细，实测单只约 20 秒；盘前那批候选并发取完要两分多钟。
    泄漏到测试里的表现不是失败，是**整套测试从 11 秒变成 128 秒**——慢到没人愿意
    跑，但每一条断言都还是绿的。

    要测逐笔行为的测试传 `TencentDetailAdapter(opener=...)`
    （`tests/test_tencent_detail.py`）或 `run_morning(trade_flow_adapter=...)`。

    只有一条目标：`morning` 与 `post_close` 的取数都走 `jobs/sources.py` 之后，
    默认适配器只在那一处解析。这正是收口想要的形状——**漏掉一条 patch 目标**
    就是这条闸门唯一的失效方式。
    """
    monkeypatch.setattr(
        "ashare_decision_support.jobs.sources.default_trade_flow_adapter", lambda: None
    )
