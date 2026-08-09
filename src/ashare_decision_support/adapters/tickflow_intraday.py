"""付费档分钟线：日内批量，以及回测要用的多月历史。

免费档对分钟线是硬拒的（`PermissionError: 免费服务仅提供日K数据和标的信息查询`），
所以此前分钟线只能走新浪 `stock_zh_a_minute`——一只票一次 HTTP 往返，实测串行
1.0 秒/只，8 线程 0.19 秒/只，而且单次最多 1970 根（1 分钟线约 8 个交易日）。

付费档给的是**批量**接口，两件事因此变得可行：

1. **盘中监控**。`klines.batch(symbols, period='1m')` 一次请求拿回一批标的的分钟
   线，不再是 N 次往返。监控循环的节奏此前是由新浪的往返次数决定的。
2. **回测**。1 分钟线能回溯到 **2025-05-23**（实测数据底），而新浪只有 8 天。
   这是这个仓最常被写进文档的那句「未经回测」第一次具备解除条件。

## 两个必须记住的边界

**单次 5000 根是硬顶。** `count` 传 5000 / 10000 / 20000，甚至配上
`start_time`，返回的都是 5000 根——服务端给的是 `end_time` 之前最近的 5000 根，
`count` 调大和把窗口拉宽都不起作用。要拿更早的数据，只能**把 `end_time` 逐段
往前推**，见 `fetch_minute_history`。实测 600519.SH 翻 14 页 6.4 秒拿到 7 万根。

**`intraday_batch` 在 Pro 档没有权限**（`无日内分时查询批量查询权限`），单只的
`klines.intraday()` 可以用。但本模块统一走 `klines.batch`——它本来就是批量的，
且同样覆盖当日实时，没有理由为了日内单独走一条需要更高档位的路。

## 量纲

**`volume` 是「手」，新浪那路是「股」，差 100 倍。** 这一条不是风格问题：
`signals/intraday.py` 的 `_vwap` 用 `amount / volume` 算成交均价，不换算的话
茅台的 VWAP 会算成 135083 而不是 1351。同一根 K 线实测：新浪 `volume=37000`、
TickFlow `volume=369`。所以本模块输出的 bar 一律换算成**股**，与新浪那路可以
直接互换。

## 两家的分钟线并不逐根相同

换算对齐之后，两条路仍然不是同一份数据。2026-08-02 拿重叠的 1970 根 1 分钟线
逐根对照：

| 标的 | 完全相同 | 相对差中位 | 相对差最大 |
|---|---|---|---|
| 000858.SZ | 776 / 1970（39.4%） | 1.3e-4 | 0.149% |
| 600519.SH | 993 / 1970（50.4%） | 0 | 0.312% |
| 000001.SZ | 1205 / 1970（61.2%） | 0 | 0.185% |

这是不同厂商对逐笔归并到分钟的边界处理差异，不是谁错了。但它意味着**换一条路
会让价位漂移最多约 0.3%**——所以别把两条路的读数混在同一次计算里，也别拿一条路
的历史去回测另一条路要跑的规则。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ashare_decision_support.numeric import finite

CN_TZ = timezone(timedelta(hours=8))

# 仓内沿用的周期写法（"1"/"5"/…）到 TickFlow 写法（"1m"/"5m"/…）。
PERIOD_SUFFIX = "m"
SUPPORTED_PERIODS: tuple[str, ...] = ("1", "5", "15", "30", "60")

# 服务端单次返回上限，实测钉死在这个数上，调大 `count` 无效。
MAX_BARS_PER_REQUEST = 5000

# 每次批量请求的标的数。SDK 内部还会再分批，这里只是不把一个 URL 撑爆。
SYMBOL_BATCH = 100

# `fetch_minute_history` 的翻页上限。14 页就能覆盖 600519 的全部 1m 历史，
# 32 页是留给成交更密集的标的的余量，同时防翻页失控。
MAX_HISTORY_PAGES = 32

# 手 → 股。见模块头「量纲」。
LOTS_TO_SHARES = 100.0

# 分钟线一律取**不复权**。
#
# SDK 默认是 `forward`（比例前复权），不是不复权——实测五粮液 21 天 1 分钟线窗口
# 内有除权，最早一根前复权 71.1339、不复权 73.6200，**差 3.4%**。三个理由必须
# 显式指定：
#
# 1. `advise` 把这些价格当**挂单价**发出去，挂单价只有原始价一种口径；
# 2. 免费路径（新浪 `adjust=""`）给的就是原始价。两条路对同一件事给不同的数，
#    是这类系统里最难查的一种错；
# 3. 简报里那句「价格为**不复权**实际成交价」得是真的。
#
# 回测要跨除权比较收益率时应当另取复权序列，`fetch_minute_history` 因此把
# `adjust` 开成了参数。
ADJUST_RAW = "none"


class TickFlowIntradayAdapter:
    """分钟线，接口与 `SinaIntradayAdapter` 一致（`get_minute_bars`）。"""

    name = "tickflow-intraday"

    def __init__(self, client: Any, default_bars: int = 1200, adjust: str = ADJUST_RAW) -> None:
        self.client = client
        # 新浪那路一次给 1970 根，取相近量级，好让两条路径的下游行为一致。
        self.default_bars = default_bars
        self.adjust = adjust

    def get_minute_bars(
        self,
        symbols: list[str],
        period: str = "5",
        max_bars: int | None = None,
        max_workers: int | None = None,
    ) -> tuple[dict[str, list[dict]], list[str]]:
        if period not in SUPPORTED_PERIODS:
            raise ValueError(f"unsupported period {period!r}; expected one of {SUPPORTED_PERIODS}")
        if not symbols:
            return {}, []

        count = min(max_bars or self.default_bars, MAX_BARS_PER_REQUEST)
        result: dict[str, list[dict]] = {}
        failed: list[str] = []

        for start in range(0, len(symbols), SYMBOL_BATCH):
            chunk = symbols[start : start + SYMBOL_BATCH]
            try:
                payload = self.client.klines.batch(
                    symbols=chunk,
                    period=f"{period}{PERIOD_SUFFIX}",
                    count=count,
                    adjust=self.adjust,
                )
            except Exception:
                failed.extend(chunk)
                continue
            if not isinstance(payload, dict):
                failed.extend(chunk)
                continue
            for symbol in chunk:
                bars = to_bars(payload.get(symbol), symbol)
                if bars:
                    result[symbol] = bars[-max_bars:] if max_bars is not None else bars
                else:
                    failed.append(symbol)

        notes = [f"以下标的分钟线拉取失败：{'、'.join(failed[:8])}"] if failed else []
        return result, notes

    def health_check(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            bars, _ = self.get_minute_bars(["600519.SH"], period="1", max_bars=5)
            ok, error = bool(bars), None
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        return {
            "source": self.name,
            "mode": "paid",
            "reachable": ok,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "capabilities": {
                "supports_minute": True,
                "supports_tick": False,
                "supports_orderbook": False,
                "raw_prices": False,          # 分钟线默认口径同日线，见下方说明
            },
            "periods": list(SUPPORTED_PERIODS),
            "max_bars_per_request": MAX_BARS_PER_REQUEST,
            "error": error,
        }


# ---- 历史回溯 -------------------------------------------------------------


def fetch_minute_history(
    client: Any,
    symbol: str,
    period: str = "1",
    start: date | None = None,
    end: date | None = None,
    max_pages: int = MAX_HISTORY_PAGES,
    adjust: str = ADJUST_RAW,
) -> tuple[list[dict], list[str]]:
    """把 `symbol` 的分钟线尽量往前拉，直到够到 `start` 或者没有更早的数据。

    **为什么要翻页**：单次请求硬顶 5000 根，且 `count` 调大、`start_time` 放宽
    都不管用——服务端返回的永远是 `end_time` 之前最近的 5000 根。所以唯一的办法
    是拿到一页之后，把 `end_time` 挪到这页最早一根的前一分钟，再要下一页。

    实测 600519.SH：14 页 / 6.4 秒 / 7 万根，回溯到 **2025-05-23**——那就是数据
    底，再往前翻返回的还是同一批，所以「这一页没有新数据」是终止条件之一。

    返回的 bar 与 `get_minute_bars` 同构，按时间升序，跨页去重。

    `adjust` 默认不复权（挂单价口径）。要跨除权比较收益率就传 `"forward"`——
    实测五粮液这两种口径在 21 天窗口内差 3.4%，训练与推理必须用同一种。
    """
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported period {period!r}; expected one of {SUPPORTED_PERIODS}")

    floor_ms = _day_start_ms(start) if start else None
    cursor_ms = _day_end_ms(end) if end else None
    notes: list[str] = []

    seen: dict[int, dict] = {}
    pages = 0
    for _ in range(max(1, max_pages)):
        kwargs: dict[str, Any] = {
            "symbols": [symbol],
            "period": f"{period}{PERIOD_SUFFIX}",
            "count": MAX_BARS_PER_REQUEST,
            "adjust": adjust,
        }
        if cursor_ms is not None:
            kwargs["end_time"] = cursor_ms
        try:
            payload = client.klines.batch(**kwargs)
        except Exception as exc:
            notes.append(f"{symbol} 分钟历史第 {pages + 1} 页取数失败（{type(exc).__name__}），已取到的部分照用")
            break
        pages += 1

        compact = payload.get(symbol) if isinstance(payload, dict) else None
        stamps = (compact or {}).get("timestamp") or []
        if not stamps:
            break

        fresh = 0
        for bar in to_bars(compact, symbol):
            key = int(bar["timestamp"].timestamp() * 1000)
            if key not in seen:
                seen[key] = bar
                fresh += 1
        if not fresh:
            # 已经到数据底了：再往前翻拿回来的还是同一批。
            break

        oldest = min(int(value) for value in stamps)
        if floor_ms is not None and oldest <= floor_ms:
            break
        cursor_ms = oldest - 60_000
    else:
        # 翻满了还没触底：说出来，别让「只回溯到这里」看起来像「就这么多数据」。
        reached = _fmt(min(seen)) if seen else "无"
        notes.append(
            f"{symbol} 分钟历史翻满 {max_pages} 页仍未触底，本次只回溯到 {reached}；"
            "要更早的数据请加大 max_pages"
        )

    bars = [seen[key] for key in sorted(seen)]
    if floor_ms is not None:
        bars = [bar for bar in bars if int(bar["timestamp"].timestamp() * 1000) >= floor_ms]
    if bars:
        notes.append(
            f"{symbol} {period} 分钟线 {len(bars)} 根（{pages} 页），"
            f"{_fmt_dt(bars[0]['timestamp'])} → {_fmt_dt(bars[-1]['timestamp'])}"
        )
    else:
        notes.append(f"{symbol} 未取到任何分钟线")
    return bars, notes


# ---- parsing -------------------------------------------------------------


def to_bars(compact: Any, symbol: str) -> list[dict]:
    """TickFlow 的**列式**响应 → 仓内通用的行式 bar。

    注意返回的是 `{'timestamp': [...], 'open': [...], ...}` 这种按字段分列的结构，
    和日线接口的行式不一样，直接当行处理会得到一条巨大的假记录。
    """
    if not isinstance(compact, dict):
        return []
    stamps = compact.get("timestamp")
    closes = compact.get("close")
    if not isinstance(stamps, list) or not isinstance(closes, list):
        return []

    opens = compact.get("open") if isinstance(compact.get("open"), list) else []
    highs = compact.get("high") if isinstance(compact.get("high"), list) else []
    lows = compact.get("low") if isinstance(compact.get("low"), list) else []
    volumes = compact.get("volume") if isinstance(compact.get("volume"), list) else []
    amounts = compact.get("amount") if isinstance(compact.get("amount"), list) else []

    bars: list[dict] = []
    for index, stamp in enumerate(stamps):
        close = _at(closes, index)
        when = _to_dt(stamp)
        if close is None or when is None:
            continue
        open_price = _at(opens, index)
        volume_lots = _at(volumes, index)
        bars.append(
            {
                "symbol": symbol,
                "timestamp": when,
                "open": _px(close if open_price is None else open_price),
                "high": _px(_at(highs, index)),
                "low": _px(_at(lows, index)),
                "close": _px(close),
                # 手 → 股。不换算 `_vwap` 会把 amount/volume 算大 100 倍。
                "volume": None if volume_lots is None else volume_lots * LOTS_TO_SHARES,
                "amount": _at(amounts, index),
            }
        )
    return bars


def _px(value: float | None) -> float | None:
    """价格取到 4 位小数。

    **这不是美观问题。** 复权后的 OHLC 带浮点尾巴，而四个字段是各自算出来的，
    尾巴方向不一致：实测 60 只 / 15000 根 1 分钟线里有 **1399 根（9.33%）满足
    `high < low`**（倒挂量级 1e-13，例如 high=1350.599999999988 而
    low=1350.6000000000106）。下游把 `high - low` 当日内振幅、把
    `low == min(lows)` 当枢轴点判据，倒挂会让这些判断静默出错。取到 4 位之后
    倒挂根数归零，而 A 股最小报价单位是 0.01，4 位小数远够用。
    """
    return None if value is None else round(value, 4)


def _at(values: list, index: int) -> float | None:
    """列式载荷按下标取一个**有限**读数，取不到返回 None。

    走 `finite` 而不是 `try/float`：后者对 NaN 是**成功**的（`float(nan)` 不抛），
    于是 NaN 会一路进到分钟线的 OHLCV 里。`_px` 的 `round(nan, 4)` 还是 NaN，
    `volume_lots * LOTS_TO_SHARES` 也还是 NaN，而 `signals/intraday.py` 里
    `float(bar.get("volume") or 0.0)` 收得下它——NaN 是真值，所以连那个 `or 0.0`
    的兜底都不会触发，当日累计成交量整个变成 NaN。
    """
    return finite(values[index]) if index < len(values) else None


def _to_dt(value: Any) -> datetime | None:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    # 交易所本地时间，与新浪那路保持一致的 naive datetime。
    return datetime.fromtimestamp(stamp / 1000.0, CN_TZ).replace(tzinfo=None)


def _day_start_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=CN_TZ).timestamp() * 1000)


def _day_end_ms(day: date) -> int:
    return _day_start_ms(day) + 86_400_000 - 1


def _fmt(stamp_ms: int) -> str:
    return _fmt_dt(datetime.fromtimestamp(stamp_ms / 1000.0, CN_TZ).replace(tzinfo=None))


def _fmt_dt(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


# ---- 选路 -----------------------------------------------------------------


def build_intraday_adapter(data_adapter: Any = None) -> Any | None:
    """付费档走 TickFlow 批量，否则退回新浪。取不到任何一路时返回 ``None``。

    放在这里而不是各个 job 里，是因为「什么时候该用付费档」只应该有一个答案。
    判据用 `supports_intraday` 而不是「有没有 key」——档位是服务端说了算的，
    适配器连上之后自己知道，猜没有意义。
    """
    client = getattr(data_adapter, "client", None)
    if client is not None and getattr(data_adapter, "supports_intraday", False):
        return TickFlowIntradayAdapter(client)
    try:
        from ashare_decision_support.adapters.sina_intraday import SinaIntradayAdapter
    except Exception:
        return None
    try:
        return SinaIntradayAdapter()
    except Exception:
        # akshare 没装。分钟线缺了链路仍然能跑（只出日线结构判断），所以不抛。
        return None
