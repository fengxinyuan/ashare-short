from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from ashare_decision_support.jobs import run_post_close, run_pre_open
from ashare_decision_support.notify import FileNotifier, split_utf8_text
from ashare_decision_support.notify.base import NotifierCapabilities
from ashare_decision_support.scoring import RuleBasedScoreProvider, ScoreInput
from ashare_decision_support.signals import DailyTechnicalAnalyzer, WyckoffAnalyzer


def make_bars(symbol: str, count: int = 140, start_price: float = 20.0, drift: float = 0.15) -> list[dict]:
    """Synthetic daily bars with a steady drift, enough to fill the MA60 window."""
    bars: list[dict] = []
    price = start_price
    day = date(2026, 1, 5)
    for index in range(count):
        price = max(price + drift, 1.0)
        bars.append(
            {
                "symbol": symbol,
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": round(price - 0.1, 2),
                "high": round(price + 0.3, 2),
                "low": round(price - 0.4, 2),
                "close": round(price, 2),
                "volume": 100000 + index * 100,
                "amount": (100000 + index * 100) * price * 100,
            }
        )
    return bars


class FakeAdapter:
    """Adapter stub that serves canned bars without touching the network."""

    def __init__(self, bars_by_symbol: dict[str, list[dict]], mode: str = "free") -> None:
        self.bars_by_symbol = bars_by_symbol
        self.mode = mode
        self.calls = 0

    def get_historical_bars(self, symbols, start, end):
        self.calls += 1
        return {symbol: self.bars_by_symbol.get(symbol, []) for symbol in symbols}

    def health_check(self):
        return {
            "mode": self.mode,
            "capabilities": {"supports_intraday": self.mode == "full"},
        }


class FakeTradeFlow:
    def __init__(self, symbol: str, buy_ratio: float, net_amount: float, large_net_amount: float) -> None:
        self.symbol = symbol
        self.buy_ratio = buy_ratio
        self.net_amount = net_amount
        self.large_net_amount = large_net_amount

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "ok": True,
            "prints_count": 42,
            "buy_amount": 5_000_000.0,
            "sell_amount": 3_000_000.0,
            "neutral_amount": 200_000.0,
            "net_amount": self.net_amount,
            "buy_ratio": self.buy_ratio,
            "large_threshold_amount": 180_000.0,
            "large_buy_amount": 2_400_000.0,
            "large_sell_amount": 1_100_000.0,
            "large_net_amount": self.large_net_amount,
            "tail_net_amount": 600_000.0,
            "tail_ratio": 0.24,
            "notes": [],
        }


class FakeDetailAdapter:
    def get_trade_flow(self, symbols):
        return (
            {
                symbol: FakeTradeFlow(
                    symbol=symbol,
                    buy_ratio=0.62,
                    net_amount=2_000_000.0,
                    large_net_amount=1_300_000.0,
                )
                for symbol in symbols
            },
            ["逐笔资金旁证已附到测试标的"],
        )


def score_for(symbol: str, bars: list[dict]):
    snapshot = DailyTechnicalAnalyzer().analyze(symbol, bars)
    wyckoff = WyckoffAnalyzer().analyze(symbol, bars)
    return RuleBasedScoreProvider().score(ScoreInput(symbol=symbol, bars=bars, snapshot=snapshot, wyckoff=wyckoff))


def test_rule_based_scoring_prefers_uptrend_over_downtrend() -> None:
    up = score_for("UP.SH", make_bars("UP.SH", drift=0.2))
    down = score_for("DOWN.SH", make_bars("DOWN.SH", start_price=60.0, drift=-0.2))

    assert up.score > down.score
    assert 0.0 <= down.score <= 100.0 and 0.0 <= up.score <= 100.0
    assert up.reasons, "an actionable score must explain itself"


def test_rule_based_scoring_flags_thin_history() -> None:
    result = score_for("SHORT.SH", make_bars("SHORT.SH", count=30))
    assert any("样本" in warning for warning in result.warnings)


def test_split_utf8_text_respects_byte_limit_for_chinese() -> None:
    chunks = split_utf8_text("中文测试内容\n" * 20, 60)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 60 for chunk in chunks)


def test_file_notifier_reports_failure_instead_of_raising(tmp_path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    notifier = FileNotifier(blocker / "sub", slug="x")

    assert notifier.send("t", "c") is False


def test_post_close_then_pre_open_round_trip(tmp_path) -> None:
    bars = {"AAA.SH": make_bars("AAA.SH"), "BBB.SZ": make_bars("BBB.SZ", start_price=30.0, drift=0.05)}
    adapter = FakeAdapter(bars)

    summary = run_post_close(
        adapter=adapter,
        symbols=["AAA.SH", "BBB.SZ"],
        output_dir=tmp_path,
        # 涨停池要联网，且这条链路测的是选股段不是情绪判定。关掉后 regime 走
        # UNKNOWN 分支，仓位上限按退潮档——这正是「未知不等于中性」的预期行为。
        use_limit_pool=False,
    )

    # 自选股只标注不剔除：两只都必须出现在某一侧。
    assert summary["scored"] == 2
    assert summary["actionable"] + summary["watch_only"] == 2
    assert summary["regime"] == "未知"
    assert Path(summary["brief_path"]).name == f"post-close-{summary['trade_date'].replace('-', '')}.md"

    payload_path = tmp_path / "post_close_latest.json"
    assert payload_path.exists()

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    rows = payload["actionable"] + payload["watch_only"]
    assert {row["symbol"] for row in rows} == {"AAA.SH", "BBB.SZ"}
    assert all(row["score"] is not None for row in rows)
    # 每一行都要能回答「凭哪条打法」或「为什么没有打法」，二者必居其一。
    for row in rows:
        setup = row["setup"]
        assert setup["matched"] or setup["rejected"], row["symbol"]
    assert payload["regime"]["cycle"] == "未知"

    calls_after_post_close = adapter.calls
    pre_open = run_pre_open(payload_path=payload_path, notifiers=[])

    assert pre_open["candidates"] == len(payload["actionable"])
    assert "09:24:58" in pre_open["content"], "the auction cutoff rule must be on the pre-open card"
    assert adapter.calls == calls_after_post_close, "pre-open must reuse the snapshot, not refetch"


def test_post_close_reports_why_a_name_is_not_buyable(tmp_path) -> None:
    """没有可用打法时，理由必须出现在卡上——「没选上」不是一个答案。

    两条不同的机制都要覆盖到，因为它们在数据里长得不一样：
      判据不过   → `setup.rejected` 带一句话（「超跌但无反转触发……属于接刀」）
      周期挡掉   → `setup.blocked_by_regime` 带打法名，`rejected` 是空的
    一路下跌的票在 UNKNOWN 档正好走第二条：DOWNTREND 只允许超跌反转，而 UNKNOWN
    档只留回踩，交集为空——**一条判据都不会被验**。
    """
    bars = {"FALL.SH": make_bars("FALL.SH", start_price=60.0, drift=-0.25)}
    summary = run_post_close(
        adapter=FakeAdapter(bars),
        symbols=["FALL.SH"],
        output_dir=tmp_path,
        notifiers=[],
        use_limit_pool=False,
    )

    payload = json.loads((tmp_path / "post_close_latest.json").read_text(encoding="utf-8"))
    assert summary["actionable"] == 0 and summary["watch_only"] == 1

    setup = payload["watch_only"][0]["setup"]
    explained = [
        text for entry in setup["rejected"] for text in entry["reasons"]
    ] + list(setup["blocked_by_regime"])
    assert explained, "「为什么不能买」必须被序列化出来，否则卡上只剩「没选上」"

    assert "只能看，不能做" in summary["content"]
    assert "超跌反转" in summary["content"], "被周期挡掉的那条打法要指名道姓"


def test_post_close_attaches_trade_flow_side_reading(tmp_path) -> None:
    bars = {"AAA.SH": make_bars("AAA.SH"), "BBB.SZ": make_bars("BBB.SZ", start_price=30.0, drift=0.05)}

    summary = run_post_close(
        adapter=FakeAdapter(bars),
        symbols=["AAA.SH", "BBB.SZ"],
        output_dir=tmp_path,
        notifiers=[],
        use_limit_pool=False,
        detail_adapter=FakeDetailAdapter(),
    )

    payload = json.loads((tmp_path / "post_close_latest.json").read_text(encoding="utf-8"))
    rows = payload["actionable"] + payload["watch_only"]

    assert rows and all("trade_flow" in row for row in rows)
    assert rows[0]["trade_flow"]["buy_ratio"] == 0.62
    assert "资金旁证" in summary["content"]
    assert "大单净额" in summary["content"]


def test_post_close_attaches_only_disclosed_lhb_seats(tmp_path) -> None:
    class Seats:
        def fetch(self, symbol, trade_date):
            class Read:
                def to_dict(self):
                    return {
                        "symbol": symbol,
                        "trade_date": str(trade_date),
                        "covered": symbol == "AAA.SH",
                        "net_by_category": {"机构专用": 12_000_000.0},
                        "concentration": 0.08,
                    }
            return Read()

    run_post_close(
        adapter=FakeAdapter({"AAA.SH": make_bars("AAA.SH"), "BBB.SZ": make_bars("BBB.SZ")}),
        symbols=["AAA.SH", "BBB.SZ"], output_dir=tmp_path, notifiers=[], use_limit_pool=False,
        use_trade_flow=False, lhb_seat_adapter=Seats(),
    )

    payload = json.loads((tmp_path / "post_close_latest.json").read_text(encoding="utf-8"))
    rows = {row["symbol"]: row for row in payload["actionable"] + payload["watch_only"]}
    assert rows["AAA.SH"]["lhb_seats"]["covered"] is True
    assert "lhb_seats" not in rows["BBB.SZ"]


def test_post_close_notes_missing_symbols_and_free_tier_limits(tmp_path) -> None:
    adapter = FakeAdapter({"AAA.SH": make_bars("AAA.SH")})

    summary = run_post_close(
        adapter=adapter,
        symbols=["AAA.SH", "GONE.SZ"],
        output_dir=tmp_path,
        notifiers=[],
        use_limit_pool=False,
    )

    notes = " ".join(summary["data_notes"])
    assert "GONE.SZ" in notes
    assert "免费档" in notes
    assert "前复权" in notes
    assert summary["scored"] == 1 and summary["requested"] == 2


def test_pre_open_survives_a_corrupt_snapshot_file(tmp_path) -> None:
    adapter = FakeAdapter({"AAA.SH": make_bars("AAA.SH")})
    run_post_close(
        adapter=adapter,
        symbols=["AAA.SH"],
        output_dir=tmp_path,
        notifiers=[],
        use_limit_pool=False,
    )
    (tmp_path / "snapshots_latest.json").write_text("{not json", encoding="utf-8")

    summary = run_pre_open(payload_path=tmp_path / "post_close_latest.json", notifiers=[])

    assert any("快照读取失败" in note for note in summary["data_notes"])


def test_a_notifier_that_raises_does_not_lose_a_finished_brief(tmp_path) -> None:
    """投递是**副作用**，不是结果。一份已经算完的盘后简报，不该因为某个渠道的
    webhook 抛了异常就整个丢掉——那是让最不重要的一环决定最重要的一环。

    内置的三个 notifier 恰好都不抛，所以此前六处 `[c... if c.send(...)]` 的写法
    一直没出事；但它守的是 `Notifier` 的契约而不是一个事实，自定义渠道一旦违约，
    崩的是已经算完的那份产物。
    """

    class _Recording:
        capabilities = NotifierCapabilities(channel="recording", markdown=True, max_bytes=1000)

        def __init__(self) -> None:
            self.messages: list[tuple[str, str]] = []

        def send(self, title: str, content: str) -> bool:
            self.messages.append((title, content))
            return True

    class Exploding:
        capabilities = NotifierCapabilities(channel="exploding", markdown=True, max_bytes=1000)

        def send(self, title: str, content: str) -> bool:
            raise RuntimeError("webhook 挂了")

    recording = _Recording()
    adapter = FakeAdapter({"AAA.SH": make_bars("AAA.SH")})

    summary = run_post_close(
        adapter=adapter,
        symbols=["AAA.SH"],
        output_dir=tmp_path,
        notifiers=[Exploding(), recording],
        use_limit_pool=False,
    )

    assert summary["scored"] == 1                     # 分析结果没丢
    assert recording.messages                         # 后面的渠道照常收到
    assert "exploding" not in summary["delivered"]    # 抛了的那个不算投递成功
    assert "recording" in summary["delivered"]


def test_brief_path_reports_the_file_that_was_actually_written(tmp_path) -> None:
    """`delivered` 写着 `["file"]`、`brief_path` 却是 null——文件明明写出来了，
    回执说没有。四条链路此前有两种答法，`postclose` / `preopen` 是错的那种：
    调用方自带 notifier 时（daemon 就是这么调的），它们只看自己构造的那个。
    """
    from ashare_decision_support.notify import ConsoleNotifier, FileNotifier
    from ashare_decision_support.notify.base import written_path

    caller_owned = FileNotifier(tmp_path, slug="caller")
    caller_owned.send("标题", "正文")
    unused = FileNotifier(tmp_path / "never", slug="inner")

    assert written_path([ConsoleNotifier(), caller_owned], unused) == str(caller_owned.last_path)
    # 谁都没写文件时才是 None。
    assert written_path([ConsoleNotifier()], unused) is None
