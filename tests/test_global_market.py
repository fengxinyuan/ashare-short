"""海外快照解析。

**夹具全部是 2026-08-20 从 `hq.sinajs.cn` 实测抓下来的原始行**，不是照着解析器
编的。改成实测的理由写在 `_sina_reading` 的 docstring 里：原来的夹具把 `gb_ixic`
的前收摆在第 25 位、`b_KOSPI` 的 `[8]` 填成 6365（而 `6258−(−37)=6295`），
两处都精确地迎合了当时解析器的错误取位，于是 `previous_close` 这个字段错了多久
就没被测到多久。
"""

from ashare_decision_support.adapters.global_market import (
    GROUP_COMMODITY,
    GROUP_INDEX,
    GROUP_US_SECTOR,
    SINA_INSTRUMENTS,
    GlobalMarketAdapter,
)

# 实测原始行。字段数分别是 36 / 36 / 13 / 15 / 19，三种布局都覆盖到。
LIVE_ROWS = """var hq_str_gb_ixic="纳斯达克,26331.0902,0.16,2026-08-20 09:48:02,41.3791,26289.7100,26400.1200,26200.5500,27190.2070,20690.2500,0,0,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,0.00,0.00,,Aug 19 05:15PM EDT,26289.7111,0,1,2026,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000";
var hq_str_gb_smh="半导体指数ETF-HOLDRs,560.9200,-1.55,2026-08-20 17:43:53,-8.8500,575.0600,576.8350,556.3150,671.8300,280.6350,8713226,7174457,5718537331,0.00,--,0.00,0.00,0.00,0.00,10194925,0,562.5391,0.29,1.62,Aug 20 05:43AM EDT,Aug 19 04:00PM EDT,569.7700,34258,1,2026,4905812348.5165,569.0000,560.8712,19346574.2932,565.9547,560.9200";
var hq_str_b_KOSPI="韩国KOSPI指数,6852.5800,381.41,5.89,2:27 AM,14:27:00,2026-08-20,14:32:40,6680.3400,6471.1700,6904.5500,6600.0900,0";
var hq_str_hf_CL="86.352,,86.320,86.330,86.610,84.230,17:43:58,84.390,84.400,0,4,2,2026-08-20,纽约原油,0";
var hq_str_fx_susdcny="02:52:32,6.7300,6.7600,6.7250,6.7400,,,,,,,,,,,,,2026-08-20";"""

LIVE_INSTRUMENTS = {
    "nasdaq": ("gb_ixic", "纳斯达克", "equity", GROUP_INDEX),
    "smh": ("gb_smh", "半导体ETF", "equity", GROUP_US_SECTOR),
    "kospi": ("b_KOSPI", "KOSPI", "index", GROUP_INDEX),
    "wti": ("hf_CL", "WTI原油", "future", GROUP_COMMODITY),
    "usdcny": ("fx_susdcny", "美元/人民币", "fx", "fx"),
}


def _of(snapshot, key):
    """测试按 key 读取快照，避免为测试向适配器增加专用接口。"""
    return next(item for item in snapshot.readings if item.key == key)


def _live_snapshot():
    return GlobalMarketAdapter(
        fetch_text=lambda _: LIVE_ROWS,
        sina_instruments=LIVE_INSTRUMENTS,
        yahoo_instruments={},
    ).fetch_snapshot()


def test_sina_snapshot_parses_all_four_layouts() -> None:
    snapshot = _live_snapshot()

    assert snapshot.ok is True
    assert [item.source for item in snapshot.readings] == ["sina"] * 5
    assert _of(snapshot, "nasdaq").change_pct == 0.16
    assert _of(snapshot, "smh").change_pct == -1.55
    assert _of(snapshot, "kospi").change_pct == 5.89
    assert _of(snapshot, "kospi").as_of == "2026-08-20 14:27:00"
    assert _of(snapshot, "wti").last == 86.352
    assert _of(snapshot, "usdcny").last == 6.73
    assert _of(snapshot, "usdcny").as_of == "2026-08-20 02:52:32"


def test_previous_close_is_the_previous_close_not_a_timestamp_or_the_open() -> None:
    """前收必须满足 `last - previous ≈ 涨跌额`。这是那两个 bug 的判据。

    美股那一路原来取 `[25]`，实测是 `'Aug 19 05:15PM EDT'`——`finite()` 把它判成
    None，于是恒为空；韩股那一路原来取 `[8]`，实测是**开盘价** 6680.34，而真前收
    是 `[9]` 的 6471.17。两处都不影响 `change_pct`（上游直接给），所以只表现为
    一个静默为空、一个静默偏移的字段。
    """
    snapshot = _live_snapshot()

    # 美股指数与 ETF：[26]
    assert _of(snapshot, "nasdaq").previous_close == 26289.7111
    assert _of(snapshot, "smh").previous_close == 569.77
    # 韩国指数：[9]，且**不是** [8] 的开盘价 6680.34
    assert _of(snapshot, "kospi").previous_close == 6471.17

    for key in ("nasdaq", "smh", "kospi"):
        item = _of(snapshot, key)
        implied = (item.last / item.previous_close - 1) * 100
        assert abs(implied - item.change_pct) < 0.02, f"{key} 前收与涨跌幅对不上"


def test_short_row_loses_only_the_previous_close() -> None:
    """字段被截断时前收留空，品种照样进快照。

    前收是次要读数——`change_pct` 不靠它算。为了取不到前收就把整个品种判成不可用，
    等于让一个次要字段决定主读数在不在。
    """
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: 'var hq_str_gb_ixic="纳斯达克,26331.09,0.16,2026-08-20 09:48:02";',
        sina_instruments={"nasdaq": ("gb_ixic", "纳斯达克", "equity", GROUP_INDEX)},
        yahoo_instruments={},
    ).fetch_snapshot()

    reading = _of(snapshot, "nasdaq")
    assert reading.last == 26331.09
    assert reading.change_pct == 0.16
    assert reading.previous_close is None
    assert reading.note is None


def test_readings_carry_their_group_and_sort_by_move() -> None:
    snapshot = _live_snapshot()

    assert _of(snapshot, "smh").group == GROUP_US_SECTOR
    assert _of(snapshot, "wti").group == GROUP_COMMODITY
    # 指数组按涨跌幅从高到低：KOSPI +5.89 在纳指 +0.16 前面。
    assert [item.key for item in snapshot.by_group(GROUP_INDEX)] == ["kospi", "nasdaq"]
    assert [item.key for item in snapshot.by_group(GROUP_US_SECTOR)] == ["smh"]


def test_by_group_skips_readings_without_a_move() -> None:
    """取不到涨跌幅的不进分组视图——排序里混一个 None 就是 TypeError。"""
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: 'var hq_str_gb_ixic="纳斯达克,26331.09,,2026-08-20 09:48:02";',
        sina_instruments={"nasdaq": ("gb_ixic", "纳斯达克", "equity", GROUP_INDEX)},
        yahoo_instruments={},
    ).fetch_snapshot()

    assert _of(snapshot, "nasdaq").last == 26331.09
    assert snapshot.by_group(GROUP_INDEX) == []


def test_unavailable_sina_source_is_visible_and_does_not_raise() -> None:
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: (_ for _ in ()).throw(OSError("offline")),
        sina_instruments={"nasdaq": ("gb_ixic", "纳斯达克", "equity", GROUP_INDEX)},
        yahoo_instruments={},
    ).fetch_snapshot()

    assert snapshot.ok is False
    assert snapshot.readings[0].last is None
    assert snapshot.readings[0].group == GROUP_INDEX
    assert "新浪海外行情不可用" in snapshot.notes[0]


def test_one_bad_instrument_does_not_take_the_others_down() -> None:
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: LIVE_ROWS,
        sina_instruments={
            **LIVE_INSTRUMENTS,
            "broken": ("gb_nope", "不存在的品种", "equity", GROUP_INDEX),
        },
        yahoo_instruments={},
    ).fetch_snapshot()

    assert snapshot.ok is True
    assert _of(snapshot, "broken").last is None
    assert "不可用" in _of(snapshot, "broken").note
    assert "海外快照缺失：不存在的品种" in snapshot.notes[-1]


def test_yahoo_is_opt_in_and_its_status_is_explicit() -> None:
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: "",
        sina_instruments={},
        yahoo_instruments={"vix": ("^VIX", "VIX")},
    ).fetch_snapshot()

    assert snapshot.readings[0].source == "yahoo"
    assert "未启用备用源" in snapshot.readings[0].note


def test_shipped_instrument_table_is_well_formed() -> None:
    """品种表是手写的，四元组写成三元组不会在导入时报错，只会在取数那一刻炸。"""
    kinds = {"equity", "future", "fx", "index"}
    groups = {GROUP_INDEX, GROUP_US_SECTOR, GROUP_COMMODITY, "fx"}
    symbols = [value[0] for value in SINA_INSTRUMENTS.values()]

    assert len(symbols) == len(set(symbols)), "品种表里有重复的新浪代码"
    for key, value in SINA_INSTRUMENTS.items():
        assert len(value) == 4, f"{key} 不是四元组"
        symbol, name, kind, group = value
        assert symbol and name, key
        assert kind in kinds, f"{key} 的解析口径 {kind!r} 不认识"
        assert group in groups, f"{key} 的分组 {group!r} 不认识"
        # 口径必须和代码前缀对得上——填错不报错，只会静默读到别的字段。
        expected = {"gb_": "equity", "hf_": "future", "fx_": "fx", "b_": "index"}
        assert kind == expected[symbol[: symbol.index("_") + 1]], f"{key} 口径与代码前缀不符"
