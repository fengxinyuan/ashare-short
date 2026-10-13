"""盘前研报输入包：海外那一节的组织与排版。

这一节此前**零测试覆盖**——`tests/test_cli_wiring.py` 只断言 `build_preopen_research`
这个名字存在。品种表从 9 个铺到 44 个之后，「怎么折叠」变成了这一节的主要判断，
所以补上。
"""

from ashare_decision_support.adapters.global_market import (
    GROUP_COMMODITY,
    GROUP_INDEX,
    GROUP_US_SECTOR,
    GlobalMarketReading,
    GlobalMarketSnapshot,
)
from ashare_decision_support.research.preopen import (
    OVERSEAS_HEAD,
    OVERSEAS_QUIET_PCT,
    _base_rate_lines,
    _movers,
    _overseas_lines,
    _overseas_payload,
    build_preopen_research,
)


def test_preopen_context_has_schema_and_forces_reproducible_phase(monkeypatch) -> None:
    from ashare_decision_support.research import preopen

    captured = {}

    class Bundle:
        def to_dict(self, **kwargs):
            return {"phase": "pre_open"}

    def fake_bundle(**kwargs):
        captured.update(kwargs)
        return Bundle()

    monkeypatch.setattr(preopen, "build_facts_bundle", fake_bundle)
    monkeypatch.setattr(preopen.TradingCalendar, "is_trading_day", lambda self, day: True)

    payload = build_preopen_research(market_adapter=None)

    assert payload["schema"] == "ashare-preopen-research-context-v1"
    assert captured["phase"] == "pre_open"
    assert "盘前主链路" in captured["sections"]
    assert "板块预测盘中验证" not in captured["sections"]


def _reading(key: str, change_pct: float | None, group: str = GROUP_US_SECTOR):
    return GlobalMarketReading(
        key=key,
        name=key.upper(),
        symbol=f"gb_{key}",
        source="sina",
        last=100.0,
        previous_close=99.0,
        change_pct=change_pct,
        as_of="2026-08-20 09:48:02",
        group=group,
    )


class _Adapter:
    def __init__(self, readings, notes=()):
        self._snapshot = GlobalMarketSnapshot(readings=list(readings), notes=list(notes))

    def fetch_snapshot(self):
        return self._snapshot


def test_no_adapter_means_no_payload_at_all() -> None:
    assert _overseas_payload(None) is None


def test_payload_keeps_every_reading_even_when_the_summary_truncates() -> None:
    """摘要取前几名，`readings` 必须仍是全量。

    否则「铜矿 ETF 昨夜怎么样」在这个包里就没有答案了——而外部 AI 拿到的正是
    这个包。
    """
    readings = [_reading(f"s{i}", float(i)) for i in range(1, 30)]
    payload = _overseas_payload(_Adapter(readings))

    assert len(payload["readings"]) == 29
    block = next(item for item in payload["groups"] if item["group"] == GROUP_US_SECTOR)
    assert block["total"] == 29
    assert len(block["movers"]) == OVERSEAS_HEAD * 2


def test_movers_takes_both_ends_not_just_the_winners() -> None:
    """跌得最惨的那几个和涨得最猛的那几个同样是信息。

    只取涨幅榜会让「昨夜费半 −2.1%」整条消失，而那恰好是最该被看到的一条。
    """
    rows = sorted(
        [
            _reading("up1", 9.0),
            _reading("up2", 8.0),
            _reading("mid", 0.6),
            *[_reading(f"f{i}", 5.0 - i * 0.1) for i in range(12)],
            _reading("dn1", -7.0),
            _reading("dn2", -6.0),
        ],
        key=lambda item: item.change_pct,
        reverse=True,
    )
    keys = [item.key for item in _movers(rows)]

    assert keys[:2] == ["up1", "up2"]
    assert keys[-2:] == ["dn2", "dn1"]
    assert len(keys) == OVERSEAS_HEAD * 2


def test_movers_drops_the_quiet_ones() -> None:
    quiet = OVERSEAS_QUIET_PCT / 2
    rows = [_reading("loud", 3.0), _reading("quiet", quiet), _reading("still", -quiet)]

    assert [item.key for item in _movers(rows)] == ["loud"]


def test_movers_below_the_cap_returns_them_all_without_duplicating() -> None:
    """两端各取会在总数不足时重复元素——所以不足两倍上限时整批返回。"""
    rows = [_reading("a", 3.0), _reading("b", -3.0)]

    assert [item.key for item in _movers(rows)] == ["a", "b"]


def test_not_requested_and_nothing_moved_read_differently() -> None:
    """这两件事对开盘预案是相反的信息，不能长成同一段空白。"""
    not_requested = _overseas_lines({})
    quiet = _overseas_lines(_overseas_payload(_Adapter([_reading("flat", 0.1, GROUP_COMMODITY)])))

    assert not_requested == ["- 未请求海外快照；不对隔夜风险偏好作数值判断。"]
    assert any("隔夜平静，不是没取到" in line for line in quiet)
    assert not any("未请求" in line for line in quiet)


def test_lines_group_by_block_and_show_the_move() -> None:
    payload = _overseas_payload(
        _Adapter(
            [
                _reading("sox", -2.12, GROUP_INDEX),
                _reading("smh", -1.55, GROUP_US_SECTOR),
                _reading("copper", 3.17, GROUP_COMMODITY),
            ]
        )
    )
    lines = _overseas_lines(payload)
    text = "\n".join(lines)

    assert "**指数与风险偏好**（1/1 个动了）" in text
    assert "SOX -2.12%" in text
    assert "COPPER +3.17%" in text
    assert "前收 99.0" in text


def test_unavailable_instruments_are_listed_not_silently_dropped() -> None:
    broken = GlobalMarketReading(
        key="vix", name="VIX", symbol="^VIX", source="yahoo", note="未启用备用源"
    )
    payload = _overseas_payload(_Adapter([_reading("smh", -1.55), broken]))
    text = "\n".join(_overseas_lines(payload))

    assert "未覆盖：VIX（未启用备用源）" in text
    # 取不到读数的不该混进任何一组的排序里。
    assert all(item["key"] != "vix" for block in payload["groups"] for item in block["movers"])


def test_overseas_payload_carries_cached_base_rates(monkeypatch) -> None:
    """外盘映射不只给机制，也带本地历史基准率。

    这一层只读缓存，不打网络；缓存不存在时是 notes，不应该让盘前研报断掉。
    """
    from ashare_decision_support.research import preopen

    monkeypatch.setattr(
        preopen,
        "load_overseas_series",
        lambda: {
            "sox": [
                {"date": "20260101", "close": 100.0},
                {"date": "20260102", "close": 96.0},
                {"date": "20260103", "close": 97.0},
            ]
        },
    )
    monkeypatch.setattr(
        preopen,
        "load_sector_series",
        lambda: {
            "881001": (
                "半导体",
                [
                    {"date": "20260102", "close": 100.0},
                    {"date": "20260103", "close": 102.0},
                    {"date": "20260104", "close": 103.0},
                ],
            ),
            "881002": (
                "消费电子",
                [
                    {"date": "20260102", "close": 100.0},
                    {"date": "20260103", "close": 99.0},
                    {"date": "20260104", "close": 100.0},
                ],
            ),
        },
    )

    payload = _overseas_payload(_Adapter([_reading("sox", -3.5, GROUP_INDEX)]))
    rates = payload["sector_base_rates"]

    assert rates["ok"] is True
    assert {item["sector"] for item in rates["items"]} >= {"半导体", "消费电子"}
    assert all(item["instrument"] == "sox" for item in rates["items"])
    assert any("外盘映射历史基准率" in line for line in _base_rate_lines(rates))


def test_base_rates_explain_missing_history_without_hiding_links(monkeypatch) -> None:
    from ashare_decision_support.research import preopen

    monkeypatch.setattr(preopen, "load_overseas_series", dict)
    monkeypatch.setattr(preopen, "load_sector_series", dict)

    payload = _overseas_payload(_Adapter([_reading("sox", -3.5, GROUP_INDEX)]))

    assert payload["sector_links"]["links"]
    assert payload["sector_base_rates"]["ok"] is False
    assert "history-refresh" in payload["sector_base_rates"]["notes"][0]


def test_base_rates_with_only_zero_sample_rows_are_not_ok(monkeypatch) -> None:
    """有条目不等于有样本；状态要按可用历史判断。"""
    from ashare_decision_support.research import preopen

    monkeypatch.setattr(
        preopen,
        "load_overseas_series",
        lambda: {
            "sox": [
                {"date": "20260101", "close": 100.0},
                {"date": "20260102", "close": 96.0},
            ]
        },
    )
    monkeypatch.setattr(
        preopen,
        "load_sector_series",
        lambda: {
            "881999": (
                "没有映射的板块",
                [
                    {"date": "20260103", "close": 100.0},
                    {"date": "20260104", "close": 101.0},
                ],
            )
        },
    )

    payload = _overseas_payload(_Adapter([_reading("sox", -3.5, GROUP_INDEX)]))
    rates = payload["sector_base_rates"]

    assert rates["items"]
    assert all(item["samples"] == 0 for item in rates["items"])
    assert rates["ok"] is False


def test_render_preopen_research_adds_actionable_sections() -> None:
    from ashare_decision_support.research.preopen import render_preopen_research

    payload = {
        "as_of": "2026-08-26T20:09:54+08:00",
        "facts": {
            "notes": ["盘前主链路的数据日期是 2026-08-26"],
            "facts": {
                "盘前主链路": {
                    "market": {
                        "direction": "中性偏多",
                        "score": 67.02,
                        "breadth": 0.5312,
                        "median_ret_5d": 0.02081,
                        "warnings": ["宽基指数停在 2026-08-26，等权读数是 2026-08-25"],
                    },
                    "rotation_price": {
                        "switch_label": "高低切换",
                        "persistent": ["有色金属", "医药生物"],
                    },
                    "turnover_migration": {
                        "total_change": -0.1341,
                        "total_trend": -0.2710,
                        "rising": [
                            {"name": "贵金属", "quadrant": "放量上涨"},
                            {"name": "生物制品", "quadrant": "放量下跌"},
                        ],
                        "falling": [{"name": "软件开发"}],
                        "distribution": [{"name": "生物制品"}],
                    },
                    "limit_pool": {
                        "limit_up": 52,
                        "blowup_rate": 0.278,
                        "max_streak": 5,
                    },
                    "sectors": [
                        {
                            "name": "有色金属",
                            "direction": "偏多",
                            "score": 74.55,
                            "ret_1d": 0.007895,
                            "ret_5d": 0.028007,
                        }
                    ],
                }
            },
        },
        "overseas_snapshot": {
            "readings": [{"name": "费城半导体", "last": 100.0}, {"name": "VIX", "last": None}],
            "groups": [],
            "sector_links": {"links": []},
            "sector_base_rates": {"ok": False, "items": [], "notes": ["缺少历史"]},
        },
    }

    rendered = render_preopen_research(payload)

    assert "## 总判断" in rendered
    assert "## 先手观察" in rendered
    assert "## 只等确认" in rendered
    assert "## 风险" in rendered
    assert "贵金属放量上涨" in rendered
    assert "生物制品放量下跌" in rendered
    assert "软件开发缩量走弱" in rendered
    assert "偏热" not in rendered
    assert "海外快照缺少 VIX" in rendered
