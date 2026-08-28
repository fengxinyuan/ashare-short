from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ashare_decision_support.adapters.daily_cache import cache_for
from ashare_decision_support.numeric import positive
from ashare_decision_support.symbols import normalize_symbol

SECURITY_LIST_FIELDS = ("industries", "concepts")
SECURITY_SCALAR_FIELDS = ("industry", "industry_leaf", "industry_path", "name_en")

# 分钟线周期的仓内写法。**两个盘中适配器必须认同一张表**：`build_intraday_adapter`
# 在运行时才决定给出付费档（TickFlow）还是免费档（新浪），而 `cli.py` 把这张表
# 当成六条命令 `--period` 的 `choices` 用——校验发生在**解析参数时**，取数发生在
# **另一个适配器里**。两份各自演化的话，分叉不会在改动那天报错：它表现为某个周期
# 在 CLI 上能敲进去，跑到一半被适配器以 ValueError 顶回来，或者反过来，付费档明明
# 支持的周期敲不进去。同 `tickflow_intraday.py` 对两个适配器签名的那条要求。
SUPPORTED_PERIODS: tuple[str, ...] = ("1", "5", "15", "30", "60")

# 取多少天日线。240 个自然日约等于一个交易年，是 `DailyTechnicalAnalyzer` 与
# `WyckoffAnalyzer` 那几个 60/120 日窗口能算满所需的量。
#
# 和 `fetch_daily_bars` 收在一起是因为**它们坏的方式一样**：那个函数的 docstring
# 说的是四份逐字相同的取数实现改一处改不全，这个数原来也是三份（`monitor/session`、
# `agents/collectors`、`jobs/advise`），而它比取数实现更隐蔽——三处不一致不表现为
# 「慢」，表现为**同一只票经 `advise` 和经盘中监控算出来的均线与分位不一样**，
# 两个数都不报错。（`jobs/advise` 那一份 2026-08-15 清理时发现是**定义了从没被
# 读过**的：接线审计按名字数引用，另外两处的同名常量替它顶了账。）
DAILY_LOOKBACK_DAYS = 240


class MarketDataAdapter(ABC):
    @abstractmethod
    def get_security_master(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch static symbol metadata."""

    @abstractmethod
    def get_historical_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
        """Fetch historical OHLCV bars."""

    @abstractmethod
    def get_trading_calendar(self) -> list[date]:
        """Fetch trading days."""

def security_names(adapter: Any, symbols: list[str]) -> dict[str, str | None]:
    """代码 → 简称。拿不到就返回空表。

    名字是**纯展示字段**：没有它简报里显示的是代码，有它显示的是「贵州茅台」。
    所以这里三道防线全部退化成空表而不是抛异常——为了一个不影响任何判定的
    字段中断整轮跑批，是把展示层的故障升级成了链路故障。

    收在这里而不是各 job 各写一份：`morning` / `post_close` / `monitor` 原来
    有三份逐字相同的实现，而它们对同一个适配器提同一个问题。
    """
    return metadata_names(_adapter_security_rows(adapter, symbols))


def security_metadata(
    adapter: Any,
    symbols: list[str],
    reference_adapter: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """代码 → 合并后的静态画像。

    基础信息优先取行情适配器自己的 `get_security_master`；如果调用方**显式**提供
    `reference_adapter`，再把中文名 / 行业路径 / 概念标签补进来。这里故意不默认
    创建远端 reference 源——默认行为必须保持纯本地/现有适配器，不把一个展示增强
    悄悄升级成新的外网依赖。
    """
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
            metadata.get(symbol, {}).get("name")
            if isinstance(metadata.get(symbol), dict)
            else None
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
    """代码 → 流通股本（**股**）。拿不到的标的直接不出现在结果里。

    这个字段决定两样读数能不能算：`signals/volume_price.py` 的换手率，和
    `signals/chips.py` 的筹码分布（换手率就是它的衰减速度）。两者在缺失时都整体
    弃权，所以这里**宁可少给一只，也不能给错一只**——猜一个股本会让换手率错到
    没边，而错误的换手率不会报错，只会让每只票看起来都在天量或地量。

    字段位置按源不同：TickFlow 放在 `ext.float_shares`（实测 600519 的 ext 里
    同时有 `total_shares`），别的源可能放在顶层。**只认流通股本，不拿总股本顶替**
    ——两者对茅台恰好相等，对绝大多数有限售股的票差很多，而「恰好相等」正是这类
    错误最容易被漏掉的原因。
    """
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
        industries = tuple(getattr(info, "industries", ()) or ())
        rows[normalized] = {
            "symbol": normalized,
            "name": getattr(info, "name", None),
            "name_en": getattr(info, "name_en", None),
            "industries": list(industries),
            "industry": industries[0] if industries else None,
            "industry_leaf": industries[-1] if industries else None,
            "industry_path": "·".join(item for item in industries if item) or None,
            "concepts": list(getattr(info, "concepts", ()) or ()),
        }
    return rows


def fetch_daily_bars(
    adapter: Any, symbols: list[str], start: date, end: date, notes: list[str] | None = None
) -> dict[str, list[dict]]:
    """取日线。适配器有批量接口就走批量，没有就走逐只那条。

    `get_historical_bars_batch` 不在 `MarketDataAdapter` 里，因为不是每个源都
    有——TickFlow 一次请求能带一串代码，新浪只能一只一只问。**能省一轮网络往返
    的适配器不该被接口拉平到最慢的那个**，所以按能力探测，而不是按类型分支。

    原来 `collectors` / `morning` / `verify` / `monitor.session` 各有一份逐字
    相同的实现。四份的问题不在重复，在于哪天多一个源支持批量，改一处改不全，
    剩下三条路仍然逐只打——而且只表现为「慢」，不表现为「错」。
    """
    batch = getattr(adapter, "get_historical_bars_batch", None)
    cache = cache_for(adapter)
    try:
        result = batch(symbols, start, end) if callable(batch) else adapter.get_historical_bars(symbols, start, end)
    except Exception as exc:
        result = {}
        recovered: list[str] = []
        for symbol in symbols:
            rows = cache.read(symbol, start, end)
            if rows:
                result[symbol] = rows
                recovered.append(symbol)
        if recovered:
            latest = max(
                (cache.latest_date(result[symbol]) for symbol in recovered),
                default=None,
            )
            if notes is not None:
                notes.append(
                    f"行情源失败（{type(exc).__name__}），已回退日线缓存："
                    f"{len(recovered)}/{len(symbols)} 只，缓存截止 {latest or '未知'}；结果不可视为实时"
                )
            return {symbol: result.get(symbol, []) for symbol in symbols}
        raise

    for symbol, rows in result.items():
        if rows:
            try:
                cache.write(symbol, rows)
            except OSError:
                if notes is not None:
                    notes.append("日线缓存写入失败，本次仍使用在线行情")
    return result
