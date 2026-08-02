from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ashare_decision_support.models.schemas import AuctionFeatures, OpenFeatures


class MarketDataAdapter(ABC):
    @abstractmethod
    def subscribe_quotes(self, symbols: list[str]) -> None:
        """Subscribe to top-of-book or quote updates."""

    @abstractmethod
    def subscribe_l2_snapshot(self, symbols: list[str]) -> None:
        """Subscribe to auction or L2 snapshots."""

    @abstractmethod
    def subscribe_ticks(self, symbols: list[str]) -> None:
        """Subscribe to tick-by-tick execution data."""

    @abstractmethod
    def subscribe_orders(self, symbols: list[str]) -> None:
        """Subscribe to order-detail data when available."""

    @abstractmethod
    def get_security_master(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch static symbol metadata."""

    @abstractmethod
    def get_historical_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict]]:
        """Fetch historical OHLCV bars."""

    @abstractmethod
    def get_trading_calendar(self) -> list[date]:
        """Fetch trading days."""

    @abstractmethod
    def get_auction_features(self, symbols: list[str]) -> dict[str, AuctionFeatures]:
        """Fetch normalized auction-stage features."""

    @abstractmethod
    def get_open_features(self, symbols: list[str]) -> dict[str, OpenFeatures]:
        """Fetch normalized open-follow features."""
