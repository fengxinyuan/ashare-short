"""宽基指数快照：市值加权口径的大盘，用来对照等权口径的宽度。

## 为什么需要这一层

`sectors/predict.py` 的大盘方向读数完全由**等权**量构成——板块涨幅中位数、
上涨家数占比、站上 20 日均线占比。等权口径回答的是「今天多少股票在涨」，
它结构上看不见**市值加权的指数在跌**，而这两件事经常同一天发生，且差得很远。

实测 2026-08-03：31 个板块 4236 只成分股加权上涨占比 76%、板块当日涨幅中位数
+1.66%，系统给出「偏多，综合读数 88.85／100」。同一天的宽基指数——

| 指数 | 涨跌 | 指数 | 涨跌 |
|---|---|---|---|
| 科创50 | **-5.08%** | 上证指数 | -0.59% |
| 上证50 | -1.23% | 深证成指 | -0.96% |
| 沪深300 | -0.98% | 创业板指 | -1.24% |
| 中证500 | -1.06% | 中证1000 | +0.06% |

七成股票在涨，同时每一个宽基指数都在跌，科创50 跌掉 5%。两个读数都没算错，
它们量的是不同的东西：小票普涨、权重股杀跌。但只报前者的话，「偏多 88.85」
会在一个权重崩塌的日子里给出接近满分的方向读数——那是**方向性盲区**，不是噪声。

## 这一层只出读数，不改分

**刻意不进打分、不改仓位系数。** 除了「新读数先进简报、等 `review` 验证过再谈
给分」这条通例，这里还有一条更硬的理由：台账里已经积了 82 条待兑现记录，它们
的 `score` 与 `position_factor` 都是按现行公式算的。现在改公式，等于让新旧记录
不可比——而那 82 条正是这套系统用来验证自己的全部样本。

所以这一层做的事只有一件：把市值加权口径的事实摆出来，并在它与等权口径背离时
出一条告警。判断留给读的人。

## 口径

- 取的是**指数**不是成分股聚合，所以它天然是市值加权的，不需要自己加权。
- 数据源 `hq.sinajs.cn`，与 `adapters/snapshot.py` 同一路（本机实测通，见
  `docs/DATA_SOURCES.md`）。指数与个股的字段布局一致，只是买卖五档恒为 0。
- 涨跌幅由 `close / prev_close - 1` 现算，不取服务端字段——指数行没有涨跌幅列。
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from statistics import median
from typing import Any

SINA_URL = "https://hq.sinajs.cn/list="
SINA_REFERER = "https://finance.sina.com.cn"
USER_AGENT = "Mozilla/5.0"
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
        rets = self._rets()
        return median(rets) if rets else None

    @property
    def advancing(self) -> int:
        return sum(1 for ret in self._rets() if ret > 0)

    @property
    def counted(self) -> int:
        return len(self._rets())

    def worst(self) -> IndexQuote | None:
        scored = [quote for quote in self.quotes if quote.ret is not None]
        return min(scored, key=lambda quote: quote.ret or 0.0) if scored else None

    def best(self) -> IndexQuote | None:
        scored = [quote for quote in self.quotes if quote.ret is not None]
        return max(scored, key=lambda quote: quote.ret or 0.0) if scored else None

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
            body = self._get(SINA_URL + ",".join(code for code, _name in self.indices))
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
        request = urllib.request.Request(
            url, headers={"Referer": SINA_REFERER, "User-Agent": USER_AGENT}
        )
        with self._opener(request, timeout=self.timeout) as response:
            return response.read().decode("gbk", errors="replace")


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
        close = _positive(fields[3])
        if close is None:
            # 停牌或未开盘的指数返回 0。把 0.00 当点位发出去比报缺失更糟。
            continue
        quotes.append(
            IndexQuote(
                code=code,
                name=names.get(code) or fields[0] or code,
                close=close,
                prev_close=_positive(fields[2]),
                amount=_positive(fields[9]),
                trade_date=fields[30].strip() or None,
            )
        )
    return quotes


def _positive(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def divergence_verdict(
    read: BroadIndexRead | None,
    breadth: float | None,
    expected_date: str | None = None,
) -> str:
    """等权与市值加权的关系，判定成一个标签。

    **这是判据的单一真源**：`read_divergence` 的措辞由它决定，台账里存的也是它。
    两边各写一遍判据的话，日后调 `BREADTH_STRONG` 只会改到其中一处，于是简报说
    「背离」而台账记「一致」，事后统计再也对不上当天实际报出去的结论。

    存标签而不是只存 `breadth` 与 `median_ret` 两个原始数，理由与
    `journal/stats.py:_regime_bucket` 那条完全相同：阈值以后一定会调，而调完之后
    **历史记录必须仍然按当天生效的那个判定分组**，否则分层统计描述的是一个从未
    存在过的过去。
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
    """宽基读数 + 等权宽度 → `(reasons, warnings)`。

    **只出话，不出分。** 见模块开头：台账里的待兑现记录都是按现行公式打的，
    改公式会让新旧记录不可比。

    背离的判据是两条同时成立：等权宽度高（多数股票在涨）且宽基中位数为负
    （市值加权在跌）。任何一条不成立都不叫背离——宽度低时指数跟着跌是一致，
    宽度高时指数也涨更是一致。判定本身在 `divergence_verdict`，这里只负责措辞。

    `expected_date` 是等权那一路的数据日期（`YYYY-MM-DD`）。**两边不同日就不比，
    不知道是哪天也不比。**

    这个闸门是必需的而不是保险：`hq.sinajs.cn` 给的是**实时**行情。盘前跑时它返回
    的是上一个交易日的收盘，与等权那一路对得上；一旦盘中跑，它返回的是此刻的
    实时点位，而板块动量仍是昨收——拿今天的指数和昨天的宽度算背离，正是这套
    系统在别处一直防的跨时点比较。

    `expected_date=None` 走「不知道」而不是「不用查」：把未知当成可比，等于在
    调用方忘了传日期时**静默地**恢复成有 bug 的版本。事实照报，判断不给。
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
        warnings.append(
            "等权那一路的数据日期未知，无法确认与宽基是同一天，本次不做背离判断"
        )
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
