"""Tencent trade-detail adapter for post-close flow summaries.

The endpoint is useful exactly where the project is currently blind: same-day
money flow. It does **not** expose exchange-side order intent — the `B/S/M`
flag is Tencent's inference from the quoted book at the time of the print — so
the output here is kept as a post-close *side reading*, never a scoring input.

First implementation scope is intentionally narrow:

- fetch per-symbol trade prints from Tencent's `detail` endpoint;
- summarise active-buy / active-sell imbalance, large-print net flow, and
  last-30-minutes net flow;
- stay deterministic and dependency-free, with all caveats carried in notes.
"""

from __future__ import annotations

import ast
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.numeric import as_int, finite, positive
from ashare_decision_support.symbols import normalize_symbol

DETAIL_URL = "https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={code}&p={page}"
REFERER = "https://gu.qq.com"
USER_AGENT = "Mozilla/5.0"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_PAGES = 80
# 逐笔取数的并发度。腾讯这个端点没有公开配额；4 是实测档位（22 只标的 0 失败），
# 再高没有验证过，别为了快去调它。
DEFAULT_MAX_WORKERS = 4
TAIL_START_MINUTE = 14 * 60 + 30
LARGE_PRINT_QUANTILE = 0.95


@dataclass(frozen=True)
class TradePrint:
    symbol: str
    sequence: int
    time_text: str
    price: float
    change: float | None
    volume_lot: int
    amount: float
    side: str

    @property
    def signed_amount(self) -> float:
        if self.side == "B":
            return self.amount
        if self.side == "S":
            return -self.amount
        return 0.0

    @property
    def minute_of_day(self) -> int | None:
        return _minute_of_day(self.time_text)


@dataclass(frozen=True)
class TradeFlowSummary:
    symbol: str
    ok: bool
    prints_count: int
    buy_amount: float
    sell_amount: float
    neutral_amount: float
    net_amount: float
    buy_ratio: float | None
    large_threshold_amount: float | None
    large_buy_amount: float
    large_sell_amount: float
    large_net_amount: float
    tail_net_amount: float | None
    tail_ratio: float | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ok": self.ok,
            "prints_count": self.prints_count,
            "buy_amount": self.buy_amount,
            "sell_amount": self.sell_amount,
            "neutral_amount": self.neutral_amount,
            "net_amount": self.net_amount,
            "buy_ratio": self.buy_ratio,
            "large_threshold_amount": self.large_threshold_amount,
            "large_buy_amount": self.large_buy_amount,
            "large_sell_amount": self.large_sell_amount,
            "large_net_amount": self.large_net_amount,
            "tail_net_amount": self.tail_net_amount,
            "tail_ratio": self.tail_ratio,
            "notes": list(self.notes),
        }


class TencentDetailAdapter:
    """Same-day trade-flow summaries from Tencent detail pages."""

    name = "tencent-detail"

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Any = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self.max_pages = max(1, int(max_pages))
        self.max_workers = max(1, int(max_workers))

    def get_trade_flow(
        self,
        symbols: list[str],
    ) -> tuple[dict[str, TradeFlowSummary], list[str]]:
        """一批标的的逐笔摘要。

        并发取数：一只票要翻约 67 页（`page 0` 起，见 `_fetch_prints`），串行时
        单只约 20 秒，盘前那 28 只候选就是 9 分钟。并发度默认 4——腾讯这个端点
        没有公开配额，4 是实测稳定（22 只 0 失败）又不至于把对方打疼的档位。

        **结果与说明都按入参顺序收集**，不按完成顺序：简报里那几行说明的顺序
        若随线程调度变化，两次同样的取数会产出不同的文本，diff 就没法看了。
        """
        results: dict[str, TradeFlowSummary] = {}
        notes = [
            "逐笔资金按腾讯 B/S/M 方向推断，非交易所委托方向；只作旁证，不进打分",
            "大单阈值按当日逐笔成交额 95 分位自适应，尚未具备多日逐笔历史",
        ]
        ordered = [normalized for raw in symbols if (normalized := normalize_symbol(raw))]
        if not ordered:
            return results, notes

        fetched: dict[str, tuple[TradeFlowSummary | None, Exception | None]] = {}
        if self.max_workers > 1 and len(ordered) > 1:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(ordered))) as pool:
                futures = {pool.submit(self.fetch_summary, symbol): symbol for symbol in ordered}
                for future, symbol in futures.items():
                    # 单只失败不该拖垮整批：错误存起来，按入参顺序统一转成说明。
                    try:
                        fetched[symbol] = (future.result(), None)
                    except Exception as exc:
                        fetched[symbol] = (None, exc)
        else:
            for symbol in ordered:
                try:
                    fetched[symbol] = (self.fetch_summary(symbol), None)
                except Exception as exc:
                    fetched[symbol] = (None, exc)

        for symbol in ordered:
            summary, error = fetched.get(symbol, (None, None))
            if error is not None:
                notes.append(f"{symbol} 逐笔资金不可用（{type(error).__name__}）")
                continue
            if summary is not None and summary.ok:
                results[symbol] = summary
            elif summary is not None and summary.notes:
                notes.append(f"{symbol}：{summary.notes[0]}")
        if results:
            notes.append(f"逐笔资金旁证已附到 {len(results)} 只标的上")
        return results, notes

    def fetch_summary(self, symbol: str) -> TradeFlowSummary | None:
        prints = self._fetch_prints(symbol)
        if not prints:
            return TradeFlowSummary(
                symbol=symbol,
                ok=False,
                prints_count=0,
                buy_amount=0.0,
                sell_amount=0.0,
                neutral_amount=0.0,
                net_amount=0.0,
                buy_ratio=None,
                large_threshold_amount=None,
                large_buy_amount=0.0,
                large_sell_amount=0.0,
                large_net_amount=0.0,
                tail_net_amount=None,
                tail_ratio=None,
                notes=("未取到逐笔成交明细",),
            )

        buy_amount = sum(item.amount for item in prints if item.side == "B")
        sell_amount = sum(item.amount for item in prints if item.side == "S")
        neutral_amount = sum(item.amount for item in prints if item.side == "M")
        active_total = buy_amount + sell_amount
        net_amount = buy_amount - sell_amount
        buy_ratio = buy_amount / active_total if active_total > 0 else None

        amounts = [item.amount for item in prints if item.amount > 0]
        threshold = _quantile(amounts, LARGE_PRINT_QUANTILE)
        large_buy = sum(item.amount for item in prints if item.side == "B" and threshold is not None and item.amount >= threshold)
        large_sell = sum(item.amount for item in prints if item.side == "S" and threshold is not None and item.amount >= threshold)
        large_net = large_buy - large_sell

        tail_prints = [
            item for item in prints
            if item.minute_of_day is not None and item.minute_of_day >= TAIL_START_MINUTE and item.side in {"B", "S"}
        ]
        tail_net = sum(item.signed_amount for item in tail_prints) if tail_prints else None
        tail_ratio = tail_net / net_amount if tail_net is not None and net_amount > 0 else None

        row_notes: list[str] = []
        if net_amount <= 0:
            row_notes.append("全天主动净额非正，尾盘承接比例不定义")

        return TradeFlowSummary(
            symbol=symbol,
            ok=True,
            prints_count=len(prints),
            buy_amount=round(buy_amount, 2),
            sell_amount=round(sell_amount, 2),
            neutral_amount=round(neutral_amount, 2),
            net_amount=round(net_amount, 2),
            buy_ratio=round(buy_ratio, 4) if buy_ratio is not None else None,
            large_threshold_amount=round(threshold, 2) if threshold is not None else None,
            large_buy_amount=round(large_buy, 2),
            large_sell_amount=round(large_sell, 2),
            large_net_amount=round(large_net, 2),
            tail_net_amount=round(tail_net, 2) if tail_net is not None else None,
            tail_ratio=round(tail_ratio, 4) if tail_ratio is not None else None,
            notes=tuple(row_notes),
        )

    def _fetch_prints(self, symbol: str) -> list[TradePrint]:
        code = _tencent_code(symbol)
        prints: list[TradePrint] = []
        previous_fingerprint: tuple[tuple[int, str], ...] | None = None
        # 从第 0 页起。第 0 页装的是 09:25 集合竞价那一笔和开盘后约 8 分钟的成交，
        # 从第 1 页起会把它整段丢掉——实测缺口精确等于第 0 页（利欧股份 16.71 亿
        # ＝全天 23.9%、中际旭创 30.86 亿＝10.6%、华能国际 0.36 亿＝4.1%）。
        # 缺口对跳空与涨停的标的最大，而那恰好是超短最在意的那批，且竞价那一笔
        # 全部带 B/S 方向，漏掉它会直接扭曲主动买卖占比。
        #
        # 竞价那一笔的方向本身不可信——集合竞价是集中撮合，没有主动方，腾讯给的
        # B/S 是它按盘口的推断。**实测过影响，很小**：8 只标的上，含竞价与剔竞价
        # 的主动买占比只差 -0.7 ~ +1.3pp（竞价占全天主动成交的 0.4%~2.9%）。
        # 所以照单收下、不单独剔除；真要纠结的是整个 B/S 都是推断值这件事，
        # 那条已经写在 `get_trade_flow` 的说明里，不是竞价独有的问题。
        for page in range(self.max_pages + 1):
            body = self._get(DETAIL_URL.format(code=code, page=page))
            batch = _parse_page(body, symbol)
            if not batch:
                break
            fingerprint = tuple((item.sequence, item.time_text) for item in batch[:5])
            if previous_fingerprint is not None and fingerprint == previous_fingerprint:
                break
            previous_fingerprint = fingerprint
            prints.extend(batch)
        return prints

    def _get(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={"Referer": REFERER, "User-Agent": USER_AGENT},
        )
        with self._opener(request, timeout=self.timeout) as response:
            return response.read().decode("gbk", errors="replace")


def _tencent_code(symbol: str) -> str:
    code, _, suffix = normalize_symbol(symbol).partition(".")
    return f"{suffix.lower()}{code}" if suffix else code


def _parse_page(body: str, symbol: str) -> list[TradePrint]:
    if "[" not in body or "]" not in body:
        return []
    start = body.find("[")
    end = body.rfind("]")
    if end <= start:
        return []
    try:
        payload = ast.literal_eval(body[start : end + 1])
    except (SyntaxError, ValueError):
        return []
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    rows = payload[1]
    if not isinstance(rows, str) or not rows.strip():
        return []

    prints: list[TradePrint] = []
    for raw in rows.split("|"):
        fields = raw.split("/")
        if len(fields) < 7:
            continue
        price = positive(fields[2])
        amount = positive(fields[5])
        if price is None or amount is None:
            continue
        side = str(fields[6] or "M").strip().upper()
        if side not in {"B", "S", "M"}:
            side = "M"
        prints.append(
            TradePrint(
                symbol=symbol,
                sequence=as_int(fields[0]),
                time_text=str(fields[1]).strip(),
                price=price,
                change=finite(fields[3]),
                volume_lot=as_int(fields[4]),
                amount=amount,
                side=side,
            )
        )
    return prints


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    rank = max(0, min(len(values) - 1, math.ceil(len(values) * q) - 1))
    return sorted(values)[rank]


def _minute_of_day(text: str) -> int | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) < 2:
            return None
        hour = as_int(parts[0], default=-1)
        minute = as_int(parts[1], default=-1)
        return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    if len(raw) >= 4 and raw[:4].isdigit():
        hour = as_int(raw[:2], default=-1)
        minute = as_int(raw[2:4], default=-1)
        return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    return None


def default_trade_flow_adapter() -> TencentDetailAdapter:
    """默认逐笔源。

    存在的理由不是省一次构造，而是**让它可以被替换掉**。消费方直接
    `TencentDetailAdapter()` 的话，测试里就没有一个可打补丁的名字，于是每个跑
    `run_morning` 的测试都会静默去打一次 `stock.gtimg.cn`——那正是 `tests/conftest.py`
    里 `no_implicit_snapshot_adapter` 与 `no_implicit_earnings_adapter` 两条治过的
    同一类泄漏：不会失败，只会悄悄变成另一件事（这里是每只候选多花约 20 秒）。
    """
    return TencentDetailAdapter()
