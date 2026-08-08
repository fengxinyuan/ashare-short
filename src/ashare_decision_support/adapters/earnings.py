"""业绩预告与业绩快报：中报/年报窗口里唯一的「公司自己说的基本面事实」。

**为什么单列一层。** 系统此前完全没有业绩维度。舆情层回答「有没有人在说」，
量价层回答「走成什么样」，而「这家公司上半年是赚是亏」——最硬、最先于股价、
且由公司自己签字披露的那个事实——一条都进不来。实测 2026 中报窗口 1874 家披露
预告，归母口径里预增 580 家、首亏 284 家。超短线最怕的「业绩杀」和最想要的
「中报超预期」全在这批数据里，而这一层建起来之前它们对系统是不存在的。

**一个标的有多行，每个「预测指标」一行，而预告类型随指标而变。**
这是这个源最容易出错的地方，实测同一家公司：

```text
归属于上市公司股东的净利润   续亏   +6.98%
扣除非经常性损益后的净利润   续亏  +15.74%
扣除后营业收入               略增  +22.55%
营业收入                     略增  +25.62%
```

取错指标会把一家**持续亏损**的公司读成「略增」。所以这里只认归母净利润，取不到
才按 `METRIC_PRIORITY` 往下退，并且**把退到哪一档写进读数**（`metric` 字段）——
一个基于营业收入的「略增」和一个基于归母净利润的「略增」不是同一个结论，
下游必须能分辨。实测 1874 家里 1867 家有归母口径，退档是罕见路径而不是常态。

**快报优先于预告。** `stock_yjkb_em` 是已经算出来的实际数，`stock_yjyg_em` 是
区间估计。同一标的两边都有时以快报为准，并在 `source` 上标明——这和别处
「不用低分辨率数据顶替」是同一条原则的正面用法：高分辨率的那个到了就该换。

**口径提醒：`change_pct` 是百分数不是比值**（+296.645 表示 +296.645%）。
实测极值到 +265430 与 -31094，所以任何下游都不该拿它直接乘进分数——
本层只报数，`screens/earnings.py` 只用**类型**做否决、用幅度做措辞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.symbols import normalize_symbol
from ashare_decision_support.tabular import records, text

# 预测指标的优先级。只有第一档是真正想要的，其余是取不到时的退路。
# 营业收入排在最后：一家公司可以营收大涨而净利润巨亏，用它判业绩方向会反号。
METRIC_PRIORITY: tuple[str, ...] = (
    "归属于上市公司股东的净利润",
    "扣除非经常性损益后的净利润",
    "每股收益",
    "扣除后营业收入",
    "营业收入",
)

# 预告类型 → 方向。交易所口径的固定词表，不是自由文本，所以按词表映射是可靠的。
POSITIVE_TYPES: frozenset[str] = frozenset({"预增", "略增", "扭亏", "减亏", "续盈"})
NEGATIVE_TYPES: frozenset[str] = frozenset({"预减", "略减", "首亏", "增亏", "续亏"})

# 归母净利润仍为负的四种。**「方向」和「水平」是两个问题，这里回答第二个。**
#
# 「减亏」在上面的词表里方向为正（同比在改善），但它的净利润仍然是负的。只看
# 方向会把一家还在亏钱的公司标成「业绩催化」——而这一层存在的理由之一正是别让
# 亏损公司混进候选。两个字段各答一问，谁也不覆盖谁，和 `DATA_SOURCES.md` §五
# 里「量纲差异和读数分歧分开报」是同一条处理原则。
LOSS_TYPES: frozenset[str] = frozenset({"首亏", "增亏", "续亏", "减亏"})
DETERIORATING_LOSS_TYPES: frozenset[str] = frozenset({"首亏", "增亏"})
IMPROVING_LOSS_TYPES: frozenset[str] = frozenset({"减亏"})

DIRECTION_POSITIVE = "正面"
DIRECTION_NEGATIVE = "负面"
DIRECTION_UNCERTAIN = "不确定"

# 「还算事件」的窗口。超过这个天数的预告，市场已经消化过一轮，它仍然是事实
# （所以否决照旧生效），但不再是**催化**。两者必须分开，否则四月披露的预增
# 会和昨晚披露的预增在简报里长得一模一样。
FRESH_DAYS = 10

# 措辞用的幅度分档，不参与任何计算。
STRONG_GROWTH_PCT = 100.0
SEVERE_DECLINE_PCT = -50.0

REPORT_QUARTER_ENDS = ((12, 31), (9, 30), (6, 30), (3, 31))


def report_period(day: date) -> str:
    """当天最近一个已过去的季末，`YYYYMMDD`。

    披露窗口总是滞后于报告期：8 月 3 日要问的是 2026-06-30 那期中报的预告。
    取「最近一个已过去的季末」而不是硬编码，是为了让这一层在四个季度里
    行为一致，不需要每季改一次常量。
    """
    for month, dom in REPORT_QUARTER_ENDS:
        if (day.month, day.day) >= (month, dom):
            return f"{day.year}{month:02d}{dom:02d}"
    return f"{day.year - 1}1231"


def previous_period(period: str) -> str:
    """上一个季末。用于窗口刚开、当期还没人披露时往回退一档。"""
    year, month = int(period[:4]), int(period[4:6])
    index = next(
        (i for i, (m, _) in enumerate(REPORT_QUARTER_ENDS) if m == month),
        None,
    )
    if index is None:
        return f"{year - 1}1231"
    if index == len(REPORT_QUARTER_ENDS) - 1:
        prev_month, prev_dom = REPORT_QUARTER_ENDS[0]
        return f"{year - 1}{prev_month:02d}{prev_dom:02d}"
    prev_month, prev_dom = REPORT_QUARTER_ENDS[index + 1]
    return f"{year}{prev_month:02d}{prev_dom:02d}"


@dataclass(frozen=True)
class EarningsForecast:
    """一个标的在一个报告期上的业绩读数。"""

    symbol: str
    name: str
    kind: str
    direction: str
    period: str
    # 用的是哪个预测指标。**必须带出去**：营业收入口径的「略增」和归母净利润
    # 口径的「略增」不是同一个结论。
    metric: str
    source: str = "预告"
    change_pct: float | None = None
    published_at: str = ""
    days_since: int | None = None
    reason: str = ""

    @property
    def is_loss(self) -> bool:
        """归母净利润仍为负。**与 `direction` 正交**——「减亏」两者同时成立。"""
        return self.kind in LOSS_TYPES

    @property
    def is_deteriorating_loss(self) -> bool:
        return self.kind in DETERIORATING_LOSS_TYPES

    @property
    def is_improving_loss(self) -> bool:
        """还在亏，但比去年亏得少。不是催化，也不是恶化。"""
        return self.kind in IMPROVING_LOSS_TYPES

    @property
    def is_profitable_growth(self) -> bool:
        """真正的「业绩超预期」候选：方向为正**且**已经在赚钱。"""
        return self.direction == DIRECTION_POSITIVE and not self.is_loss

    @property
    def is_net_profit_basis(self) -> bool:
        """读数是否建立在净利润口径上。营收口径的方向判断不可用于否决。"""
        return self.metric in METRIC_PRIORITY[:2]

    @property
    def is_fresh(self) -> bool:
        return self.days_since is not None and self.days_since <= FRESH_DAYS

    @property
    def headline(self) -> str:
        """一句人话，进简报与台账理由。"""
        magnitude = f" {self.change_pct:+.1f}%" if self.change_pct is not None else ""
        basis = "" if self.is_net_profit_basis else f"（{self.metric}口径）"
        stamp = f"，{self.published_at} 披露" if self.published_at else ""
        return f"{self.source}{self.kind}{magnitude}{basis}{stamp}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "direction": self.direction,
            "period": self.period,
            "metric": self.metric,
            "source": self.source,
            "change_pct": self.change_pct,
            "published_at": self.published_at,
            "days_since": self.days_since,
            "is_fresh": self.is_fresh,
            "headline": self.headline,
        }


@dataclass
class EarningsSnapshot:
    """一个报告期的全市场业绩读数。"""

    period: str = ""
    forecasts: dict[str, EarningsForecast] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ok: bool = False

    def __len__(self) -> int:
        return len(self.forecasts)

    def get(self, symbol: str) -> EarningsForecast | None:
        return self.forecasts.get(normalize_symbol(symbol))

    def by_direction(self, direction: str) -> list[EarningsForecast]:
        return [item for item in self.forecasts.values() if item.direction == direction]

    def describe(self, n: int = 5) -> list[str]:
        """给简报用的两三行摘要。"""
        if not self.forecasts:
            return []
        tally: dict[str, int] = {}
        for item in self.forecasts.values():
            tally[item.kind] = tally.get(item.kind, 0) + 1
        ordered = sorted(tally.items(), key=lambda pair: pair[1], reverse=True)
        lines = [
            f"业绩预告（{self.period} 报告期）{len(self.forecasts)} 家："
            + "、".join(f"{kind} {count}" for kind, count in ordered[:6])
        ]

        # 新鲜的大幅预增单独点名——这是这一层唯一能提供**催化**的部分。
        # 用 `is_profitable_growth` 而不是 `direction == 正面`：后者会把一家
        # 「减亏 +300%」的亏损公司排到榜首，而它仍然在亏钱。
        strong = [
            item
            for item in self.forecasts.values()
            if item.is_profitable_growth
            and item.is_fresh
            and item.change_pct is not None
            and item.change_pct >= STRONG_GROWTH_PCT
        ]
        if strong:
            strong.sort(key=lambda item: item.change_pct or 0.0, reverse=True)
            lines.append(
                f"近 {FRESH_DAYS} 天新披露且增幅翻倍以上 {len(strong)} 家："
                + "、".join(
                    f"{item.name or item.symbol}（{item.kind} {item.change_pct:+.0f}%）"
                    for item in strong[:n]
                )
            )
        return lines


class EarningsAdapter:
    """AkShare 业绩预告 + 业绩快报，按标的收敛成一个读数。

    走 `datacenter-web.eastmoney.com`（与龙虎榜同一台主机，本机实测可达），
    不是不可达的 `push2` 系。取数失败一律返回 `ok=False` 的空快照 + note，
    绝不抛给调用方——业绩缺失必须是**放行**，让它拦人等于让数据缺失冒充风险判断。
    """

    name = "earnings"

    def __init__(self, ak_module: Any | None = None, cache: Any | None = None) -> None:
        if ak_module is None:
            try:
                import akshare as ak_module  # type: ignore
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "akshare is not installed. Install it with `pip install -e .[akshare]`."
                ) from exc
        self.ak = ak_module
        self.cache = cache

    def fetch(self, trade_date: date, period: str | None = None) -> EarningsSnapshot:
        """取当期业绩读数；当期为空就往回退一个报告期。

        回退是必需的：季末刚过的头几周一家都还没披露，硬用当期会得到一个空快照，
        而那和「这批公司都没有业绩问题」在只报数量的设计里长得一模一样。
        """
        wanted = period or report_period(trade_date)
        notes: list[str] = []

        for attempt, candidate in enumerate((wanted, previous_period(wanted))):
            rows, note = self._rows("yjyg", candidate, "业绩预告", lambda p=candidate: self.ak.stock_yjyg_em(date=p))
            if note:
                notes.append(note)
            forecasts = _collapse(rows, candidate, trade_date)
            if not forecasts:
                continue

            fast, fast_note = self._rows(
                "yjkb", candidate, "业绩快报", lambda p=candidate: self.ak.stock_yjkb_em(date=p)
            )
            if fast_note:
                notes.append(fast_note)
            replaced = _merge_fast_reports(forecasts, fast, candidate, trade_date)

            if attempt:
                notes.append(
                    f"{wanted} 报告期尚无业绩预告，改用上一期 {candidate} 的 {len(forecasts)} 家"
                    "（季末刚过时属于预期行为）"
                )
            if replaced:
                notes.append(f"其中 {replaced} 家已发业绩快报，以快报实际数覆盖预告估计区间")
            return EarningsSnapshot(period=candidate, forecasts=forecasts, notes=notes, ok=True)

        notes.append(
            f"{wanted} 与上一报告期都没有取到业绩预告，**本次没有业绩风险否决、"
            "也没有业绩催化标注**，候选未经该层过滤"
        )
        return EarningsSnapshot(period=wanted, notes=notes, ok=False)

    # ---- internals -------------------------------------------------------

    def _rows(self, key: str, period: str, label: str, loader) -> tuple[list[dict], str | None]:
        if self.cache is not None:
            cached = self.cache.read(period, f"{key}_{period}")
            if isinstance(cached, list):
                return cached, None
        try:
            table = loader()
        except Exception as exc:
            return [], f"{label}数据源不可用（{type(exc).__name__}），本次跳过"
        rows = records(table)
        if self.cache is not None and rows:
            self.cache.write(period, f"{key}_{period}", rows)
        return rows, None


# ---- parsing -------------------------------------------------------------


def _collapse(rows: list[dict], period: str, trade_date: date) -> dict[str, EarningsForecast]:
    """多行按标的收敛成一行，按 `METRIC_PRIORITY` 选指标。

    先按优先级分组再挑，而不是边遍历边比较：后者依赖行的到达顺序，而这个源
    并不保证同一标的的几行是连续的。
    """
    best: dict[str, tuple[int, dict]] = {}
    for row in rows:
        code = text(row, "股票代码")
        if not code:
            continue
        metric = text(row, "预测指标")
        try:
            rank = METRIC_PRIORITY.index(metric)
        except ValueError:
            # 没见过的指标排在所有已知指标之后，但仍然比「一条都没有」好。
            rank = len(METRIC_PRIORITY)
        symbol = normalize_symbol(code)
        current = best.get(symbol)
        if current is None or rank < current[0]:
            best[symbol] = (rank, row)

    result: dict[str, EarningsForecast] = {}
    for symbol, (rank, row) in best.items():
        kind = text(row, "预告类型")
        if not kind:
            continue
        published = text(row, "公告日期")[:10]
        result[symbol] = EarningsForecast(
            symbol=symbol,
            name=text(row, "股票简称"),
            kind=kind,
            direction=_direction(kind),
            period=period,
            metric=(
                METRIC_PRIORITY[rank] if rank < len(METRIC_PRIORITY) else text(row, "预测指标")
            ),
            source="预告",
            change_pct=finite(row.get("业绩变动幅度")),
            published_at=published,
            days_since=_days_since(published, trade_date),
            reason=text(row, "业绩变动原因")[:200],
        )
    return result


def _merge_fast_reports(
    forecasts: dict[str, EarningsForecast],
    rows: list[dict],
    period: str,
    trade_date: date,
) -> int:
    """快报覆盖预告。返回被覆盖的家数。

    快报只给数不给「预增/首亏」这类标签，所以类型由**净利润同比增长**反推。
    反推而不是沿用预告的标签：一家公司可以预告「预增」而快报实际同比下滑，
    沿用标签会让最硬的那个数被最软的那个标签盖住。
    """
    replaced = 0
    for row in rows:
        code = text(row, "股票代码")
        if not code:
            continue
        symbol = normalize_symbol(code)
        growth = finite(row.get("净利润-同比增长"))
        profit = finite(row.get("净利润-净利润"))
        kind = _kind_from_actuals(growth, profit)
        if kind is None:
            continue
        published = text(row, "公告日期")[:10]
        if symbol in forecasts:
            replaced += 1
        forecasts[symbol] = EarningsForecast(
            symbol=symbol,
            name=text(row, "股票简称"),
            kind=kind,
            direction=_direction(kind),
            period=period,
            metric="归属于上市公司股东的净利润",
            source="快报",
            change_pct=growth,
            published_at=published,
            days_since=_days_since(published, trade_date),
            reason="",
        )
    return replaced


def _kind_from_actuals(growth: float | None, profit: float | None) -> str | None:
    """快报的实际数 → 与预告同一套词表的类型标签。

    亏损优先于增长率判断：净利润为负时同比「增长」说的是亏得比去年少，
    那是「减亏」不是「预增」——这两个在超短线上的含义完全不同。
    """
    if profit is not None and profit < 0:
        if growth is None:
            return "续亏"
        return "减亏" if growth > 0 else "增亏"
    if growth is None:
        return None
    if growth >= 50.0:
        return "预增"
    if growth > 0:
        return "略增"
    if growth > -50.0:
        return "略减"
    return "预减"


def _direction(kind: str) -> str:
    if kind in POSITIVE_TYPES:
        return DIRECTION_POSITIVE
    if kind in NEGATIVE_TYPES:
        return DIRECTION_NEGATIVE
    return DIRECTION_UNCERTAIN


def _days_since(published: str, trade_date: date) -> int | None:
    if not published:
        return None
    try:
        return max(0, (trade_date - date.fromisoformat(published)).days)
    except ValueError:
        return None


def default_earnings_adapter() -> EarningsAdapter | None:
    """默认业绩源。akshare 缺席时返回 None，由调用方降级。

    单独提出来是为了让测试能把它替掉。适配器在测试里自己长出来会打真实外网，
    而业绩数据**会改变候选名单**（否决掉真实亏损股），于是同一份 fixture 的结果
    开始取决于外网是否可达——这类泄漏不会报错，只会让测试时而通过时而失败。

    消费方各自 `from ... import default_earnings_adapter`，所以 `conftest.py`
    要**逐个消费模块**打补丁，和 `default_snapshot_adapter` 完全同一套做法。
    """
    try:
        return EarningsAdapter()
    except RuntimeError:
        return None
