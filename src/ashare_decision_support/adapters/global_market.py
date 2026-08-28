"""盘前海外市场快照，按“主源批量取数、备用源可插拔、缺失可见”设计。

新浪行情是默认主源：一次请求覆盖美股指数、**美股行业与主题 ETF**、商品期货、
汇率及韩国指数，已在本机出口实测。Yahoo Finance 不作为默认调用：它在当前出口
返回 429，只保留为可显式替换的备用接口，避免一个受限端点拖慢整份盘前报告。

## 为什么品种表可以铺这么宽

`_fetch_sina` 把所有代码拼成**一个** `list=` 请求，所以品种数不影响网络往返次数
——45 个品种和 9 个品种同样是一次 HTTP。铺宽的成本只在响应体大小上，而收益是
**外盘映射的粒度**：只有指数时，能说的最细一句话是「隔夜美股偏弱」；有了行业与
主题 ETF 之后，说得出「太阳能 ETF +1.2%、锂价指数 +0.7%、铜矿 +3.2%」——
这三条各自对得上 A 股的一个真实板块，而「美股偏弱」对不上任何一个。

## 品种的取舍标准：有没有一条说得出口的 A 股映射

宽不等于全。收进来的每一个品种都要能回答「它动了，A 股哪个板块该被多看一眼」，
映射表本身在 `sectors/overseas_link.py`，不在这里——**这一层只负责把数取回来，
不负责说它意味着什么**。所以本模块没有任何方向判断，只有读数与分组。

分组（`group`）的用途是呈现与聚合：45 行读数平铺进简报没人看得完，按组折叠之后
「指数说风险偏好、板块说方向、商品说成本与资源品」是三句各自成立的话。
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

from ashare_decision_support.adapters import http
from ashare_decision_support.numeric import finite

# 新浪那一路打的是 `hq.sinajs.cn/list=`，和 `snapshot`（个股快照）、`broad_index`
# （宽基指数）**同一个端点**——地址与 `Referer` 因此取 `adapters/http.py` 里的那
# 一份，不在这里再写一遍。整包按模块名引用（`http.fetch_text`）而不是 `from ...
# import fetch_text`：构造函数已经有一个叫 `fetch_text` 的注入参数占着这个名字。
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
TIMEOUT_SECONDS = 10

# 分组。呈现与聚合用，不参与任何判断。
GROUP_INDEX = "index"          # 宽基与风险偏好
GROUP_US_SECTOR = "us_sector"  # 美股行业与主题 ETF
GROUP_COMMODITY = "commodity"  # 商品与期货
GROUP_FX = "fx"                # 汇率

# 品种表：key -> (新浪代码, 中文名, 解析口径, 分组)。
#
# **解析口径（第三项）决定读哪几个字段**，三种布局的字段数分别是 30/36（gb_）、
# 13（b_）、若干（hf_/fx_），见 `_sina_reading`。口径填错不会报错，只会让前收
# 和时间戳静默取到别的字段——这个坑真实发生过，判据记在 `_sina_reading` 上。
SINA_INSTRUMENTS: dict[str, tuple[str, str, str, str]] = {
    # ---- 指数：风险偏好与直接映射 ----
    "sp500": ("gb_inx", "标普500", "equity", GROUP_INDEX),
    "nasdaq": ("gb_ixic", "纳斯达克", "equity", GROUP_INDEX),
    "dow": ("gb_dji", "道琼斯", "equity", GROUP_INDEX),
    # 费半是外盘映射里**最硬的一条**：A 股半导体与消费电子跟它的同步性高于跟纳指。
    "sox": ("gb_sox", "费城半导体", "equity", GROUP_INDEX),
    # 韩股放在指数组而不是板块组：KOSPI/KOSDAQ 是**整个市场**，只是它的权重高度
    # 集中在存储与电子，所以它同时兼具「亚洲风险偏好」和「存储产业链」两重身份。
    "kospi": ("b_KOSPI", "KOSPI", "index", GROUP_INDEX),
    "kosdaq": ("b_KOSDAQ", "KOSDAQ", "index", GROUP_INDEX),
    # 台指与日经的 key 与 `adapters/overseas_history.py` 的历史表**逐字一致**。
    # 两张表分开是对的（这张取实时快照走 `hq.sinajs.cn`，那张取历史日线走另外
    # 两个端点，symbol 格式都不同），但 key 必须对得上——基准率层要拿今天的读数
    # 去查历史分布，key 对不上的话样本数恒为 0 而且不报错。
    # `tests/test_overseas_link.py` 有一条断言把两张表的交集校一遍。
    "nikkei": ("b_NKY", "日经225", "index", GROUP_INDEX),
    # 台积电在台湾加权里权重极高，且它收在北京 13:30——和费半是**同一条产业链的
    # 两个时段**，A 股当日午后就能看到台指，而费半要等次日凌晨。
    "taiex": ("b_TWJQ", "台湾加权", "index", GROUP_INDEX),
    # 中国资产在境外的定价。它不映射某个板块，映射的是**外资此刻怎么看中国**，
    # 而那件事在 A 股这边没有任何一个盘前读数回答得了。
    "fxi": ("gb_fxi", "富时中国大盘ETF", "equity", GROUP_INDEX),
    "kweb": ("gb_kweb", "中概网络股ETF", "equity", GROUP_INDEX),

    # ---- 美股行业与主题 ETF：这一组才是「板块级」映射的来源 ----
    "smh": ("gb_smh", "半导体ETF", "equity", GROUP_US_SECTOR),
    "xlk": ("gb_xlk", "科技ETF", "equity", GROUP_US_SECTOR),
    # 主题 ETF 比宽行业 ETF 更值钱：`tan`/`lit`/`botz`/`ura` 各自对得上 A 股一个
    # **概念板块**（光伏/锂电/机器人/核电），而概念才是超短真正轮动的单位。
    "tan": ("gb_tan", "太阳能ETF", "equity", GROUP_US_SECTOR),
    "lit": ("gb_lit", "锂价指数ETF", "equity", GROUP_US_SECTOR),
    "botz": ("gb_botz", "机器人与AIETF", "equity", GROUP_US_SECTOR),
    "ura": ("gb_ura", "铀业股ETF", "equity", GROUP_US_SECTOR),
    "copx": ("gb_copx", "铜矿ETF", "equity", GROUP_US_SECTOR),
    "gdx": ("gb_gdx", "金矿指数ETF", "equity", GROUP_US_SECTOR),
    "xme": ("gb_xme", "金属与采矿ETF", "equity", GROUP_US_SECTOR),
    "ibb": ("gb_ibb", "生物科技ETF", "equity", GROUP_US_SECTOR),
    "xlv": ("gb_xlv", "医疗保健ETF", "equity", GROUP_US_SECTOR),
    "xle": ("gb_xle", "能源ETF", "equity", GROUP_US_SECTOR),
    "xop": ("gb_xop", "油气开采ETF", "equity", GROUP_US_SECTOR),
    "jets": ("gb_jets", "航空ETF", "equity", GROUP_US_SECTOR),
    "moo": ("gb_moo", "农业ETF", "equity", GROUP_US_SECTOR),
    "xlb": ("gb_xlb", "材料ETF", "equity", GROUP_US_SECTOR),
    "xli": ("gb_xli", "工业ETF", "equity", GROUP_US_SECTOR),
    "xlf": ("gb_xlf", "金融ETF", "equity", GROUP_US_SECTOR),

    # ---- 商品与期货：成本端与资源品 ----
    "wti": ("hf_CL", "WTI原油", "future", GROUP_COMMODITY),
    "brent": ("hf_OIL", "布伦特原油", "future", GROUP_COMMODITY),
    "natgas": ("hf_NG", "天然气", "future", GROUP_COMMODITY),
    "gold": ("hf_GC", "COMEX黄金", "future", GROUP_COMMODITY),
    "silver": ("hf_SI", "COMEX白银", "future", GROUP_COMMODITY),
    "copper": ("hf_HG", "COMEX铜", "future", GROUP_COMMODITY),
    # LME 那一组比 COMEX 更贴 A 股有色：国内有色的定价基准看伦敦，不看纽约。
    "lme_copper": ("hf_CAD", "伦铜", "future", GROUP_COMMODITY),
    "lme_alu": ("hf_AHD", "伦铝", "future", GROUP_COMMODITY),
    "lme_zinc": ("hf_ZSD", "伦锌", "future", GROUP_COMMODITY),
    "lme_nickel": ("hf_NID", "伦镍", "future", GROUP_COMMODITY),
    "lme_tin": ("hf_SND", "伦锡", "future", GROUP_COMMODITY),
    "lme_lead": ("hf_PBD", "伦铅", "future", GROUP_COMMODITY),
    "corn": ("hf_C", "CBOT玉米", "future", GROUP_COMMODITY),
    "wheat": ("hf_W", "CBOT小麦", "future", GROUP_COMMODITY),
    "cotton": ("hf_CT", "ICE棉花", "future", GROUP_COMMODITY),

    # ---- 汇率 ----
    "usdcny": ("fx_susdcny", "美元/人民币", "fx", GROUP_FX),
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
    group: str = GROUP_INDEX

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GlobalMarketSnapshot:
    readings: list[GlobalMarketReading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(item.last is not None for item in self.readings)

    def by_group(self, group: str) -> list[GlobalMarketReading]:
        """一组里取到读数的那些，按涨跌幅从高到低。

        排序放在这里而不是各调用方各排一次：呈现层与映射层都要「谁动得最厉害」，
        两处各写一个 `sorted` 会在其中一处漏掉 `change_pct is None` 的过滤，而那
        一漏是 `TypeError`——在盘前链路上，为了排个序把整份快照带走。
        """
        return sorted(
            (item for item in self.readings if item.group == group and item.change_pct is not None),
            key=lambda item: item.change_pct or 0.0,
            reverse=True,
        )

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
        sina_instruments: dict[str, tuple[str, str, str, str]] | None = None,
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
            payload = _parse_sina(self.fetch_text(http.SINA_QUOTE_URL + codes))
        except Exception as exc:
            return (
                [
                    GlobalMarketReading(
                        key, name, symbol, "sina", note=f"不可用：{type(exc).__name__}", group=group
                    )
                    for key, (symbol, name, _, group) in self.sina_instruments.items()
                ],
                [f"新浪海外行情不可用（{type(exc).__name__}）"],
            )
        readings = []
        for key, (symbol, name, kind, group) in self.sina_instruments.items():
            try:
                readings.append(
                    _sina_reading(key, name, symbol, kind, payload.get(symbol, ""), group)
                )
            except Exception as exc:
                readings.append(
                    GlobalMarketReading(
                        key, name, symbol, "sina", note=f"不可用：{type(exc).__name__}", group=group
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


def _at(values: list[str], index: int) -> str | None:
    """越界返回 None 而不是抛。

    只给**次要读数**用（前收）。主读数缺失仍然抛，由 `_fetch_sina` 收成一条
    「这个品种不可用」——两者的区别是：取不到价格的品种不该出现在快照里，
    取不到前收的品种照样有用（`change_pct` 是上游直接给的，不靠前收算）。
    """
    return values[index] if 0 <= index < len(values) else None


def _sina_reading(
    key: str, name: str, symbol: str, kind: str, raw: str, group: str = GROUP_INDEX
) -> GlobalMarketReading:
    """按口径解析一行 `hq_str_*`。

    **前收的字段位置是 2026-08-20 实测更正的。** 三种布局的字段数不同（`gb_` 是
    30 或 36、`b_` 是 13），但前收的位置在各自布局内是固定的：

    ```text
    gb_（美股指数与 ETF）   [1]现价 [2]涨跌幅% [3]时间 [4]涨跌额 … [25]收盘时间戳 [26]前收
    b_ （韩国指数）         [1]现价 [2]涨跌额 [3]涨跌幅% [5]时间 [6]日期 [8]开盘 [9]前收
    ```

    改之前 `gb_` 取的是 `[25]`——那是 `'Aug 19 04:38PM EDT'` 这样一个**时间戳
    字符串**，`finite()` 判成 None，于是全部美股品种的 `previous_close` 恒为空；
    `b_` 取的是 `[8]`，那是**开盘价**不是前收（实测 KOSPI：last 6852.58 −
    change 381.41 = 6471.17 = `[9]`，而 `[8]` 是 6680.34）。

    两处都不会报错，也不会被 `ok` 拦下——`ok` 判的是 `last is not None`，而
    `change_pct` 是从 `[2]`/`[3]` 直接读的，不经过前收。所以症状只有一个：
    一个静默为空、一个静默偏移的字段。**测试夹具当时是照着解析器编的**
    （`gb_ixic` 的前收摆在第 25 位、`b_KOSPI` 的 `[8]=6365` 与 `6258−(−37)=6295`
    对不上），于是这两处从来没有被测到。现在的夹具改成实测抓包。
    """
    values = raw.split(",") if raw else []
    if not values:
        raise ValueError("empty quote")
    if kind == "equity":
        last, change_pct, as_of, previous = (
            finite(values[1]),
            finite(values[2]),
            values[3],
            finite(_at(values, 26)),
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
            finite(_at(values, 9)),
        )
    if last is None:
        raise ValueError("missing price")
    return GlobalMarketReading(
        key, name, symbol, "sina", last, previous, change_pct, as_of, group=group
    )


def _yahoo_url(symbol: str) -> str:
    return YAHOO_URL.format(symbol=urllib.parse.quote(symbol, safe=""))


def _fetch_text(url: str) -> str:
    """新浪那一路的默认取数。`gb18030`/`replace` 是这一路自己的取值：海外品种名
    里混着的个别非法字节，丢掉一个字符也比整份盘前快照取不到强。
    """
    return http.fetch_text(
        urllib.request.urlopen,
        url,
        timeout=TIMEOUT_SECONDS,
        referer=http.SINA_REFERER,
        encoding="gb18030",
        errors="replace",
    )


def _fetch_json(url: str) -> Any:
    # `strict` 而不是这一层的默认 `ignore`：后面紧跟着 `json.loads`，静默丢字节
    # 只会把「响应坏了」变成一个更难读的 JSONDecodeError。
    return json.loads(
        http.fetch_text(urllib.request.urlopen, url, timeout=TIMEOUT_SECONDS, errors="strict")
    )


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
