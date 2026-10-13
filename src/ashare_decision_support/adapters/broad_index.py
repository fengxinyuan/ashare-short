"""市值加权宽基指数快照，与成分股等权宽度对照。

新浪指数行沿用个股字段布局，涨跌幅由现价和前收计算。
日期可比时输出背离标签与告警；结果用于展示和台账，不修改评分或仓位。
数据源与字段口径见 docs/DATA_SOURCES.md。
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.adapters.http import SINA_QUOTE_URL, SINA_REFERER, fetch_text
from ashare_decision_support.numeric import median_of, positive

DEFAULT_TIMEOUT = 10.0

# 覆盖大中小盘与两个科技板。**必须同时含大盘与小盘**：这一层存在的意义就是
# 呈现两者的分化，只取上证指数的话，2026-08-03 会读成「跌了 0.59%，还好」，
# 而当天真正的事实是科创50 -5.08% 与中证1000 +0.06% 同时发生。
BROAD_INDICES: tuple[tuple[str, str], ...] = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000016", "上证50"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000852", "中证1000"),
)

# 判定「权重在杀跌」的门槛：宽基指数涨跌幅的中位数低于这个值。
# 用中位数而不是均值——科创50 那种 -5% 的极值会把均值单独拉穿。
WEIGHTED_WEAK = -0.005

# 等权宽度高于这个值才谈得上「背离」。宽度本身就低的时候，指数跟着跌不是背离，
# 是一致。
BREADTH_STRONG = 0.55

# 单个指数跌到这个幅度就单独点名，无论整体是否背离。板块级的崩塌可以被其余
# 七个指数的中位数平掉，而它恰恰是当天最该看见的那一行。
SINGLE_INDEX_ALARM = -0.03

# 判定标签。四个值互斥且穷尽，台账按它分组，所以措辞一旦定下就不能改——
# 改了会让新旧记录落进不同的桶，而它们描述的是同一件事。
VERDICT_DIVERGED = "等权强加权弱"
VERDICT_ALIGNED = "等权与加权一致"
# 「不可比」与「未记录」必须分开：前者是两边日期对不上（盘中取数的常态），
# 后者是压根没这个读数（老记录，或宽基取数失败）。合成一个桶的话，
# 「这层从来没生效过」和「这层每天都在被跳过」会长得一模一样。
VERDICT_INCOMPARABLE = "跨日不可比"
VERDICT_UNKNOWN = "未记录"


@dataclass(frozen=True)
class IndexQuote:
    code: str
    name: str
    close: float
    prev_close: float | None = None
    amount: float | None = None
    trade_date: str | None = None

    @property
    def ret(self) -> float | None:
        if not self.prev_close or self.prev_close <= 0:
            return None
        return self.close / self.prev_close - 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "close": self.close,
            "prev_close": self.prev_close,
            "ret": self.ret,
            "amount": self.amount,
            "trade_date": self.trade_date,
        }


@dataclass
class BroadIndexRead:
    """一次宽基快照。`ok=False` 时任何结论都不该被引用。"""

    quotes: list[IndexQuote] = field(default_factory=list)
    trade_date: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self._rets())

    def _rets(self) -> list[float]:
        return [quote.ret for quote in self.quotes if quote.ret is not None]

    @property
    def median_ret(self) -> float | None:
        return median_of(self._rets())

    @property
    def advancing(self) -> int:
        return sum(1 for ret in self._rets() if ret > 0)

    @property
    def counted(self) -> int:
        return len(self._rets())

    def worst(self) -> IndexQuote | None:
        scored = [quote for quote in self.quotes if quote.ret is not None]
        return min(scored, key=lambda quote: quote.ret or 0.0) if scored else None

    def summary(self) -> str:
        """一行摘要，按涨跌幅排序。"""
        ordered = sorted(
            (quote for quote in self.quotes if quote.ret is not None),
            key=lambda quote: quote.ret or 0.0,
            reverse=True,
        )
        return "、".join(f"{quote.name} {quote.ret:+.2%}" for quote in ordered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "median_ret": self.median_ret,
            "advancing": self.advancing,
            "counted": self.counted,
            "quotes": [quote.to_dict() for quote in self.quotes],
            "notes": list(self.notes),
        }


class BroadIndexAdapter:
    """宽基指数快照。一次请求取全部，失败只降级不抛。"""

    name = "sina-broad-index"

    def __init__(
        self,
        indices: tuple[tuple[str, str], ...] = BROAD_INDICES,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Any = None,
    ) -> None:
        self.indices = indices
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def fetch(self) -> BroadIndexRead:
        read = BroadIndexRead()
        try:
            body = self._get(SINA_QUOTE_URL + ",".join(code for code, _name in self.indices))
        except Exception as exc:
            read.notes.append(f"宽基指数不可用（{type(exc).__name__}），本次无市值加权口径读数")
            return read

        parsed = _parse(body, dict(self.indices))
        read.quotes = parsed
        missing = [name for code, name in self.indices if code not in {q.code for q in parsed}]
        if missing:
            read.notes.append(f"宽基指数缺 {len(missing)} 个：{'、'.join(missing)}")
        stamps = {quote.trade_date for quote in parsed if quote.trade_date}
        if len(stamps) > 1:
            # 不同指数停在不同交易日，说明至少有一路是旧的。不对齐就比较，
            # 等于拿昨天的指数和今天的指数算中位数。
            read.notes.append(
                f"⚠️ 宽基指数交易日不一致（{'、'.join(sorted(stamps))}），"
                "读数可能跨日，本次不做背离判断"
            )
            read.quotes = []
            return read
        read.trade_date = next(iter(stamps), None)
        return read

    def health_check(self) -> dict[str, Any]:
        read = self.fetch()
        return {
            "name": self.name,
            "ok": read.ok,
            "counted": read.counted,
            "trade_date": read.trade_date,
            "notes": list(read.notes),
        }

    def _get(self, url: str) -> str:
        return fetch_text(
            self._opener,
            url,
            timeout=self.timeout,
            referer=SINA_REFERER,
            encoding="gbk",
            errors="replace",
        )


def _parse(body: str, names: dict[str, str]) -> list[IndexQuote]:
    """`var hq_str_sh000001="上证指数,open,prevclose,last,high,low,...,date,time,";`

    指数行与个股行字段布局一致（`adapters/snapshot.py:_parse_sina`），只是买卖
    五档恒为 0，所以这里只取到成交额与日期。
    """
    quotes: list[IndexQuote] = []
    for line in body.splitlines():
        if "hq_str_" not in line or '"' not in line:
            continue
        code = line.split("hq_str_", 1)[1].split("=", 1)[0].strip()
        fields = line.split('"', 2)[1].split(",")
        if len(fields) < 32:
            continue
        close = positive(fields[3])
        if close is None:
            # 停牌或未开盘的指数返回 0。把 0.00 当点位发出去比报缺失更糟。
            continue
        quotes.append(
            IndexQuote(
                code=code,
                name=names.get(code) or fields[0] or code,
                close=close,
                prev_close=positive(fields[2]),
                amount=positive(fields[9]),
                trade_date=fields[30].strip() or None,
            )
        )
    return quotes


def divergence_verdict(
    read: BroadIndexRead | None,
    breadth: float | None,
    expected_date: str | None = None,
) -> str:
    """生成等权宽度与加权指数的关系标签，供简报和台账共用。

    台账保存当时生效的标签，避免调整阈值后重新解释历史判断。
    """
    if read is None or not read.ok:
        return VERDICT_UNKNOWN
    if not expected_date or (read.trade_date and read.trade_date != expected_date):
        return VERDICT_INCOMPARABLE
    median_ret = read.median_ret
    if breadth is None or median_ret is None:
        return VERDICT_UNKNOWN
    if breadth >= BREADTH_STRONG and median_ret <= WEIGHTED_WEAK:
        return VERDICT_DIVERGED
    return VERDICT_ALIGNED


def read_divergence(
    read: BroadIndexRead | None,
    breadth: float | None,
    expected_date: str | None = None,
) -> tuple[list[str], list[str]]:
    """返回宽基摘要与告警 `(reasons, warnings)`，判据由 divergence_verdict 提供。

    expected_date 是等权读数的日期；未知或与指数日期不同时只报事实。
    宽基快照可能已更新至盘中，而等权日线仍停在昨收，不能跨日比较。
    """
    if read is None or not read.ok:
        return [], []

    reasons: list[str] = []
    warnings: list[str] = []
    median_ret = read.median_ret
    reasons.append(
        f"宽基指数（市值加权，{read.trade_date or '—'}）{read.advancing}/{read.counted} 上涨，"
        f"中位 {median_ret:+.2%}：{read.summary()}"
    )

    if not expected_date:
        warnings.append("等权那一路的数据日期未知，无法确认与宽基是同一天，本次不做背离判断")
        return reasons, warnings

    if read.trade_date and read.trade_date != expected_date:
        warnings.append(
            f"宽基指数停在 {read.trade_date}，等权读数是 {expected_date}——"
            "两边不同日，本次不做背离判断（盘中取数会出现这种情况）"
        )
        return reasons, warnings

    if divergence_verdict(read, breadth, expected_date) == VERDICT_DIVERGED:
        warnings.append(
            f"⚠️ 等权与市值加权背离：{breadth:.0%} 的成分股在涨，但 {read.counted} 个宽基"
            f"指数中位数 {median_ret:+.2%}——涨的是小票，杀的是权重。"
            "大盘方向读数由等权量构成，结构上看不见这一层，别把它当成指数会涨。"
        )

    worst = read.worst()
    if worst is not None and (worst.ret or 0.0) <= SINGLE_INDEX_ALARM:
        warnings.append(
            f"⚠️ {worst.name} 单日 {worst.ret:+.2%}——单个宽基的崩塌会被其余指数的"
            "中位数平掉，但它通常就是当天资金在逃的那条主线。"
        )
    return reasons, warnings


def default_broad_index_adapter() -> BroadIndexAdapter:
    return BroadIndexAdapter()
