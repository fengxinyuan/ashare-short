"""跑批链路的两处**可选数据源取数**：涨停池与逐笔资金。

`morning`（盘前）与 `post_close`（盘后）各要这两样东西，此前各写了一份——
两份 `_trade_flow` 34 行、两份 `_limit_pool` 15 行，**除了 note 的措辞之外逐字
相同**。

值得收口的不是行数，是这两个函数里的**失败处置**。它们是可选源，取数失败必须
是「这次没有这一项」而不是「这轮跑批没了」，于是每一份里都埋着同一串判断：

```text
开关关了      → 记一条 note，返回空，链路照跑
适配器是 None → 返回空（连默认适配器都造不出来，比如可选依赖没装）
没有 getter   → 记一条 note，返回空（适配器版本对不上）
取数抛异常    → 记一条 note，返回空
```

四条里漏掉任何一条，症状都不是报错，是**一次没有资金旁证的跑批**——而且盘前和
盘后会漏得不一样。这正是把它收成一份的理由。

**措辞留给调用方。** 盘前那条 note 要提 `--no-trade-flow`（用户是在命令行上关掉
它的），盘后那条要说「本次盘后卡」。共用的是取数与兜底的顺序，不是话术，同
`sectors/panel.py` 开头的那条纪律。
"""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.broad_index import default_broad_index_adapter
from ashare_decision_support.adapters.earnings import default_earnings_adapter
from ashare_decision_support.adapters.lhb_seats import LhbSeatAdapter
from ashare_decision_support.adapters.limit_pool import LimitPoolAdapter
from ashare_decision_support.adapters.market_scan import MarketScanAdapter
from ashare_decision_support.adapters.tencent_detail import default_trade_flow_adapter
from ashare_decision_support.adapters.ths_sector_index import ThsSectorIndexAdapter
from ashare_decision_support.adapters.tickflow_market import (
    CompositeMarketScanAdapter,
    TickFlowMarketScanAdapter,
)
from ashare_decision_support.fanout import gather
from ashare_decision_support.news import (
    AkshareNewsSource,
    CompositeNewsSource,
    NewsCache,
    OfficialNewsSource,
    RssNewsSource,
    WallstreetNewsSource,
)


def fetch_limit_pool(
    adapter: Any | None,
    today: date,
    *,
    enabled: bool,
    notes: list[str],
    off_note: str,
) -> Any:
    """涨停池快照。关掉、取不到，都返回 None，链路继续。

    快照自己的 `notes` 与 `describe()` 一并倒进来：涨停池是情绪周期判定的唯一
    输入，「今天连板高度几何、炸板率多少」这几句话必须跟着数据一起进简报，
    否则读的人看到一个仓位上限却看不到它是从哪来的。
    """
    if not enabled:
        notes.append(off_note)
        return None
    source = adapter if adapter is not None else LimitPoolAdapter()
    try:
        snapshot = source.fetch_recent(today)
    except Exception as exc:
        notes.append(f"涨停池不可用（{type(exc).__name__}），本次无连板与炸板读数")
        return None
    notes.extend(snapshot.notes)
    if snapshot.ok:
        notes.extend(snapshot.describe())
    return snapshot


def fetch_trade_flow(
    adapter: Any | None,
    symbols: list[str],
    *,
    enabled: bool,
    notes: list[str],
    off_note: str,
    subject: str,
) -> dict[str, dict]:
    """候选/持仓的逐笔资金旁证，`{symbol: 摘要}`。取不到就是空字典。

    **数据是上一个交易日的整段成交**，盘前跑正好对得上；盘中跑拿到的是当日截至
    此刻的半段，那时「尾盘净额」这类读数根本没发生——所以摘要里带着交易日，
    由读的人判断时点对不对，这一层不替他判断。

    `subject` 只进「取不到」那条 note（「候选行无资金旁证」/「本次盘后卡无…」）。
    """
    if not enabled:
        notes.append(off_note)
        return {}
    if not symbols:
        return {}
    source = adapter if adapter is not None else default_trade_flow_adapter()
    if source is None:
        return {}
    getter = getattr(source, "get_trade_flow", None)
    if not callable(getter):
        notes.append("逐笔资金适配器缺少 get_trade_flow，已跳过")
        return {}
    try:
        flows, extra = getter(symbols)
    except Exception as exc:
        notes.append(f"逐笔资金不可用（{type(exc).__name__}），{subject}无资金旁证")
        return {}
    notes.extend(extra or [])
    # 适配器可能交出 dataclass，也可能已经是 dict。两种都收，其余形状**丢掉**
    # 而不是原样透传：这份摘要要整个序列化进简报与台账，放一个不认识的对象进去，
    # 出错的地方会在很远的 json.dumps 那里。
    mapped: dict[str, dict] = {}
    for symbol, flow in (flows or {}).items():
        if hasattr(flow, "to_dict"):
            mapped[str(symbol)] = flow.to_dict()
        elif isinstance(flow, dict):
            mapped[str(symbol)] = dict(flow)
    return mapped


def fetch_lhb_seats(
    adapter: Any | None,
    symbols: list[str],
    trade_date: date,
    *,
    enabled: bool,
    notes: list[str],
) -> dict[str, dict]:
    """龙虎榜席位是盘后披露事实，不可当作盘中资金流。

    **逐标的一次 HTTP，所以并发。** 实测每只 0.45s，而盘后要看的是几十只——串行
    要花十几秒，且这几十次请求彼此毫无依赖。`gather` 保序，所以 `mapped` 的插入
    顺序仍由 `symbols` 决定，不受完成顺序影响；下游按 symbol 取值不受影响，但
    note 里那句「x/y 只上榜」在两次跑之间必须是同一个顺序才好比对。
    """
    if not enabled or not symbols:
        return {}
    source = adapter if adapter is not None else LhbSeatAdapter()
    getter = getattr(source, "fetch", None)
    if not callable(getter):
        notes.append("龙虎榜席位适配器缺少 fetch，已跳过")
        return {}

    done, failed = gather(symbols, lambda symbol: getter(symbol, trade_date))
    for symbol, exc in failed:
        notes.append(f"龙虎榜席位 {symbol} 不可用（{type(exc).__name__}）")

    mapped: dict[str, dict] = {}
    for symbol, read in done:
        payload = read.to_dict() if hasattr(read, "to_dict") else read if isinstance(read, dict) else None
        if isinstance(payload, dict) and payload.get("covered"):
            mapped[symbol] = dict(payload)
    if mapped:
        notes.append(
            f"龙虎榜席位：{len(mapped)}/{len(symbols)} 只在 {trade_date} 上榜；"
            "为盘后披露事实，不代表盘中实时资金方向"
        )
    return mapped


# ---- 盘前独有的六路可选源 --------------------------------------------------
#
# 上面三个是 `morning` 与 `post_close` 共用的，下面六个只有 `morning` 用。放在
# 同一个模块里而不是留在 `jobs/morning.py`，理由是**它们是同一个形状**：开关关了
# / 适配器造不出来 / 适配器版本对不上 / 取数抛异常，四条路径都必须落到「这次没有
# 这一项」而不是「这轮跑批没了」。
#
# 混在主链路里时，这个形状看不出来——六份各自散在两百行判定逻辑中间，谁也不会
# 发现第七份漏写了一条。收在一起之后，加第七路源时照着抄就是对的。
def fetch_broad_index(adapter: Any | None, enabled: bool, notes: list[str]) -> Any:
    """宽基指数快照。取不到就没有市值加权口径，等权那一路照跑。

    这一层唯一的失败模式是「静默地没有」——没有它时简报读起来完全正常，只是
    少了一个方向性维度。所以关掉与取不到都必须留一句话。
    """
    if not enabled:
        notes.append("已关闭宽基指数读数（--no-broad-index），本次无市值加权口径对照")
        return None
    source = adapter if adapter is not None else default_broad_index_adapter()
    if source is None:
        return None
    try:
        read = source.fetch()
    except Exception as exc:
        notes.append(f"宽基指数不可用（{type(exc).__name__}），本次无市值加权口径对照")
        return None
    notes.extend(read.notes)
    if not read.ok:
        notes.append("宽基指数没有可用读数，本次无市值加权口径对照")
        return None
    return read


def fetch_market_scan(
    adapter: Any | None, enabled: bool, notes: list[str], data_adapter: Any = None
) -> Any:
    """全市场扫描。取不到就退化成「没有可交易性过滤」，链路照跑。

    这一层失败必须是**放行**而不是拦截：拿不到行情去否决标的，等于让数据缺失
    冒充风险判断。`LiquidityScreen` 在 scan 为 None 时全部放行，并把「未经过滤」
    写进 note。
    """
    if not enabled:
        notes.append("已关闭全市场扫描（--no-market-scan），本次无可交易性过滤")
        return None
    source = adapter if adapter is not None else default_market_scan(data_adapter)
    try:
        scan = source.fetch()
    except Exception as exc:
        notes.append(f"全市场扫描不可用（{type(exc).__name__}），本次无可交易性过滤")
        return None
    notes.extend(scan.notes)
    if scan.ok:
        notes.extend(scan.describe())
    return scan


def default_market_scan(data_adapter: Any) -> Any:
    """付费档就两路合并，免费档只有新浪一路。

    合并而非替换：TickFlow 更快、带流通股本和真实涨跌停价，但 PE/PB 要
    `financials.*`（Pro 档报「无公司财务数据查询权限」），那两个字段只有新浪有。
    两路 2026-08-02 实测逐字段吻合到新浪的取整精度，可以互为校验。
    """
    sina = MarketScanAdapter()
    client = getattr(data_adapter, "client", None)
    if client is None or not getattr(data_adapter, "supports_realtime", False):
        return sina
    return CompositeMarketScanAdapter(TickFlowMarketScanAdapter(client), sina)


def fetch_sector_index(adapter: Any | None, output_path: Path, enabled: bool, notes: list[str]) -> Any:
    """同花顺行业指数，独立于申万聚合的第二套板块读数。

    刻意**不参与打分**：同花顺 90 个行业 ≈ 申万二级粒度，硬映到申万一级 31 个
    只会造出一个看起来精确的错数。它并列呈现，由人（或写稿层）去读两套读数的
    分歧——指数涨而宽度弱，说的就是权重股在拉抬。
    """
    if not enabled:
        notes.append("已关闭同花顺行业指数读数（--no-sector-index）")
        return None
    source = adapter if adapter is not None else ThsSectorIndexAdapter(cache_dir=output_path)
    try:
        snapshot = source.fetch()
    except Exception as exc:
        notes.append(f"同花顺行业指数不可用（{type(exc).__name__}），本次无板块日内位置读数")
        return None
    notes.extend(snapshot.notes)
    if snapshot.ok:
        notes.extend(snapshot.describe())
    return snapshot


def default_news_source(output_path: Path) -> Any | None:
    """商业快讯 + 海外宏观 + 国内官方政策与监管发布。

    分开列而不是合成一个源：akshare 那三路覆盖盘面与政策文件，华尔街见闻补的是
    **数据发布**（PMI/CPI/社融）与海外流动性。实测 2026-08-02 全市场 200 条里
    「PMI/统计局/制造业/美债/降准」命中全为 0——不是权重低，是压根没进来。

    四路共用同一个 `NewsCache`：缓存按「交易日 + 源自己的键」分目录，四路的键互不
    相同，所以共用一个实例只是共用一个根目录。此前只有 akshare 那一路收 cache，
    另外三路每次重跑都重新打网络——而盘前本来就会跑好几次，拿到的是几批不同的
    快讯流，板块舆情分也跟着变，简报上还看不出来是哪一次。
    """
    cache = NewsCache(output_path)
    sources: list[Any] = []
    with contextlib.suppress(RuntimeError):
        sources.append(AkshareNewsSource(cache=cache))
    sources.append(WallstreetNewsSource(cache=cache))
    sources.append(OfficialNewsSource(cache=cache))
    sources.append(RssNewsSource(cache=cache))
    return CompositeNewsSource(*sources) if sources else None


def fetch_earnings(adapter: Any | None, today: date, enabled: bool, notes: list[str]) -> Any:
    """业绩预告/快报。取不到就退化成「没有业绩过滤」，链路照跑。

    和 `fetch_market_scan` 完全同一条纪律：这一层失败必须是**放行**而不是拦截。
    拿不到业绩去否决标的，等于让数据缺失冒充风险判断。
    """
    if not enabled:
        notes.append("已关闭业绩读数（--no-earnings），本次无业绩否决与业绩催化标注")
        return None
    source = adapter if adapter is not None else default_earnings_adapter()
    if source is None:
        notes.append("业绩数据源不可用，本次无业绩否决与业绩催化标注")
        return None
    try:
        snapshot = source.fetch(today)
    except Exception as exc:
        notes.append(f"业绩预告不可用（{type(exc).__name__}），本次无业绩否决")
        return None
    notes.extend(snapshot.notes)
    if snapshot.ok:
        notes.extend(snapshot.describe())
    return snapshot
