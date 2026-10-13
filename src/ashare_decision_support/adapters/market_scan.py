"""全市场实时行情扫描，提供估值、换手、流通市值与成交额。

结果用于市场宽度、盘中板块截面和可交易性筛选；缺失字段保留未知。
免费与付费路径、量纲及跨源合并规则见 docs/DATA_SOURCES.md。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ashare_decision_support.adapters.http import fetch_text
from ashare_decision_support.fanout import gather
from ashare_decision_support.numeric import finite
from ashare_decision_support.schedule.clock import is_continuous_session
from ashare_decision_support.schedule.timezone import beijing_naive_now
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import read_json_object, write_json

BASE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
    "?page={page}&num={size}&sort=symbol&asc=1&node={node}&symbol=&_s_r_a=page"
)
REFERER = "https://vip.stock.finance.sina.com.cn/"

NODE_ALL_A = "hs_a"
PAGE_SIZE = 100  # 服务端硬上限，加大无效
MAX_PAGES = 80  # 5500 / 100 = 55 页，留足余量兼防翻页失控
TIMEOUT_SECONDS = 20
MAX_WORKERS = 8
SCAN_CACHE_SECONDS = 300
SCAN_CACHE_FILE = "cache/market_scan_latest.json"

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
    amount: float | None = None  # 元
    volume: float | None = None  # 股
    turnover_ratio: float | None = None  # %
    pe: float | None = None
    pb: float | None = None
    float_cap: float | None = None  # 流通市值，元
    total_cap: float | None = None  # 总市值，元

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
    complete: bool = True
    # 取数时刻，以及取到的是不是半截交易日。全市场扫描是实时快照：盘前拿到的是上一
    # 交易日的整段，盘后是今天的整段，09:15–15:00 之间拿到的成交额与涨跌幅都还在
    # 形成中。由取数那一层（`jobs/sources.py:fetch_market_scan`）盖戳，读的一层
    # （`screens/liquidity.py`）据此决定成交额门槛还能不能用。
    as_of: datetime | None = None
    session_in_progress: bool = False

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, symbol: str) -> MarketRow | None:
        """按 `600519.SH` 或 `600519` 都能取。"""
        return self.rows.get(symbol.split(".", maxsplit=1)[0])

    # breadth

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
        breadth = self.breadth
        # 涨跌幅全部缺失或为 0（隔夜行情源把涨跌幅清零之后就是这样）时上涨面算不
        # 出来。写成一句「无读数」，而不是让一个 None 在格式化时把整轮盘前跑崩——
        # 2026-09-14 01:10 实测就崩在这里。
        face = f"上涨面 {breadth:.0%}" if breadth is not None else "涨跌幅全部缺失，上涨面无读数"
        lines = [
            (
                f"全市场 {len(self.rows)} 只：涨 {self.advancers} / 跌 {self.decliners}"
                f"（{face}），成交额 {self.total_amount / 1e12:.2f} 万亿"
            )
        ]
        if self.session_in_progress:
            stamp = f"截至 {self.as_of:%H:%M}" if self.as_of is not None else "盘中"
            lines.append(
                f"⚠️ 全市场扫描取自盘中（{stamp}），成交额与涨跌为半截交易日的读数，不能和收盘口径比"
            )
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
        cache_enabled = self._opener is urllib.request.urlopen and max_pages == MAX_PAGES
        if cache_enabled and (cached := self._read_recent_cache()) is not None:
            return cached
        notes: list[str] = []
        rows: dict[str, MarketRow] = {}

        pages = list(range(1, max_pages + 1))
        done, errors = gather(pages, self._page, max_workers=self.max_workers)
        failures = len(errors)

        populated_pages: set[int] = set()
        for page, payload in done:
            if not payload:
                continue
            populated_pages.add(page)
            for raw in payload:
                row = _parse_row(raw)
                if row is not None:
                    rows.setdefault(row.code, row)

        last_page = max(populated_pages, default=0)
        missing_inside = sorted(set(range(1, last_page + 1)) - populated_pages)
        complete = not failures and not missing_inside

        if failures:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            notes.append(
                f"全市场扫描 {failures}/{len(pages)} 页取数失败（{kinds}），宽度读数按已取到的算"
            )
        if missing_inside and not failures:
            notes.append(f"全市场扫描前 {last_page} 页有 {len(missing_inside)} 页为空，截面不完整")
        if not rows:
            return MarketScan(
                notes=[*notes, "全市场扫描没有取到任何数据"], ok=False, complete=False
            )

        notes.append(f"全市场扫描 {len(rows)} 只（{len(populated_pages)} 页有数据）")
        scan = MarketScan(rows=rows, notes=notes, ok=True, complete=complete)
        if cache_enabled and complete and len(rows) >= 5000:
            self._save_recent_cache(scan)
        return scan

    @staticmethod
    def _read_recent_cache() -> MarketScan | None:
        try:
            payload = read_json_object(state_path(SCAN_CACHE_FILE))
        except OSError:
            return None
        if payload.get("schema") != "market-scan-cache-v1":
            return None
        try:
            stamp = datetime.fromisoformat(payload["as_of"])
            now = beijing_naive_now()
            rows = {code: MarketRow(**value) for code, value in payload["rows"].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        age = (now - stamp).total_seconds()
        if (
            stamp.date() != now.date()
            or not 0 <= age <= SCAN_CACHE_SECONDS
            or is_continuous_session(stamp) != is_continuous_session(now)
            or len(rows) < 5000
        ):
            return None
        return MarketScan(
            rows=rows, notes=[f"全市场扫描复用 {stamp:%H:%M} 的完整截面"], ok=True, as_of=stamp
        )

    @staticmethod
    def _save_recent_cache(scan: MarketScan) -> None:
        with suppress(OSError, TypeError, ValueError):
            write_json(
                state_path(SCAN_CACHE_FILE),
                {
                    "schema": "market-scan-cache-v1",
                    "as_of": beijing_naive_now().isoformat(timespec="seconds"),
                    "rows": {code: asdict(row) for code, row in scan.rows.items()},
                },
            )

    def _page(self, page: int) -> list[dict]:
        url = BASE_URL.format(page=page, size=PAGE_SIZE, node=self.node)
        text = fetch_text(self._opener, url, timeout=self.timeout, referer=REFERER, encoding="gbk")
        text = text.strip()
        if not text or text in ("null", "[]"):
            return []
        payload = json.loads(text)
        return (
            [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )


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
