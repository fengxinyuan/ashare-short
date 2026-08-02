"""Sentiment (舆情) collection and scoring."""

from ashare_decision_support.news.akshare_source import AkshareNewsSource
from ashare_decision_support.news.base import (
    NewsBundle,
    NewsDoc,
    NewsSource,
    SentimentAnalyzer,
    SentimentScore,
    SymbolSentiment,
)
from ashare_decision_support.news.cache import NewsCache
from ashare_decision_support.news.composite import CompositeNewsSource
from ashare_decision_support.news.dedup import (
    DEFAULT_HALF_LIFE_HOURS,
    MAX_AGE_HOURS,
    dedup_key,
    freshest,
    parse_published,
    recency_weight,
    weigh_docs,
)
from ashare_decision_support.news.sentiment import LexiconSentiment
from ashare_decision_support.news.wallstreet_source import WallstreetNewsSource

__all__ = [
    "AkshareNewsSource",
    "CompositeNewsSource",
    "DEFAULT_HALF_LIFE_HOURS",
    "LexiconSentiment",
    "MAX_AGE_HOURS",
    "NewsBundle",
    "NewsCache",
    "NewsDoc",
    "NewsSource",
    "SentimentAnalyzer",
    "SentimentScore",
    "SymbolSentiment",
    "WallstreetNewsSource",
    "dedup_key",
    "freshest",
    "parse_published",
    "recency_weight",
    "weigh_docs",
]
