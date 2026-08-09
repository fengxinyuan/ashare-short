from ashare_decision_support.adapters.global_market import GlobalMarketAdapter


def test_sina_snapshot_parses_equity_future_fx_and_korean_index() -> None:
    raw = """var hq_str_gb_ixic="NASDAQ,100,1.25,2026-08-08 05:30:00,,,,,,,,,,,,,,,,,,,,,,99,0";
var hq_str_hf_CL="77,,,,,,,76,,,,,2026-08-08,WTI,0";
var hq_str_fx_susdcny="02:52:32,6.73,6.76,,,,,,,,,,,,,,,2026-08-08";
var hq_str_b_KOSPI="KOSPI,6258,-37,-0.60,2:27 AM,14:27:00,2026-08-07,,6365";"""
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: raw,
        sina_instruments={
            "nasdaq": ("gb_ixic", "纳斯达克", "equity"),
            "wti": ("hf_CL", "WTI原油", "future"),
            "usdcny": ("fx_susdcny", "美元/人民币", "fx"),
            "kospi": ("b_KOSPI", "KOSPI", "index"),
        },
        yahoo_instruments={},
    ).fetch_snapshot()

    assert snapshot.ok is True
    assert [item.source for item in snapshot.readings] == ["sina"] * 4
    assert snapshot.readings[0].change_pct == 1.25
    assert snapshot.readings[1].change_pct == 1.3158
    assert snapshot.readings[2].last == 6.73
    assert snapshot.readings[2].as_of == "2026-08-08 02:52:32"
    assert snapshot.readings[3].change_pct == -0.6


def test_unavailable_sina_source_is_visible_and_does_not_raise() -> None:
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: (_ for _ in ()).throw(OSError("offline")),
        sina_instruments={"nasdaq": ("gb_ixic", "纳斯达克", "equity")},
        yahoo_instruments={},
    ).fetch_snapshot()

    assert snapshot.ok is False
    assert snapshot.readings[0].last is None
    assert "新浪海外行情不可用" in snapshot.notes[0]


def test_yahoo_is_opt_in_and_its_status_is_explicit() -> None:
    snapshot = GlobalMarketAdapter(
        fetch_text=lambda _: "",
        sina_instruments={},
        yahoo_instruments={"vix": ("^VIX", "VIX")},
    ).fetch_snapshot()

    assert snapshot.readings[0].source == "yahoo"
    assert "未启用备用源" in snapshot.readings[0].note
