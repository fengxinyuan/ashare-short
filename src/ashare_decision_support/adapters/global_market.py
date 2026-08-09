"""盘前海外市场快照，按“主源批量取数、备用源可插拔、缺失可见”设计。

新浪行情是默认主源：一次请求覆盖美股指数、商品、汇率及韩国指数，已在本机出口实测。
Yahoo Finance 不作为默认调用：它在当前出口返回 429，只保留为可显式替换的备用接口，
避免一个受限端点拖慢整份盘前报告。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from ashare_decision_support.numeric import finite

SINA_URL = "https://hq.sinajs.cn/list={codes}"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
TIMEOUT_SECONDS = 10

SINA_INSTRUMENTS = {
    "sp500": ("gb_inx", "标普500", "equity"),
    "nasdaq": ("gb_ixic", "纳斯达克", "equity"),
    "sox": ("gb_sox", "费城半导体", "equity"),
    "wti": ("hf_CL", "WTI原油", "future"),
    "gold": ("hf_GC", "COMEX黄金", "future"),
    "copper": ("hf_HG", "COMEX铜", "future"),
    "usdcny": ("fx_susdcny", "美元/人民币", "fx"),
    "kospi": ("b_KOSPI", "KOSPI", "index"),
    "kosdaq": ("b_KOSDAQ", "KOSDAQ", "index"),
}
YAHOO_INSTRUMENTS = {"vix": ("^VIX", "VIX"), "us10y": ("^TNX", "美国10年期国债收益率")}


@dataclass(frozen=True)
class GlobalMarketReading:
    key: str
    name: str
    symbol: str
    source: str
    last: float | None = None
    previous_close: float | None = None
    change_pct: float | None = None
    as_of: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GlobalMarketSnapshot:
    readings: list[GlobalMarketReading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(item.last is not None for item in self.readings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "readings": [item.to_dict() for item in self.readings],
            "notes": self.notes,
        }


class GlobalMarketAdapter:
    """默认新浪批量快照；Yahoo 仅在显式开启时作为 VIX/美债备用源。"""

    def __init__(
        self,
        fetch_text: Callable[[str], str] | None = None,
        fetch_json: Callable[[str], Any] | None = None,
        sina_instruments: dict[str, tuple[str, str, str]] | None = None,
        yahoo_instruments: dict[str, tuple[str, str]] | None = None,
        include_yahoo_fallback: bool = False,
    ) -> None:
        self.fetch_text = fetch_text or _fetch_text
        self.fetch_json = fetch_json or _fetch_json
        self.sina_instruments = dict(
            SINA_INSTRUMENTS if sina_instruments is None else sina_instruments
        )
        self.yahoo_instruments = dict(
            YAHOO_INSTRUMENTS if yahoo_instruments is None else yahoo_instruments
        )
        self.include_yahoo_fallback = include_yahoo_fallback

    def fetch_snapshot(self) -> GlobalMarketSnapshot:
        readings, notes = self._fetch_sina()
        if self.include_yahoo_fallback:
            readings.extend(self._fetch_yahoo())
        else:
            readings.extend(
                GlobalMarketReading(
                    key, name, symbol, "yahoo", note="未启用备用源（当前出口会限流）"
                )
                for key, (symbol, name) in self.yahoo_instruments.items()
            )
        missing = [item.name for item in readings if item.last is None]
        if missing:
            notes.append(f"海外快照缺失：{'、'.join(missing)}")
        return GlobalMarketSnapshot(readings=readings, notes=notes)

    def _fetch_sina(self) -> tuple[list[GlobalMarketReading], list[str]]:
        codes = ",".join(value[0] for value in self.sina_instruments.values())
        try:
            payload = _parse_sina(self.fetch_text(SINA_URL.format(codes=codes)))
        except Exception as exc:
            return (
                [
                    GlobalMarketReading(
                        key, name, symbol, "sina", note=f"不可用：{type(exc).__name__}"
                    )
                    for key, (symbol, name, _) in self.sina_instruments.items()
                ],
                [f"新浪海外行情不可用（{type(exc).__name__}）"],
            )
        readings = []
        for key, (symbol, name, kind) in self.sina_instruments.items():
            try:
                readings.append(_sina_reading(key, name, symbol, kind, payload.get(symbol, "")))
            except Exception as exc:
                readings.append(
                    GlobalMarketReading(
                        key, name, symbol, "sina", note=f"不可用：{type(exc).__name__}"
                    )
                )
        return readings, []

    def _fetch_yahoo(self) -> list[GlobalMarketReading]:
        readings = []
        for key, (symbol, name) in self.yahoo_instruments.items():
            try:
                readings.append(
                    _yahoo_reading(key, name, symbol, self.fetch_json(_yahoo_url(symbol)))
                )
            except Exception as exc:
                readings.append(
                    GlobalMarketReading(
                        key, name, symbol, "yahoo", note=f"不可用：{type(exc).__name__}"
                    )
                )
        return readings


def _parse_sina(text: str) -> dict[str, str]:
    return dict(re.findall(r'var hq_str_([^=]+)="(.*?)";', text))


def _sina_reading(key: str, name: str, symbol: str, kind: str, raw: str) -> GlobalMarketReading:
    values = raw.split(",") if raw else []
    if not values:
        raise ValueError("empty quote")
    if kind == "equity":
        last, change_pct, as_of, previous = (
            finite(values[1]),
            finite(values[2]),
            values[3],
            finite(values[25]),
        )
    elif kind == "future":
        last, previous, as_of = finite(values[0]), finite(values[7]), f"{values[12]} {values[6]}"
        change_pct = (
            round((last / previous - 1) * 100, 4)
            if last is not None and previous not in (None, 0)
            else None
        )
    elif kind == "fx":
        last, previous, as_of = finite(values[1]), finite(values[2]), f"{values[17]} {values[0]}"
        change_pct = (
            round((last / previous - 1) * 100, 4)
            if last is not None and previous not in (None, 0)
            else None
        )
    else:
        last, change_pct, as_of, previous = (
            finite(values[1]),
            finite(values[3]),
            f"{values[6]} {values[5]}",
            finite(values[8]),
        )
    if last is None:
        raise ValueError("missing price")
    return GlobalMarketReading(key, name, symbol, "sina", last, previous, change_pct, as_of)


def _yahoo_url(symbol: str) -> str:
    return YAHOO_URL.format(symbol=urllib.parse.quote(symbol, safe=""))


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("gb18030", "replace")


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _yahoo_reading(key: str, name: str, symbol: str, payload: Any) -> GlobalMarketReading:
    result = ((payload or {}).get("chart") or {}).get("result") or []
    meta = result[0].get("meta") if result and isinstance(result[0], dict) else {}
    last = finite(meta.get("regularMarketPrice") if isinstance(meta, dict) else None)
    previous = finite(meta.get("previousClose") if isinstance(meta, dict) else None)
    if last is None:
        raise ValueError("missing market price")
    change_pct = round((last / previous - 1) * 100, 4) if previous not in (None, 0) else None
    stamp = meta.get("regularMarketTime")
    as_of = (
        datetime.fromtimestamp(stamp, UTC).isoformat() if isinstance(stamp, (int, float)) else None
    )
    return GlobalMarketReading(key, name, symbol, "yahoo", last, previous, change_pct, as_of)
