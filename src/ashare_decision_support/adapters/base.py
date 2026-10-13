from __future__ import annotations

from datetime import date
from typing import Any

from ashare_decision_support.adapters.daily_cache import DailyBarsCache, cache_for
from ashare_decision_support.numeric import positive
from ashare_decision_support.symbols import normalize_symbol

SECURITY_LIST_FIELDS = ("concepts",)
SECURITY_SCALAR_FIELDS = ("industry", "name_en")

# CLI 参数校验与两条分钟线适配器共用的周期。
SUPPORTED_PERIODS: tuple[str, ...] = ("1", "5", "15", "30", "60")

# 日线回看自然日数；各任务共用，保证均线和分位的输入窗口一致。
DAILY_LOOKBACK_DAYS = 240


def security_names(adapter: Any, symbols: list[str]) -> dict[str, str | None]:
    """代码到简称的映射；元数据不可用时返回空表。"""
    return metadata_names(_adapter_security_rows(adapter, symbols))


def security_metadata(
    adapter: Any,
    symbols: list[str],
    reference_adapter: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """代码到静态画像的映射。

    优先读取行情适配器的 get_security_master；仅在显式提供 reference_adapter 时
    补充名称、行业和概念，避免名称查询隐式增加远端依赖。"""
    normalized_map = {
        raw_symbol: normalize_symbol(raw_symbol)
        for raw_symbol in symbols
        if normalize_symbol(raw_symbol)
    }
    if not normalized_map:
        return {}

    rows = _adapter_security_rows(adapter, list(dict.fromkeys(normalized_map.values())))
    reference = _reference_security_rows(reference_adapter)
    merged: dict[str, dict[str, Any]] = {}
    for raw_symbol, normalized_symbol in normalized_map.items():
        row = dict(rows.get(normalized_symbol) or rows.get(raw_symbol) or {})
        row["symbol"] = normalized_symbol

        info = reference.get(normalized_symbol)
        if info is not None:
            apply_security_metadata(row, info)
        merged[raw_symbol] = row
    return merged


def metadata_names(
    metadata: dict[str, dict[str, Any]],
    symbols: list[str] | None = None,
) -> dict[str, str | None]:
    wanted = symbols if symbols is not None else list(metadata)
    return {
        symbol: (
            metadata.get(symbol, {}).get("name") if isinstance(metadata.get(symbol), dict) else None
        )
        for symbol in wanted
    }


def apply_security_metadata(
    row: dict[str, Any],
    metadata: dict[str, Any] | None,
    *,
    prefer_name: bool = False,
) -> dict[str, Any]:
    """把一条静态画像并进业务行。

    默认保留业务行已有的 `name`，因为它通常来自实时行情源；只有显式要求
    `prefer_name=True` 时，才用静态源名称覆盖。
    """
    if not metadata:
        return row
    if metadata.get("name") and (prefer_name or not row.get("name")):
        row["name"] = metadata["name"]
    for key in SECURITY_SCALAR_FIELDS:
        if key in metadata and (key not in row or not row.get(key)):
            row[key] = metadata[key]
    for key in SECURITY_LIST_FIELDS:
        row[key] = list(metadata.get(key) or row.get(key) or [])
    return row


def apply_security_metadata_rows(
    rows: list[dict[str, Any]],
    metadata_by_symbol: dict[str, dict[str, Any]],
    *,
    prefer_name: bool = False,
) -> list[dict[str, Any]]:
    attached: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        attached.append(
            apply_security_metadata(
                copied,
                metadata_by_symbol.get(symbol),
                prefer_name=prefer_name,
            )
        )
    return attached


def float_shares(adapter: Any, symbols: list[str]) -> dict[str, float]:
    """代码到流通股本（股）的映射；缺失或无效标的不进入结果。

    支持顶层字段及 TickFlow 的 ext 字段。只读取流通股本，不用总股本代替，
    供换手率和筹码估算使用。"""
    getter = getattr(adapter, "get_security_master", None)
    if not callable(getter):
        return {}
    try:
        master = getter(symbols)
    except Exception:
        return {}
    if not isinstance(master, dict):
        return {}

    result: dict[str, float] = {}
    for symbol, row in master.items():
        if not isinstance(row, dict):
            continue
        ext = row.get("ext") if isinstance(row.get("ext"), dict) else {}
        for key in ("float_shares", "流通股本", "circulating_shares"):
            shares = positive(row.get(key, ext.get(key)))
            if shares is not None:
                result[symbol] = shares
                break
    return result


def _adapter_security_rows(adapter: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    getter = getattr(adapter, "get_security_master", None)
    if not callable(getter):
        return {}
    try:
        master = getter(symbols)
    except Exception:
        return {}
    if not isinstance(master, dict):
        return {}
    return {
        normalize_symbol(symbol): row
        for symbol, row in master.items()
        if isinstance(row, dict) and normalize_symbol(symbol)
    }


def _reference_security_rows(reference_adapter: Any | None) -> dict[str, dict[str, Any]]:
    if reference_adapter is None:
        return {}
    load = getattr(reference_adapter, "load", None)
    if not callable(load):
        return {}
    try:
        master = load()
    except Exception:
        return {}
    entries = getattr(master, "entries", None)
    if not isinstance(entries, dict):
        return {}

    rows: dict[str, dict[str, Any]] = {}
    for symbol, info in entries.items():
        normalized = normalize_symbol(symbol)
        if not normalized:
            continue
        rows[normalized] = {
            "symbol": normalized,
            "name": getattr(info, "name", None),
            "name_en": getattr(info, "name_en", None),
            "industry": getattr(info, "industry", None),
            "concepts": list(getattr(info, "concepts", ()) or ()),
        }
    return rows


def fetch_daily_bars(
    adapter: Any,
    symbols: list[str],
    start: date,
    end: date,
    notes: list[str] | None = None,
    *,
    reuse_through: date | None = None,
) -> dict[str, list[dict]]:
    """获取日线，优先使用适配器的批量接口。

    在线成功后保存缓存；请求失败时尝试覆盖所需日期的缓存，并记录覆盖与截止日。
    无可用缓存时保留原异常，缓存写入失败不丢弃在线结果。"""
    batch = getattr(adapter, "get_historical_bars_batch", None)
    try:
        cache = cache_for(adapter)
    except OSError:
        cache = None
        if notes is not None:
            notes.append("日线缓存目录不可用，本次只使用在线行情")
    reused: dict[str, list[dict]] = {}
    if cache is not None and reuse_through is not None:
        for symbol in symbols:
            rows = cache.read(symbol, start, end)
            if rows and cache.latest_date(rows) >= reuse_through:
                reused[symbol] = rows
    missing = [symbol for symbol in symbols if symbol not in reused]
    if reused and notes is not None:
        notes.append(
            f"已复用完整收盘日日线缓存 {len(reused)}/{len(symbols)} 只，截止要求 {reuse_through}"
        )
    if not missing:
        return reused
    try:
        fetched = (
            batch(missing, start, end)
            if callable(batch)
            else adapter.get_historical_bars(missing, start, end)
        )
        result = {**reused, **fetched}
    except Exception as exc:
        result = _cached_daily_bars(cache, symbols, start, end)
        if result and cache is not None:
            latest = max(
                (cache.latest_date(rows) for rows in result.values()),
                default=None,
            )
            if notes is not None:
                notes.append(
                    f"行情源失败（{type(exc).__name__}），已回退日线缓存："
                    f"{len(result)}/{len(symbols)} 只，缓存截止 {latest or '未知'}；结果不可视为实时"
                )
            return {symbol: result.get(symbol, []) for symbol in symbols}
        raise

    _cache_daily_bars(cache, fetched, notes)
    return result


def _cached_daily_bars(
    cache: DailyBarsCache | None, symbols: list[str], start: date, end: date
) -> dict[str, list[dict]]:
    if cache is None:
        return {}
    return {symbol: rows for symbol in symbols if (rows := cache.read(symbol, start, end))}


def _cache_daily_bars(
    cache: DailyBarsCache | None, result: dict[str, list[dict]], notes: list[str] | None
) -> None:
    if cache is None:
        return
    failed = 0
    for symbol, rows in result.items():
        if rows:
            try:
                cache.write(symbol, rows)
            except (OSError, TypeError, ValueError):
                failed += 1
    if failed and notes is not None:
        notes.append(f"日线缓存写入失败（{failed} 只），本次仍使用在线行情")
