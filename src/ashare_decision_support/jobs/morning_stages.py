"""盘前主链路的调参项、外部通道，以及六个阶段之间的五张交接单。

单独成模块是拆分的**前提**：`morning.py` 只剩编排，四个判定阶段各自成文件，
而它们全都要读同一批交接单。放在任何一个阶段文件里，其余三个就得反过来 import
那个文件——五张单子会替代 `morning.py` 成为新的中心。

交接单**不是领域模型**：字段就是下一阶段真正读到的那些值。它们存在的意义是让
「第 3 段依赖第 2 段的哪些东西」变成签名里看得见的事——原来这是 530 行里靠 60
多个局部变量隐式串起来的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.journal import Journal
from ashare_decision_support.news import DailyRead, MacroDigest
from ashare_decision_support.news.llm_classifier import ClassifyResult
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.scoring import ScoreProvider
from ashare_decision_support.sectors import DEFAULT_LEVEL
from ashare_decision_support.strategy.regime import RegimeVerdict
from ashare_decision_support.writer.base import BriefWriter


# 下面五个 `_Stage*` 是**阶段之间的交接单**，不是领域模型：字段就是下一阶段真正
# 读到的那些值。它们存在的意义是让「第 3 段依赖第 2 段的哪些东西」变成签名里
# 看得见的事——原来这是 530 行里靠 60 多个局部变量隐式串起来的。
@dataclass(slots=True)
class Universe:
    """① 板块与成分股。"""

    definitions: list
    all_symbols: list[str]
    metadata: dict[str, dict[str, Any]]


@dataclass(slots=True)
class Collected:
    """② 并行采集的产物：舆情一路 + 板块动量一路。"""

    source: Any
    metrics: dict
    metric_bars: dict
    market_docs: list
    announcements: list
    net_inflow: dict
    market_source_counts: dict
    news_as_of: Any
    news_targets: list
    coverage: dict
    coverage_diagnostics: dict
    raw_doc_count: int
    # 语料库本体。带到融合那一段，是因为板块选完之后才知道该问它哪几个板块的
    # 跨日热度——而语料库是在采集这一段建的。
    corpus: Any


@dataclass(slots=True)
class Fusion:
    """③ 融合：舆情归因 → 大盘方向 + 板块排序 + 仓位闸门。"""

    labels: ClassifyResult
    daily_read: DailyRead
    macro_links: MacroDigest
    limit_snapshot: Any
    regime: RegimeVerdict
    sector_index: Any
    rotation_price: Any
    turnover_migration: Any
    seesaw: Any
    market_scan: Any
    earnings: Any
    outlooks: list
    market: Any
    broad_index: Any
    index_verdict: Any
    effective_factor: float
    # 仓位决策的**完整**过程，不只是最终值。台账要能分辨「系统就建议 0.5」与
    # 「系统建议 0.9、被周期上限压到 0.5」——两者的 `effective_factor` 相同，
    # 而它们对「仓位上限这条规则有没有救过我们」是相反的证据。
    sizing: Any
    selected: list
    rotation: Any
    theme_radar: list[Any]
    theme_validation: dict[str, Any]
    sector_forecast_validation: dict[str, Any]
    sector_news_memory: list[dict[str, Any]]


@dataclass(slots=True)
class Selection:
    """④ 板块内选股。"""

    candidates: list[dict]
    control: list[dict]
    earnings_verdicts: dict
    selected_sector_of: dict[str, str]
    funnel: dict[str, int]
    diagnostic: dict[str, Any]


@dataclass(slots=True)
class Ledger:
    """⑤ 台账。"""

    facts: dict
    state: Any
    recorded: int

@dataclass(slots=True)
class MorningConfig:
    """盘前主链路的调参项——全是**取值**，不含任何数据源对象。

    和 `Sources` 分开的理由：这里的字段改了只影响产出的宽窄（选几个板块、每个
    板块几只票、手工仓位压多少），而 `Sources` 的字段改了是**换掉取数通道**。
    两类东西原来混在一个 32 参数的签名里，调用方无从判断改哪个是安全的。
    """

    top_sectors: int = 5
    # 概念板块单独取几个。**和 `top_sectors` 分开数**，因为两层是分开排名的：
    # 合成一个名额会让 346 个概念把 31 个行业整体挤出去（概念数量多，出极值的
    # 机会也多），而行业回答的「这是什么生意」并不因此变得不重要。
    top_concepts: int = 3
    candidates_per_sector: int = 3
    sector_level: str = DEFAULT_LEVEL
    position_factor: float | None = None
    max_constituents: int | None = None

    # `use_*` 开关与 `Sources` 里同名的适配器是**一对**，语义不能合并：
    #   适配器 = None  → 「没给，按默认建一个」
    #   开关   = False → 「明确不要这一路」
    # 合成「给了就开、没给就关」会让「用默认源」变得无法表达。
    use_news: bool = True
    use_llm_news: bool = False
    use_limit_pool: bool = True
    use_sector_index: bool = True
    use_market_scan: bool = True
    use_earnings: bool = True
    use_broad_index: bool = True
    use_trade_flow: bool = True
    # 概念板块进不进盘前排序。盘中监控（`sectors/intraday.py`）从 2026-08-09 起
    # 就是行业+概念两层，盘前一直只有行业——同一天同一套分类，两条链路看到的
    # 板块集合不一样，这本身就是一处口径分叉。
    use_concepts: bool = True


@dataclass(slots=True)
class Sources:
    """盘前主链路的全部外部通道：行情、舆情、评分、写稿、推送、台账。

    `adapter` 是唯一必填项——没有行情源这条链路无从开始。其余全部可空，空的含义
    统一是「按默认建一个」；要真正关掉某一路，用 `MorningConfig.use_*`。
    """

    adapter: Any
    news_source: Any | None = None
    social_source: Any | None = None
    news_classifier: Any | None = None
    news_store: Any | None = None
    score_provider: ScoreProvider | None = None
    writer: BriefWriter | None = None
    notifiers: list[Notifier] | None = None
    debate: DebatePanel | None = None
    journal: Journal | None = None
    limit_pool_adapter: Any | None = None
    sector_index_adapter: Any | None = None
    market_scan_adapter: Any | None = None
    earnings_adapter: Any | None = None
    broad_index_adapter: Any | None = None
    trade_flow_adapter: Any | None = None
    industry_taxonomy: Any | None = None
    concept_taxonomy: Any | None = None
