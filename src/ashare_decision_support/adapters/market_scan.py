"""全市场扫描：一次枚举所有 A 股，带估值、换手与流通市值。

**和 `snapshot.py` 的分工。** 那个是「给定 N 个标的，取现价与五档盘口」，前提是
你已经知道要看哪些票。这个反过来：**不需要预先知道标的池**，一次把全市场 5500+ 只
连同 `snapshot.py` 拿不到的三类字段一起扫回来——

| 字段 | 谁能给 | 项目里此前有没有 |
|---|---|---|
| 现价 / 五档盘口 | `snapshot.py`（hq.sinajs.cn） | 有 |
| **市盈率 / 市净率** | 本模块 | **没有** |
| **流通市值 / 总市值** | 本模块 | **没有** |
| **换手率 / 成交额** | 本模块 | **没有** |

这三类是超短线选股的**硬门槛**，不是锦上添花：流通市值太小的票拉一个板只要几千万，
成交额太小的票你买得进卖不出。此前候选只过滤 ST 与致命公告，一只 15 亿流通、日成交
3000 万的票和茅台在筛选层面是等价的。

**顺带得到一个更硬的宽度读数。** `sectors/momentum.py` 的 breadth 是 4430 只申万成分股
里有日线的那部分（实测 4236 只）算出来的；这里是全市场 5500+ 只的真实涨跌家数。
2026-07-31 实测：全市场 4690 涨 / 728 跌（85%），申万样本口径 83%。两者接近，但**接近
是结论不是假设**——差得远的时候说明成分股样本有偏，那本身就是要报出来的事。

**数据源。** 新浪 `Market_Center.getHQNodeData`。分页硬上限 100 条/页（实测 num=500
和 num=1000 都只回 100），所以 5500 只要 56 页并发。走 stdlib 直连、字段自己解析，
理由和 `snapshot.py`、`limit_pool.py` 一致：上游改列名时是解析失败而不是静默错值。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.adapters.http import fetch_text
from ashare_decision_support.fanout import gather
from ashare_decision_support.numeric import finite

BASE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
    "?page={page}&num={size}&sort=symbol&asc=1&node={node}&symbol=&_s_r_a=page"
)
REFERER = "https://vip.stock.finance.sina.com.cn/"

NODE_ALL_A = "hs_a"
PAGE_SIZE = 100          # 服务端硬上限，加大无效
MAX_PAGES = 80           # 5500 / 100 = 55 页，留足余量兼防翻页失控
TIMEOUT_SECONDS = 20
MAX_WORKERS = 8

# 超短线可交易性门槛。两个都是**下限**不是选股条件：
#   - 流通市值 20 亿以下，拉一个板只要几千万，价格由不了基本面；
#   - 日成交额 8000 万以下，几百万的仓位就能打穿盘口，买得进卖不出。
# 取值偏松（真做超短通常要求更高），因为这一层的职责是**排除不可交易**，
# 不是替使用者选票。想收紧在调用处传参，不要改这里。
MIN_FLOAT_CAP_YI = 20.0
MIN_AMOUNT_YI = 0.8


@dataclass(frozen=True)
class MarketRow:
    code: str
    name: str
    price: float
    prev_close: float | None = None
    change_pct: float | None = None
    amount: float | None = None          # 元
    volume: float | None = None          # 股
    turnover_ratio: float | None = None  # %
    pe: float | None = None
    pb: float | None = None
    float_cap: float | None = None       # 流通市值，元
    total_cap: float | None = None       # 总市值，元

    @property
    def float_cap_yi(self) -> float | None:
        return None if self.float_cap is None else self.float_cap / 1e8

    @property
    def amount_yi(self) -> float | None:
        return None if self.amount is None else self.amount / 1e8

    @property
    def is_st(self) -> bool:
        upper = self.name.upper().replace(" ", "")
        return "ST" in upper or "退" in upper

    def tradable(
        self,
        min_float_cap_yi: float = MIN_FLOAT_CAP_YI,
        min_amount_yi: float = MIN_AMOUNT_YI,
    ) -> bool:
        """能不能在超短线尺度上进出。**读不到字段一律判可交易**——

        缺字段是我们的观测问题，不是标的的属性。用「没测到」当否决理由，等于让
        数据缺失冒充风险判断。缺失会在调用处单独计数并报出来。
        """
        if self.float_cap_yi is not None and self.float_cap_yi < min_float_cap_yi:
            return False
        return not (self.amount_yi is not None and self.amount_yi < min_amount_yi)


@dataclass
class MarketScan:
    rows: dict[str, MarketRow] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, symbol: str) -> MarketRow | None:
        """按 `600519.SH` 或 `600519` 都能取。"""
        return self.rows.get(symbol.split(".", maxsplit=1)[0])

    # ---- breadth ---------------------------------------------------------

    @property
    def advancers(self) -> int:
        return sum(1 for r in self.rows.values() if (r.change_pct or 0) > 0)

    @property
    def decliners(self) -> int:
        return sum(1 for r in self.rows.values() if (r.change_pct or 0) < 0)

    @property
    def breadth(self) -> float | None:
        moved = self.advancers + self.decliners
        return round(self.advancers / moved, 4) if moved else None

    @property
    def total_amount(self) -> float:
        return sum(r.amount or 0.0 for r in self.rows.values())

    def describe(self) -> list[str]:
        if not self.rows:
            return []
        lines = [
            (f"全市场 {len(self.rows)} 只：涨 {self.advancers} / 跌 {self.decliners}"
            f"（上涨面 {self.breadth:.0%}），成交额 {self.total_amount / 1e12:.2f} 万亿")
        ]
        st = sum(1 for r in self.rows.values() if r.is_st)
        if st:
            lines.append(f"其中 ST/退市风险 {st} 只")
        return lines


class MarketScanAdapter:
    """新浪全市场行情扫描。"""

    name = "sina-market-scan"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        max_workers: int = MAX_WORKERS,
        node: str = NODE_ALL_A,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.max_workers = max_workers
        self.node = node

    def fetch(self, max_pages: int = MAX_PAGES) -> MarketScan:
        notes: list[str] = []
        rows: dict[str, MarketRow] = {}

        pages = list(range(1, max_pages + 1))
        done, errors = gather(pages, self._page, max_workers=self.max_workers)
        failures = len(errors)

        empty_streak = 0
        for _page, payload in done:
            if not payload:
                empty_streak += 1
                continue
            empty_streak = 0
            for raw in payload:
                row = _parse_row(raw)
                if row is not None:
                    rows.setdefault(row.code, row)

        if failures:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            notes.append(
                f"全市场扫描 {failures}/{len(pages)} 页取数失败（{kinds}），宽度读数按已取到的算"
            )
        if not rows:
            return MarketScan(notes=[*notes, "全市场扫描没有取到任何数据"], ok=False)

        notes.append(
            f"全市场扫描 {len(rows)} 只（{len(pages) - failures - empty_streak} 页有数据）"
        )
        return MarketScan(rows=rows, notes=notes, ok=True)

    # ---- internals -------------------------------------------------------


    def _page(self, page: int) -> list[dict]:
        url = BASE_URL.format(page=page, size=PAGE_SIZE, node=self.node)
        text = fetch_text(
            self._opener, url, timeout=self.timeout, referer=REFERER, encoding="gbk"
        )
        text = text.strip()
        if not text or text in ("null", "[]"):
            return []
        payload = json.loads(text)
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _parse_row(raw: dict) -> MarketRow | None:
    code = str(raw.get("code") or "").strip()
    name = str(raw.get("name") or "").strip()
    price = finite(raw.get("trade"))
    if not code or not name or price is None or price <= 0:
        return None

    # 市值字段的单位是**万元**，不是元。忘了换算会让 20 亿的门槛变成 20 万，
    # 等于这一层没开。
    float_cap = finite(raw.get("nmc"))
    total_cap = finite(raw.get("mktcap"))

    return MarketRow(
        code=code,
        name=name,
        price=price,
        prev_close=finite(raw.get("settlement")),
        change_pct=finite(raw.get("changepercent")),
        amount=finite(raw.get("amount")),
        volume=finite(raw.get("volume")),
        turnover_ratio=finite(raw.get("turnoverratio")),
        pe=finite(raw.get("per")),
        pb=finite(raw.get("pb")),
        float_cap=None if float_cap is None else float_cap * 1e4,
        total_cap=None if total_cap is None else total_cap * 1e4,
    )
