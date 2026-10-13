"""盘前配置、外部通道和各阶段共享的交接数据。

阶段依赖通过字段显式传递；配置值与数据源对象分别放在 MorningConfig 和 Sources。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.journal import Journal
from ashare_decision_support.journal.records import DEFAULT_STRATEGY_VERSION
from ashare_decision_support.news import DailyRead, MacroDigest
from ashare_decision_support.news.llm_classifier import ClassifyResult
from ashare_decision_support.notify.base import Notifier
from ashare_decision_support.scoring import ScoreProvider
from ashare_decision_support.strategy.regime import RegimeVerdict
from ashare_decision_support.writer.base import BriefWriter


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
    # 外盘 → A 股板块的映射读数（`sectors/overseas_link.OverseasRead`）。**不进板块分**，
    # 只挂到候选行与台账：它说的是「昨夜费半跌了 2%，半导体候选今天多一条风险」，
    # 不是「半导体板块少 3 分」。没取到时为 None。
    overseas: Any = None


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
    """盘前调参值与取数开关；不持有数据源对象。"""

    top_sectors: int = 5
    # 行业与概念分别排名和取名额，避免数量更多的概念挤占行业名额。
    top_concepts: int = 3
    candidates_per_sector: int = 3
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
    force_refresh_daily: bool = False
    # 概念与行业分别排序，由各自的配额选取。
    use_concepts: bool = True
    strategy_version: str = DEFAULT_STRATEGY_VERSION


@dataclass(slots=True)
class Sources:
    """盘前使用的行情、舆情、评分、写稿、通知和台账通道。

    行情适配器必填，其余通道使用注入值或任务默认值。取数开关独立于适配器，
    由 MorningConfig.use_* 显式禁用。"""

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
    # 海外快照源（`adapters/global_market.GlobalMarketAdapter`）。**没有默认实现**：
    # 其余取数器缺省时会自己建默认适配器，这一个不建——测试与 chat 上下文里的
    # 晨报不该为了一段旁证去打 40 多个海外行情代码。CLI 与 daemon 显式传入。
    global_market_adapter: Any | None = None
    trade_flow_adapter: Any | None = None
    industry_taxonomy: Any | None = None
    concept_taxonomy: Any | None = None
