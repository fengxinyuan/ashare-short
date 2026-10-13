"""东财涨停、炸板和跌停池，提供情绪与封板质量读数。

炸板率为炸板数 / (涨停数 + 炸板数)。盘中当日数据仍在变化，
盘前流程使用最近已收盘交易日。保留请求日期与实际数据日期以说明回退。
此处的真实涨停名单与板块动量中涨幅 >= 9.5% 的 strong_count 近似分别使用。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ashare_decision_support.adapters.http import BROWSER_USER_AGENT, fetch_text
from ashare_decision_support.emotion_scale import (
    BLOWUP_HEALTHY,
    BLOWUP_STRESSED,
    STREAK_MOMENTUM,
    STREAK_WEAK,
)
from ashare_decision_support.fanout import gather
from ashare_decision_support.numeric import as_int, finite, median_of
from ashare_decision_support.schedule.calendar import session_forming
from ashare_decision_support.schedule.timezone import beijing_naive_now

BASE_URL = (
    "https://push2ex.eastmoney.com/getTopic{pool}Pool"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    "&Pageindex=0&pagesize={size}&sort=fbt%3Aasc&date={date}"
)
REFERER = "https://quote.eastmoney.com/"

POOL_LIMIT_UP = "ZT"
POOL_BLOWUP = "ZB"
POOL_LIMIT_DOWN = "DT"

PAGE_SIZE = 300
TIMEOUT_SECONDS = 12

# 盘前回溯上限。跨一个长假也够。
LOOKBACK_DAYS = 5

# 描述与周期判断共用 emotion_scale 的阈值。
EARLY_SEAL_BEFORE = "10:00"
EARLY_SEAL_STRONG = 0.5

# 涨停行业分布取前几名。
SECTOR_TOP = 5

# 价格字段以千分之一元计（16920 = 16.92 元）。
PRICE_SCALE = 1000.0


@dataclass(frozen=True)
class LimitStock:
    symbol: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    amount: float | None = None
    turnover: float | None = None
    seal_fund: float | None = None  # 封板资金
    first_seal: str | None = None  # 首次封板时间 HH:MM
    blowups: int = 0  # 当日炸板次数
    streak: int = 0  # 连板数（东财 lbc，非 zttj.days）
    # 东财 `zttj` 的「N 天 M 板」：统计窗口，不是连板高度。留着是因为游资口径里
    # 「20 天 10 板」和「9 连板」说的是两件事，两个都得能报出来。
    window_days: int | None = None
    window_limit_ups: int | None = None
    sector: str | None = None  # 东财行业板块名
    limit_price: float | None = None  # 炸板池带涨停价


@dataclass(frozen=True)
class LimitPoolSnapshot:
    """一天的涨停 / 炸板 / 跌停全景。"""

    trade_date: date
    # 请求的日期。与 `trade_date` 不同即说明服务端做了回退，两者都要留痕。
    requested_date: date | None = None
    limit_up: list[LimitStock] = field(default_factory=list)
    blowups: list[LimitStock] = field(default_factory=list)
    limit_down: list[LimitStock] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    @property
    def limit_up_count(self) -> int:
        return len(self.limit_up)

    @property
    def blowup_count(self) -> int:
        return len(self.blowups)

    @property
    def limit_down_count(self) -> int:
        return len(self.limit_down)

    @property
    def blowup_rate(self) -> float | None:
        """炸板率：冲过板的票里没守住的比例。分母为 0 时返回 None，不是 0.0——
        「今天没有票冲板」和「冲板的都守住了」是两件完全不同的事。"""
        attempted = self.limit_up_count + self.blowup_count
        if attempted <= 0:
            return None
        return round(self.blowup_count / attempted, 4)

    @property
    def max_streak(self) -> int:
        return max((item.streak for item in self.limit_up), default=0)

    @property
    def streak_distribution(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for item in self.limit_up:
            if item.streak > 0:
                counts[item.streak] = counts.get(item.streak, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def early_seal_ratio(self) -> float | None:
        """首封早于 `EARLY_SEAL_BEFORE` 的涨停占比。没有首封时间返回 None。

        返回 None 而不是 0.0：「没有一只早盘封板」和「拿不到首封时间」是两件事，
        同 `blowup_rate` 分母为 0 的处置。
        """
        stamped = [item for item in self.limit_up if item.first_seal]
        if not stamped:
            return None
        early = sum(1 for item in stamped if item.first_seal < EARLY_SEAL_BEFORE)
        return round(early / len(stamped), 4)

    @property
    def median_seal_fund(self) -> float | None:
        """有效封单金额的中位数，降低极端封单影响；缺失时返回 None。"""
        return median_of(item.seal_fund for item in self.limit_up)

    @property
    def median_turnover(self) -> float | None:
        """有效涨停股换手率的中位数（百分数，7.89 表示 7.89%）。"""
        return median_of(item.turnover for item in self.limit_up)

    def sector_distribution(self, top: int = SECTOR_TOP) -> list[tuple[str, int]]:
        """涨停按东财行业分组，只数从多到少。并列时按行业名排序，保证可 diff。"""
        counts: dict[str, int] = {}
        for item in self.limit_up:
            if item.sector:
                counts[item.sector] = counts.get(item.sector, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]

    def describe(self) -> list[str]:
        """给简报用的人话描述。每一句都挂在一个实际读数上。"""
        if not self.ok:
            return ["涨停池不可用，本次无连板与炸板读数"]
        lines = [
            (
                f"涨停 {self.limit_up_count} 只、炸板 {self.blowup_count} 只、"
                f"跌停 {self.limit_down_count} 只"
            )
        ]
        rate = self.blowup_rate
        if rate is not None:
            if rate >= BLOWUP_STRESSED:
                verdict = "封板意愿弱，情绪退潮"
            elif rate >= BLOWUP_HEALTHY:
                verdict = "封板有分歧"
            else:
                verdict = "封板结实"
            lines.append(f"炸板率 {rate:.0%}（{verdict}）")
        if self.max_streak:
            if self.max_streak >= STREAK_MOMENTUM:
                verdict = "有资金愿意接力"
            elif self.max_streak <= STREAK_WEAK:
                verdict = "没有持续性资金"
            else:
                verdict = "接力一般"
            lines.append(f"最高连板 {self.max_streak} 板（{verdict}）")
        lines.extend(self._seal_quality_lines())
        return lines

    def _seal_quality_lines(self) -> list[str]:
        """封板质量与扎堆方向。四个读数各自缺数据时单独跳过，不互相拖累。"""
        lines: list[str] = []
        early = self.early_seal_ratio
        if early is not None:
            verdict = "主动性买盘为主" if early >= EARLY_SEAL_STRONG else "多数是盘中或尾盘才封上"
            lines.append(f"早盘封板占比 {early:.0%}（首封早于 {EARLY_SEAL_BEFORE}，{verdict}）")
        # 封单与换手写在同一行：它们是「这些板牢不牢」的两面，拆成两行会让读的人
        # 分别去判断，而单看任何一个都得不出结论——封单薄但换手也低是惜售，
        # 封单薄且换手高才是随时会开板。
        strength = [
            text
            for text in (
                None
                if self.median_seal_fund is None
                else f"封单中位 {self.median_seal_fund / 1e8:.2f} 亿",
                None if self.median_turnover is None else f"换手中位 {self.median_turnover:.1f}%",
            )
            if text
        ]
        if strength:
            # 只报数不下判断：封单多厚、换手多高算牢，都没有校准过的阈值，
            # 给一个会是编的。分档留给读的人和 `review` 攒够样本之后。
            lines.append("封板强度：" + "，".join(strength))
        sectors = self.sector_distribution()
        if sectors:
            lines.append(
                "涨停扎堆的行业：" + "、".join(f"{name} {count} 只" for name, count in sectors)
            )
        return lines


class LimitPoolAdapter:
    """东财涨停池取数器。三个池子并发取，单个失败只丢它自己。"""

    name = "eastmoney-limit-pool"

    def __init__(
        self,
        opener: Any | None = None,
        timeout: float = TIMEOUT_SECONDS,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.page_size = page_size

    def fetch(self, trade_date: date) -> LimitPoolSnapshot:
        stamp = trade_date.strftime("%Y%m%d")
        pools = (POOL_LIMIT_UP, POOL_BLOWUP, POOL_LIMIT_DOWN)

        done, errors = gather(pools, lambda pool: self._fetch_pool(pool, stamp))
        results = dict(done)

        notes: list[str] = []
        failed = [pool for pool, _ in errors]
        if failed:
            reasons = "、".join(
                f"{_POOL_LABELS[pool]}（{type(exc).__name__}）" for pool, exc in errors
            )
            notes.append(f"涨停池取数失败：{reasons}，相关读数本次缺失")

        def rows(pool: str) -> list[LimitStock]:
            entry = results.get(pool)
            return entry[0] if entry else []

        # 问哪天不等于答哪天。响应里的 `qdate` 是服务端**当前所知的最新交易日**，
        # 不是这批行的日期：2026-09-14 盘中实测，问 0911 得到的是周五那 40 只，`qdate`
        # 却是 0914；问 0913（周日）得到空池，`qdate` 同样是 0914。所以：问的日期
        # 晚于它所知的最新交易日（周末、盘前问「今天」），它退回最新那天的池子，
        # 行的日期是 `qdate`；否则行就是问的那一天的（非交易日就是空池）。
        # 此前一律取 `qdate`，回溯到周五时会把周五的池子标成周一的。
        served = [entry[1] for entry in results.values() if entry and entry[1]]
        latest_known = max(served) if served else None
        actual = trade_date
        if latest_known is not None and trade_date > latest_known:
            actual = latest_known
            notes.append(f"涨停池数据实际为 {actual}（请求 {trade_date}，服务端返回最近交易日）")

        snapshot = LimitPoolSnapshot(
            trade_date=actual,
            requested_date=trade_date,
            limit_up=rows(POOL_LIMIT_UP),
            blowups=rows(POOL_BLOWUP),
            limit_down=rows(POOL_LIMIT_DOWN),
            notes=notes,
            ok=len(failed) < len(pools),
        )
        if snapshot.ok and not snapshot.limit_up and not snapshot.blowups:
            # 取到了但空。可能是非交易日，也可能是当天真的没有票冲板——
            # 两种情况都不该被读成"情绪极弱"，所以明说。
            snapshot.notes.append(
                f"{trade_date} 涨停池与炸板池均为空（非交易日或数据尚未生成），本次不给情绪读数"
            )
        return snapshot

    def fetch_recent(
        self,
        trade_date: date,
        lookback: int = LOOKBACK_DAYS,
        *,
        now: datetime | None = None,
    ) -> LimitPoolSnapshot:
        """向前回溯到最近一个**已经收盘**的有数据交易日。

        盘前 08:20 跑批时，今天的池子还不存在；周一跑批时，上一个有数据的日子是
        上周五。硬取「今天」会得到空池，而空池和「昨天没人涨停」长得一模一样——
        那是两个相反的结论，所以宁可回溯也不能把它们混为一谈。

        开盘后跑批时今天的池子**已经存在但还没定格**：2026-09-14 09:33 实测读到
        15 家涨停，情绪周期据此判成「冰点」、当天不出任何候选——而这只是开盘三分钟
        的数。情绪周期回答的是「昨天封住的板今天还封得住吗」，输入必须是上一个走完
        的交易日，所以还在形成中的今日池子跳过，并把这件事写进 notes。
        """
        moment = now if now is not None else beijing_naive_now()
        attempted: list[date] = []
        carried: list[str] = []
        for back in range(lookback + 1):
            day = trade_date - timedelta(days=back)
            snapshot = self.fetch(day)
            attempted.append(day)
            if not snapshot.ok:
                snapshot.notes[:0] = carried
                return snapshot
            if snapshot.trade_date == moment.date() and session_forming(moment):
                carried.append(
                    f"涨停池 {snapshot.trade_date} 盘中尚未定格（{moment:%H:%M}），"
                    "情绪周期按上一个已收盘交易日的池子判定"
                )
                continue
            if snapshot.limit_up or snapshot.blowups:
                snapshot.notes[:0] = carried
                return snapshot
        return LimitPoolSnapshot(
            trade_date=trade_date,
            notes=[
                *carried,
                f"回溯 {lookback} 天（{attempted[0]}..{attempted[-1]}）均无涨停池数据",
            ],
            ok=False,
        )

    def _fetch_pool(self, pool: str, stamp: str) -> tuple[list[LimitStock], date | None]:
        url = BASE_URL.format(pool=pool, size=self.page_size, date=stamp)
        payload = json.loads(
            fetch_text(
                self._opener,
                url,
                timeout=self.timeout,
                referer=REFERER,
                user_agent=BROWSER_USER_AGENT,
            )
        )

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return [], None
        rows = [_parse_stock(row) for row in (data.get("pool") or []) if isinstance(row, dict)]
        return rows, _parse_qdate(data.get("qdate"))


_POOL_LABELS = {POOL_LIMIT_UP: "涨停池", POOL_BLOWUP: "炸板池", POOL_LIMIT_DOWN: "跌停池"}


def _parse_stock(row: dict) -> LimitStock:
    # `m` 是市场标志：0=深, 1=沪。用它拼交易所后缀比按代码前缀猜更可靠。
    code = str(row.get("c") or "").strip()
    suffix = ".SH" if row.get("m") == 1 else ".SZ"

    # `zttj` 是**涨停统计**「N 天 M 板」，不是连板高度：
    #   爱丽家居 {'days': 20, 'ct': 10} + lbc=9  →  20 天 10 板，当前连 9 板
    #   美利云   {'days': 10, 'ct': 5}  + lbc=1  →  10 天 5 板，当前只是首板
    # 把 days 当连板读会把统计窗口长度当成高度，且因为 days 几乎恒存在，lbc 这个
    # 真值永远轮不到。实测 2026-07-31 全市场最高连板 9 板被读成 20 板，直接虚高
    # 情绪分（`sectors/predict.py` 用 max_streak 打分，≥5 加分、≤2 减分——一只
    # 「10 天 3 板」的首板股会把 -0.4 权重翻成 +0.4）。连板高度只认 `lbc`。
    stats = row.get("zttj") if isinstance(row.get("zttj"), dict) else {}
    return LimitStock(
        symbol=f"{code}{suffix}" if code else "",
        name=str(row.get("n") or "").strip(),
        price=_scaled(row.get("p")),
        change_pct=finite(row.get("zdp")),
        amount=finite(row.get("amount")),
        turnover=finite(row.get("hs")),
        seal_fund=finite(row.get("fund")),
        first_seal=_clock(row.get("fbt")),
        blowups=as_int(row.get("zbc")),
        streak=as_int(row.get("lbc")),
        window_days=as_int(stats.get("days")) or None,
        window_limit_ups=as_int(stats.get("ct")) or None,
        sector=(str(row.get("hybk")).strip() or None) if row.get("hybk") else None,
        limit_price=_scaled(row.get("ztp")),
    )


def _scaled(value: Any) -> float | None:
    number = finite(value)
    return None if number is None else round(number / PRICE_SCALE, 3)


def _parse_qdate(value: Any) -> date | None:
    """`20260731` -> date。上游给的是整数。"""
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _clock(value: Any) -> str | None:
    """`92500` -> `09:25`。上游给的是去掉前导零的 HHMMSS 整数。"""
    number = as_int(value)
    if number <= 0:
        return None
    text = f"{number:06d}"
    return f"{text[:2]}:{text[2:4]}"
