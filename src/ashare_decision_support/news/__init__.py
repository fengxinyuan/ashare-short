"""Sentiment (舆情) collection and scoring."""

from ashare_decision_support.news.akshare_source import AkshareNewsSource
from ashare_decision_support.news.base import (
    NewsDoc,
    NewsSource,
    SentimentAnalyzer,
    SentimentScore,
    SymbolSentiment,
)
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.composite import CompositeNewsSource
from ashare_decision_support.news.daily_read import Chain, DailyNewsReader, DailyRead, ReadItem
from ashare_decision_support.news.dedup import (
    DEFAULT_HALF_LIFE_HOURS,
    MAX_AGE_HOURS,
    dedup_key,
    freshest,
    parse_published,
    recency_weight,
    weigh_docs,
)
from ashare_decision_support.news.discovery import (
    SectorNewsTarget,
    matching_docs,
    rank_news_targets,
    sector_coverage_snapshot,
    sector_doc_counts,
)
from ashare_decision_support.news.events import EventEvidence, detect_events
from ashare_decision_support.news.macro_links import (
    MacroDigest,
    MacroLink,
    macro_context_of,
    read_macro_links,
)
from ashare_decision_support.news.official_source import OfficialNewsSource
from ashare_decision_support.news.relevance import (
    company_aliases,
    filter_company_news,
    is_about,
)
from ashare_decision_support.news.rss_source import RssNewsSource
from ashare_decision_support.news.sentiment import LexiconSentiment
from ashare_decision_support.news.social_source import SocialSnapshotSource
from ashare_decision_support.news.store import IngestResult, NewsStore, StoredDoc
from ashare_decision_support.news.theme_history import ThemeOutcomeStore
from ashare_decision_support.news.theme_radar import ThemeSignal, load_themes, scan_themes
from ashare_decision_support.news.wallstreet_source import WallstreetNewsSource

__all__ = [
    "DEFAULT_HALF_LIFE_HOURS",
    "MAX_AGE_HOURS",
    "AkshareNewsSource",
    "Chain",
    "CompositeNewsSource",
    "DailyNewsReader",
    "DailyRead",
    "EventEvidence",
    "IngestResult",
    "LexiconSentiment",
    "MacroDigest",
    "MacroLink",
    "NewsCache",
    "NewsDoc",
    "NewsSource",
    "NewsStore",
    "OfficialNewsSource",
    "ReadItem",
    "RssNewsSource",
    "SectorNewsTarget",
    "SentimentAnalyzer",
    "SentimentScore",
    "SocialSnapshotSource",
    "StoredDoc",
    "SymbolSentiment",
    "ThemeOutcomeStore",
    "ThemeSignal",
    "WallstreetNewsSource",
    "company_aliases",
    "dedup_key",
    "detect_events",
    "filter_company_news",
    "freshest",
    "is_about",
    "load_themes",
    "macro_context_of",
    "matching_docs",
    "parse_published",
    "rank_news_targets",
    "read_macro_links",
    "recency_weight",
    "scan_themes",
    "sector_coverage_snapshot",
    "sector_doc_counts",
    "weigh_docs",
]
