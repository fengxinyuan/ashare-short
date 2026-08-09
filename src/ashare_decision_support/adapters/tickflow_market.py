"""付费档 TickFlow 的实时行情、五档与全市场扫描。

**这一路取代的是什么。** 免费档没有 `quotes`/`depth`，所以实时快照走新浪
`hq.sinajs.cn`（`snapshot.py`）、全市场扫描走新浪 `Market_Center`
（`market_scan.py`）。付费档把这两件事收进同一个来源，且都是批量接口：

| 读数 | 免费路径 | 付费路径 | 实测对比 |
|---|---|---|---|
| 全市场行情 | 新浪 Market_Center 56 页并发 | `quotes.get(universes=['CN_Equity_A'])` | 5529 只 / 5.3s → **5534 只 / 0.7s** |
| 五档盘口 | 新浪 hq.sinajs 批量 | `depth.batch()` | 500 只 / 0.79s |
| 涨跌停价 | 腾讯 qt.gtimg 单票 | `instruments.batch()` | 全 A **5534 只 / 0.97s，涨停价 100% 覆盖** |

最后一行解掉了一条记了很久的已知边界：`strong_count` 此前用「涨幅 ≥ 9.5%」近似
涨停，既分不清 20% 的创业板/科创板，也认不出一字板。现在每只票的真实涨跌停价
都能一次批量取到。

**它不取代新浪的全部。** PE/PB 只有 `Market_Center` 有——TickFlow 的
`financials.*` 在 Pro 档报 `PermissionError: 无公司财务数据查询权限`，属于更高
档位。所以两路是**互补**不是替换，`CompositeMarketScanAdapter` 负责合并。

## 三个实测出来的单位口径

跟新浪逐档对照过（2026-08-02，价格逐档完全一致，可以互相校验）：

1. **`volume` 是「手」不是「股」**，×100 才是股。茅台 quote `volume=55128`，
   新浪同一时刻 `5512752` 股；depth 的 `bid_volumes` 同理（TickFlow `[1,1,5]`
   对新浪 `[100,100,500]`）。
2. **`ext.change_pct` 是比值不是百分数**（`-0.0082` = −0.82%），而
   `MarketRow.change_pct` 沿用新浪 `changepercent` 的百分数口径，必须 ×100。
3. **流通市值要自己算**：`instruments.ext.float_shares × 现价`。新浪 `nmc` 是
   万元，这里是元，两条路径都归一到元之后才进 `MarketRow`。

单位错一个数量级，`LiquidityScreen` 20 亿的门槛就变成 20 万，等于这层没开——
所以这三条都有测试钉住。
"""

from __future__ import annotations

import time
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from ashare_decision_support.adapters.market_scan import MarketRow, MarketScan
from ashare_decision_support.adapters.snapshot import OrderBookLevel, Snapshot
from ashare_decision_support.adapters.validate import cross_check
from ashare_decision_support.numeric import finite
from ashare_decision_support.symbols import normalize_symbol

# 全 A 标的池 id。TickFlow 的池名不是自解释的：`CN_Equity` / `CN_Equity_All`
# 都报 `NotFoundError: 标的池不存在`，只有这一个是对的。
UNIVERSE_ALL_A = "CN_Equity_A"

# `instruments.batch` 的单次上限，见 OpenAPI 的「最多 1000 个」。实测传 5534 个
# 也能过（SDK 内部分批），显式分批只是为了不依赖这个未文档化的行为。
INSTRUMENT_BATCH = 1000
DEPTH_BATCH = 500

# 手 → 股。见模块头「单位口径」第 1 条。
LOTS_TO_SHARES = 100.0


# ---- 全市场扫描 -----------------------------------------------------------


class TickFlowMarketScanAdapter:
    """全市场行情扫描，接口与 `MarketScanAdapter` 一致（`.fetch() -> MarketScan`）。"""

    name = "tickflow-market-scan"

    def __init__(
        self,
        client: Any,
        universe: str = UNIVERSE_ALL_A,
        with_instruments: bool = True,
    ) -> None:
        self.client = client
        self.universe = universe
        # 关掉可以省一次 ~1 秒的调用，代价是拿不到流通市值——也就是
        # `LiquidityScreen` 的主判据没了。默认开。
        self.with_instruments = with_instruments

    def fetch(self) -> MarketScan:
        notes: list[str] = []
        started = time.monotonic()
        try:
            quotes = self._quotes()
        except Exception as exc:
            return MarketScan(
                notes=[f"TickFlow 全市场行情不可用（{type(exc).__name__}: {exc}）"], ok=False
            )
        if not quotes:
            return MarketScan(notes=["TickFlow 全市场行情返回空"], ok=False)

        shares: dict[str, dict] = {}
        if self.with_instruments:
            symbols = [str(row.get("symbol") or "") for row in quotes]
            shares, shares_notes = self._instruments(symbols)
            notes.extend(shares_notes)

        rows: dict[str, MarketRow] = {}
        for raw in quotes:
            row = _to_market_row(raw, shares)
            if row is not None:
                rows[row.code] = row

        if not rows:
            return MarketScan(notes=[*notes, "TickFlow 全市场行情没有可解析的行"], ok=False)

        elapsed = time.monotonic() - started
        priced = sum(1 for row in rows.values() if row.float_cap is not None)
        notes.append(
            f"TickFlow 全市场扫描 {len(rows)} 只 / {elapsed:.1f} 秒"
            f"（{priced} 只带流通市值）"
        )
        if priced < len(rows):
            # 没有流通市值 = 这只票在可交易性这层是「未测到」，会被放行。
            # 静默放行和「因为缺数据而放行」是两件事，必须说出来。
            notes.append(
                f"{len(rows) - priced} 只缺流通股本，可交易性筛选对它们不生效（一律放行）"
            )
        return MarketScan(rows=rows, notes=notes, ok=True)

    def health_check(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            rows = self._quotes()
            ok, error = bool(rows), None
        except Exception as exc:
            rows, ok, error = [], False, f"{type(exc).__name__}: {exc}"
        return {
            "source": self.name,
            "mode": "paid",
            "reachable": ok,
            "symbols": len(rows),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": error,
        }

    # ---- io --------------------------------------------------------------

    def _quotes(self) -> list[dict]:
        payload = self.client.quotes.get(universes=[self.universe])
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def _instruments(self, symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
        return _fetch_instruments(self.client, symbols)


def _to_market_row(raw: dict, shares: dict[str, dict]) -> MarketRow | None:
    symbol = str(raw.get("symbol") or "").strip()
    price = finite(raw.get("last_price"))
    if not symbol or price is None or price <= 0:
        # 停牌票返回 0 价。把 0.00 当价格发布出去，比报「这只没取到」更糟。
        return None
    ext = raw.get("ext") if isinstance(raw.get("ext"), dict) else {}
    code = symbol.split(".")[0]
    name = str(ext.get("name") or "").strip()

    volume_lots = finite(raw.get("volume"))
    # 「手」→「股」。忘了换会让换手率与流通市值同时错 100 倍。
    volume = None if volume_lots is None else volume_lots * LOTS_TO_SHARES

    turnover = finite(ext.get("turnover_rate"))
    change = finite(ext.get("change_pct"))

    inst_ext = (shares.get(symbol) or {}).get("ext") or {}
    float_shares = finite(inst_ext.get("float_shares"))
    total_shares = finite(inst_ext.get("total_shares"))
    if not name:
        name = str((shares.get(symbol) or {}).get("name") or "").strip()

    return MarketRow(
        code=code,
        name=name or code,
        price=price,
        prev_close=finite(raw.get("prev_close")),
        # 比值 → 百分数，对齐新浪 `changepercent` 的口径。
        change_pct=None if change is None else change * 100.0,
        amount=finite(raw.get("amount")),
        volume=volume,
        turnover_ratio=None if turnover is None else turnover * 100.0,
        # PE/PB 需要 `financials.*`，Pro 档报「无公司财务数据查询权限」。
        # 留 None 而不是填 0——0 会被下游当成真实读数。
        pe=None,
        pb=None,
        float_cap=None if float_shares is None else float_shares * price,
        total_cap=None if total_shares is None else total_shares * price,
    )


# ---- 实时快照与五档 --------------------------------------------------------


class TickFlowSnapshotAdapter:
    """批量快照 + 五档，接口与 `SnapshotAdapter` 一致。

    比免费路径多的是**真实涨跌停价**：`instruments` 对全 A 5534 只 100% 覆盖，
    所以 `Snapshot.at_limit_up` 不再是 None，一字板与 20% 板都能判准。
    """

    name = "tickflow-snapshot"

    def __init__(self, client: Any, with_depth: bool = True, with_limits: bool = True) -> None:
        self.client = client
        self.with_depth = with_depth
        self.with_limits = with_limits

    def get_snapshots(self, symbols: list[str]) -> tuple[dict[str, Snapshot], list[str]]:
        codes = [normalize_symbol(item) for item in symbols if normalize_symbol(item)]
        if not codes:
            return {}, []

        notes: list[str] = []
        try:
            payload = self.client.quotes.get(symbols=codes)
        except Exception as exc:
            return {}, [f"TickFlow 快照请求失败（{type(exc).__name__}），本轮缺 {len(codes)} 只"]
        quotes = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

        depth: dict[str, dict] = {}
        if self.with_depth:
            depth, depth_notes = self._depth(codes)
            notes.extend(depth_notes)

        instruments: dict[str, dict] = {}
        if self.with_limits:
            instruments, inst_notes = _fetch_instruments(self.client, codes)
            notes.extend(inst_notes)

        parsed: dict[str, Snapshot] = {}
        for raw in quotes:
            snapshot = _to_snapshot(raw, depth, instruments)
            if snapshot is not None:
                parsed[snapshot.symbol] = snapshot

        missing = [code for code in codes if code not in parsed]
        if missing:
            notes.append(f"以下标的未取到快照：{'、'.join(missing[:8])}")
        return parsed, notes

    def health_check(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            parsed, _ = self.get_snapshots(["600519.SH"])
            ok, error = bool(parsed), None
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        return {
            "source": self.name,
            "mode": "paid",
            "reachable": ok,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": error,
        }

    # ---- io --------------------------------------------------------------

    def _depth(self, codes: list[str]) -> tuple[dict[str, dict], list[str]]:
        out: dict[str, dict] = {}
        failed = 0
        for start in range(0, len(codes), DEPTH_BATCH):
            chunk = codes[start : start + DEPTH_BATCH]
            try:
                payload = self.client.depth.batch(symbols=chunk)
            except Exception:
                failed += len(chunk)
                continue
            if isinstance(payload, dict):
                out.update({k: v for k, v in payload.items() if isinstance(v, dict)})
        if failed:
            # 缺盘口不是缺价格：快照照发，只是 spread/挂单量读不到。
            return out, [f"五档盘口有 {failed} 只未取到，这些标的的价差读数为空"]
        return out, []


def _to_snapshot(
    raw: dict, depth: dict[str, dict], instruments: dict[str, dict]
) -> Snapshot | None:
    symbol = str(raw.get("symbol") or "").strip()
    last = finite(raw.get("last_price"))
    if not symbol or not last:
        return None
    ext = raw.get("ext") if isinstance(raw.get("ext"), dict) else {}
    volume_lots = finite(raw.get("volume"))

    book = depth.get(symbol) or {}
    bids = _levels(book.get("bid_prices"), book.get("bid_volumes"))
    asks = _levels(book.get("ask_prices"), book.get("ask_volumes"))

    inst_ext = (instruments.get(symbol) or {}).get("ext") or {}
    name = str(ext.get("name") or "").strip() or str(
        (instruments.get(symbol) or {}).get("name") or ""
    ).strip()

    return Snapshot(
        symbol=symbol,
        name=name or None,
        last_price=last,
        prev_close=finite(raw.get("prev_close")),
        open_price=finite(raw.get("open")),
        high=finite(raw.get("high")),
        low=finite(raw.get("low")),
        volume=None if volume_lots is None else volume_lots * LOTS_TO_SHARES,
        amount=finite(raw.get("amount")),
        as_of=_stamp(raw.get("timestamp")),
        bids=bids,
        asks=asks,
        limit_up=finite(inst_ext.get("limit_up")),
        limit_down=finite(inst_ext.get("limit_down")),
        source="tickflow",
    )


def _levels(prices: Any, volumes: Any) -> list[OrderBookLevel]:
    if not isinstance(prices, list) or not isinstance(volumes, list):
        return []
    out: list[OrderBookLevel] = []
    # 档位数不齐就按短的那边截断——半档挂单不是读数。
    for price, volume in zip(prices, volumes, strict=False):
        value = finite(price)
        if value is None or value <= 0:
            continue
        # 盘口量同样是「手」，换成股才能和新浪那路的读数并排看。
        out.append(OrderBookLevel(price=value, volume=(finite(volume) or 0.0) * LOTS_TO_SHARES))
    return out


# ---- 合并：付费优先、免费兜底 ----------------------------------------------


class CompositeMarketScanAdapter:
    """付费档优先，缺什么用免费档补。

    合并而不是二选一，有两个具体理由：

    1. **PE/PB 只有新浪有**（TickFlow 的 `financials.*` 在 Pro 档无权限），而
       付费档有流通股本和更快的全市场行情。两边字段互补。
    2. **单点风险**。本机在出口代理白名单后面，东财整条 `push2` 就是这么没的。
       多一条走得通的独立 host，是在给这件事买保险。
    """

    name = "composite-market-scan"

    def __init__(self, primary: Any, fallback: Any | None = None) -> None:
        self.primary = primary
        self.fallback = fallback

    def fetch(self) -> MarketScan:
        scan = _safe_fetch(self.primary)
        if self.fallback is None:
            return scan if scan is not None else MarketScan(
                notes=["全市场扫描不可用，且没有配置兜底源"], ok=False
            )

        other = _safe_fetch(self.fallback)
        if scan is None or not scan.ok:
            if other is None or not other.ok:
                return MarketScan(notes=["全市场扫描两路都不可用"], ok=False)
            other.notes.append("主源不可用，本次全市场扫描走兜底源")
            return other
        if other is None or not other.ok:
            scan.notes.append("兜底源不可用，本次缺 PE/PB")
            return scan

        merged, filled = _merge_rows(scan.rows, other.rows)
        notes = list(scan.notes) + list(other.notes)
        notes.append(
            f"两路合并：{len(merged)} 只（主源 {len(scan.rows)}、兜底 {len(other.rows)}，"
            f"补齐 PE/PB {filled} 只）"
        )
        # 合并有个盲区：**两路都有的字段从来没被比过**。主源某个字段悄悄换了量纲、
        # 喂了昨天的数、或者标的映射错位，合并会照单全收，而兜底那一路就摆在旁边。
        # 比对只出 note、不改数——合并策略是上面那一段的事，混在一起会让
        # 「这个数用的是哪一路」变得不可追溯。
        notes.extend(_cross_check_notes(scan.rows, other.rows))
        return MarketScan(rows=merged, notes=notes, ok=True)


# 两路都提供、且量纲上最容易出事的字段。价格类放进来是因为它们是标的映射错位的
# 最灵敏探针：映射一错，同一个 code 下两路的价格会差出数量级。
CROSS_CHECKED_FIELDS = ("price", "prev_close", "change_pct", "amount", "volume", "float_cap")


def _cross_check_notes(
    primary: dict[str, MarketRow], fallback: dict[str, MarketRow]
) -> list[str]:
    """跨源逐字段比对，返回给简报的说明。任何异常都吞掉——校验挂了不该拖垮取数。"""
    try:
        result = cross_check(
            {code: asdict(row) for code, row in primary.items()},
            {code: asdict(row) for code, row in fallback.items()},
            fields=CROSS_CHECKED_FIELDS,
        )
        return result.notes()
    except Exception:
        return []


def _safe_fetch(source: Any) -> MarketScan | None:
    try:
        return source.fetch()
    except Exception:
        return None


def _merge_rows(
    primary: dict[str, MarketRow], fallback: dict[str, MarketRow]
) -> tuple[dict[str, MarketRow], int]:
    """主源为准，只用兜底源补主源为 None 的字段。

    不是「哪个新用哪个」——两个源的口径不完全一样（成交额统计口径、停牌处理），
    逐字段挑选会造出一份哪一路都不等于的混合读数。主源整行为准，兜底只填空。
    """
    merged: dict[str, MarketRow] = {}
    filled = 0
    for code, row in primary.items():
        other = fallback.get(code)
        if other is None:
            merged[code] = row
            continue
        patch: dict[str, Any] = {}
        for field_name in ("pe", "pb", "float_cap", "total_cap", "turnover_ratio", "prev_close"):
            if getattr(row, field_name) is None and getattr(other, field_name) is not None:
                patch[field_name] = getattr(other, field_name)
        if patch:
            filled += 1
            row = replace(row, **patch)
        merged[code] = row

    # 主源没有、兜底有的标的照收——覆盖面取并集。
    for code, row in fallback.items():
        merged.setdefault(code, row)
    return merged, filled


# ---- shared --------------------------------------------------------------


def _fetch_instruments(client: Any, symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
    """symbol → 标的元数据（名称/涨跌停价/流通股本）。失败只降级，不抛。

    整批下发，分批交给 SDK——实测全 A 5534 只一次调用 **0.97 秒**，而自己切成
    6 段串行要 3 秒多。只有整批调不通时才退回自己分段，好让一段失败不至于把
    另外五段的结果一起丢掉。
    """
    wanted = [item for item in symbols if item]
    if not wanted:
        return {}, []

    whole = _instruments_call(client, wanted)
    if whole is not None:
        return whole, _missing_note(wanted, whole)

    out: dict[str, dict] = {}
    failed = 0
    for start in range(0, len(wanted), INSTRUMENT_BATCH):
        chunk = wanted[start : start + INSTRUMENT_BATCH]
        parsed = _instruments_call(client, chunk)
        if parsed is None:
            failed += len(chunk)
            continue
        out.update(parsed)
    notes = _missing_note(wanted, out)
    if failed:
        notes.append(f"标的元数据有 {failed} 只请求失败，这些标的没有真实涨跌停价")
    return out, notes


def _instruments_call(client: Any, symbols: list[str]) -> dict[str, dict] | None:
    """``None`` = 这次调用失败（与「返回了但缺标的」区分开）。"""
    try:
        payload = client.instruments.batch(symbols)
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    return {
        str(item["symbol"]): item
        for item in payload
        if isinstance(item, dict) and item.get("symbol")
    }


def _missing_note(wanted: list[str], got: dict[str, dict]) -> list[str]:
    missing = len(wanted) - sum(1 for item in wanted if item in got)
    if not missing:
        return []
    return [f"标的元数据缺 {missing} 只，这些标的没有真实涨跌停价"]


def _stamp(value: Any) -> datetime | None:
    """毫秒时间戳 → 本地 datetime。"""
    try:
        return datetime.fromtimestamp(int(value) / 1000.0)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
