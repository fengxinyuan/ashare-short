"""逐用例隔离持仓、台账、环境凭据和默认在线数据源。"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from ashare_decision_support.adapters.lhb_seats import LhbSeatAdapter
from ashare_decision_support.adapters.limit_pool import LimitPoolSnapshot
from ashare_decision_support.news import AkshareNewsSource


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ASHARE_DS_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """避免本机凭据触发真实通知、模型调用或改变免费接口测试的权限。"""
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
        "ASHARE_DS_ENV_FILE",
        "ASHARE_DS_SOCIAL_HEAT_PATH",
        "ASHARE_DS_THEME_CATALOG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    # CLI 入口不得重新载入工作区的个人配置；dotenv 专项用例自行重置加载器。
    monkeypatch.setattr("ashare_decision_support.envfile._loaded", [])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """拦截 DNS 与 TCP 访问；即使业务捕获异常并降级，测试也必须报告漏网请求。"""
    attempts = []
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def blocked(*args, **kwargs):
        attempts.append("网络访问未注入测试替身")
        raise OSError(attempts[-1])

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return blocked()
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return blocked()
        return connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    assert not attempts, f"离线测试尝试了 {len(attempts)} 次网络访问，请显式注入数据源或关闭取数"


@pytest.fixture(autouse=True)
def no_implicit_data_sources(monkeypatch):
    """默认取数工厂统一返回空；需要数据的用例显式注入适配器或会话。

    消费方使用 from-import，因此补丁必须打在绑定点。省略任一工厂会让
    job 测试静默访问行情站点，依赖实时公告、行情或网络超时。
    """
    for target in (
        "ashare_decision_support.agents.collectors.default_snapshot_adapter",
        "ashare_decision_support.jobs.monitor.default_snapshot_adapter",
        "ashare_decision_support.jobs.sources.default_earnings_adapter",
        "ashare_decision_support.jobs.sources.default_broad_index_adapter",
        "ashare_decision_support.jobs.sources.default_trade_flow_adapter",
        "ashare_decision_support.jobs.post_close.default_broad_index_adapter",
        "ashare_decision_support.jobs.post_close.default_global_market_adapter",
        "ashare_decision_support.jobs.post_close.default_macro_news_source",
    ):
        monkeypatch.setattr(target, lambda: None)
    monkeypatch.setattr(
        "ashare_decision_support.jobs.monitor.default_sector_session",
        lambda today, symbols, sector_plan=None: None,
    )
    monkeypatch.setattr(
        "ashare_decision_support.jobs.sources.LimitPoolAdapter",
        lambda: SimpleNamespace(fetch_recent=lambda day: LimitPoolSnapshot(day, ok=False)),
    )
    monkeypatch.setattr(
        "ashare_decision_support.jobs.sources.LhbSeatAdapter",
        lambda: LhbSeatAdapter(ak_module=SimpleNamespace()),
    )
    for target in (
        "ashare_decision_support.jobs.monitor.AkshareNewsSource",
        "ashare_decision_support.jobs.sources.AkshareNewsSource",
    ):
        monkeypatch.setattr(
            target,
            lambda cache=None: AkshareNewsSource(ak_module=SimpleNamespace(), cache=cache),
        )
    monkeypatch.setattr(
        "ashare_decision_support.jobs.daily_cycle.default_news_source",
        lambda output: AkshareNewsSource(ak_module=SimpleNamespace()),
    )
