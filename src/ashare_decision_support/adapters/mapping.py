from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ashare_decision_support.models.schemas import AuctionFeatures, OpenFeatures
from ashare_decision_support.numeric import finite


class NormalizationError(ValueError):
    """Raised when a raw payload cannot be normalized safely.

    **NaN 属于「不能安全归一」，不属于「一个数」。** 这里此前用 `try/float`，而
    `float(nan)` 和 `float("nan")` 都是**成功**的——于是一份带 NaN 报价的载荷会
    「归一成功」，把这一层存在的理由绕过去。改走 `numeric.finite` 之后，非有限
    值和解析失败一样抛 `NormalizationError`，由调用方决定丢这一条还是丢整批。
    """


@dataclass(frozen=True)
class MappingProfile:
    name: str
    auction_aliases: dict[str, tuple[str, ...]]
    open_aliases: dict[str, tuple[str, ...]]


def _default_auction_aliases() -> dict[str, tuple[str, ...]]:
    return {
        "symbol": ("symbol", "Symbol", "stock_code", "stockCode"),
        "matched_qty": ("MatchedQty", "matchedQty", "matched_qty"),
        "matched_amount": ("MatchedAmount", "matchedAmount", "matched_amount"),
        "unmatched_qty": ("UnmatchedQty", "unmatchedQty", "unmatched_qty"),
        "unmatched_side": ("UnmatchedSide", "unmatchedSide", "unmatched_side"),
        "indicative_match_price": (
            "IndicativeMatchPrice",
            "indicativeMatchPrice",
            "indicative_match_price",
            "BidPrice1",
            "bidPrice1"
        ),
        "open_gap_pct": ("OpenGapPct", "openGapPct", "open_gap_pct"),
        "cancel_pre920_ratio": (
            "CancelPre920Ratio",
            "cancelPre920Ratio",
            "cancel_pre920_ratio",
            "R_cancel_pre920"
        ),
        "seal_ratio": ("SealRatio", "sealRatio", "seal_ratio"),
        "tail_flow_distortion": (
            "TailFlowDistortion",
            "tailFlowDistortion",
            "tail_flow_distortion"
        ),
        "data_stale": ("DataStale", "dataStale", "data_stale"),
        "source_ts": ("source_ts", "sourceTs", "SourceTs", "timestamp"),
        "prev_close": ("PrevClose", "prevClose", "prev_close"),
    }


def _default_open_aliases() -> dict[str, tuple[str, ...]]:
    return {
        "symbol": ("symbol", "Symbol", "stock_code", "stockCode"),
        "minutes_since_open": ("minutesSinceOpen", "MinutesSinceOpen", "minutes_since_open"),
        "price_vs_open_pct": ("priceVsOpenPct", "PriceVsOpenPct", "price_vs_open_pct"),
        "price_vs_vwap_pct": ("priceVsVwapPct", "PriceVsVwapPct", "price_vs_vwap_pct"),
        "volume_ratio": ("volumeRatio", "VolumeRatio", "volume_ratio"),
        "open_gap_pct": ("openGapPct", "OpenGapPct", "open_gap_pct"),
        "data_stale": ("dataStale", "DataStale", "data_stale"),
        "source_ts": ("source_ts", "sourceTs", "SourceTs", "timestamp"),
    }


GENERIC_QMT_PROFILE = MappingProfile(
    name="generic-qmt",
    auction_aliases=_default_auction_aliases(),
    open_aliases=_default_open_aliases(),
)


PROFILES: dict[str, MappingProfile] = {
    GENERIC_QMT_PROFILE.name: GENERIC_QMT_PROFILE,
    "generic-miniqmt": GENERIC_QMT_PROFILE,
}


def get_profile(name: str) -> MappingProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise NormalizationError(f"unknown mapping profile: {name}") from exc


def mapping_profile_from_dict(payload: dict) -> MappingProfile:
    try:
        auction_aliases = payload["auction_aliases"]
        open_aliases = payload["open_aliases"]
    except KeyError as exc:
        raise NormalizationError(f"mapping profile is missing key: {exc}") from exc

    return MappingProfile(
        name=str(payload.get("name", "custom-profile")),
        auction_aliases={key: tuple(value) for key, value in auction_aliases.items()},
        open_aliases={key: tuple(value) for key, value in open_aliases.items()},
    )


def load_profile_from_path(path: str | Path) -> MappingProfile:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise NormalizationError("mapping profile file must contain a JSON object")
    return mapping_profile_from_dict(payload)


class PayloadNormalizer:
    def __init__(self, profile: MappingProfile | None = None) -> None:
        self.profile = profile or GENERIC_QMT_PROFILE

    def normalize_auction(self, payload: dict) -> AuctionFeatures:
        resolved = self._resolve_fields(payload, self.profile.auction_aliases)
        matched_qty = self._require_int(resolved, "matched_qty")
        indicative_match_price = self._require_float(resolved, "indicative_match_price")
        matched_amount = self._optional_float(resolved, "matched_amount")
        if matched_amount is None:
            matched_amount = matched_qty * indicative_match_price
        open_gap_pct = self._derive_open_gap_pct(resolved, indicative_match_price)

        return AuctionFeatures(
            symbol=self._require_str(resolved, "symbol"),
            matched_qty=matched_qty,
            matched_amount=matched_amount,
            unmatched_qty=self._require_int(resolved, "unmatched_qty"),
            unmatched_side=self._optional_side(resolved, "unmatched_side"),
            indicative_match_price=indicative_match_price,
            open_gap_pct=open_gap_pct,
            cancel_pre920_ratio=self._optional_float(resolved, "cancel_pre920_ratio"),
            seal_ratio=self._optional_float(resolved, "seal_ratio"),
            tail_flow_distortion=self._optional_bool(resolved, "tail_flow_distortion", default=False),
            data_stale=self._optional_bool(resolved, "data_stale", default=False),
            source_ts=self._require_datetime(resolved, "source_ts"),
        )

    def normalize_open(self, payload: dict) -> OpenFeatures:
        resolved = self._resolve_fields(payload, self.profile.open_aliases)
        return OpenFeatures(
            symbol=self._require_str(resolved, "symbol"),
            minutes_since_open=self._require_int(resolved, "minutes_since_open"),
            price_vs_open_pct=self._require_float(resolved, "price_vs_open_pct"),
            price_vs_vwap_pct=self._require_float(resolved, "price_vs_vwap_pct"),
            volume_ratio=self._require_float(resolved, "volume_ratio"),
            open_gap_pct=self._require_float(resolved, "open_gap_pct"),
            data_stale=self._optional_bool(resolved, "data_stale", default=False),
            source_ts=self._require_datetime(resolved, "source_ts"),
        )

    def _resolve_fields(self, payload: dict, aliases: dict[str, tuple[str, ...]]) -> dict[str, object]:
        resolved: dict[str, object] = {}
        for canonical_name, candidates in aliases.items():
            for alias in candidates:
                if alias in payload:
                    resolved[canonical_name] = payload[alias]
                    break
        return resolved

    def _derive_open_gap_pct(self, resolved: dict[str, object], indicative_match_price: float) -> float:
        raw_gap = self._optional_float(resolved, "open_gap_pct")
        if raw_gap is not None:
            return raw_gap
        prev_close = self._optional_float(resolved, "prev_close")
        if prev_close is None or prev_close <= 0:
            raise NormalizationError("missing open_gap_pct and prev_close; cannot derive open gap")
        return (indicative_match_price / prev_close - 1.0) * 100.0

    def _require_str(self, resolved: dict[str, object], key: str) -> str:
        value = resolved.get(key)
        if value is None:
            raise NormalizationError(f"missing required field: {key}")
        return str(value)

    def _require_int(self, resolved: dict[str, object], key: str) -> int:
        value = resolved.get(key)
        if value is None:
            raise NormalizationError(f"missing required field: {key}")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise NormalizationError(f"field {key} is not a valid int: {value!r}") from exc

    def _require_float(self, resolved: dict[str, object], key: str) -> float:
        value = resolved.get(key)
        if value is None:
            raise NormalizationError(f"missing required field: {key}")
        number = finite(value)
        if number is None:
            raise NormalizationError(f"field {key} is not a valid float: {value!r}")
        return number

    def _optional_float(self, resolved: dict[str, object], key: str) -> float | None:
        value = resolved.get(key)
        if value is None:
            return None
        number = finite(value)
        if number is None:
            raise NormalizationError(f"field {key} is not a valid float: {value!r}")
        return number

    def _optional_bool(self, resolved: dict[str, object], key: str, default: bool) -> bool:
        value = resolved.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise NormalizationError(f"field {key} is not a valid bool: {value!r}")

    def _optional_side(self, resolved: dict[str, object], key: str) -> str:
        value = resolved.get(key, "N")
        normalized = str(value).upper()
        if normalized not in {"B", "S", "N"}:
            raise NormalizationError(f"field {key} is not a valid side: {value!r}")
        return normalized

    def _require_datetime(self, resolved: dict[str, object], key: str) -> datetime:
        value = resolved.get(key)
        if value is None:
            raise NormalizationError(f"missing required field: {key}")
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise NormalizationError(f"field {key} is not a valid ISO datetime: {value!r}") from exc
        raise NormalizationError(f"field {key} is not a valid datetime: {value!r}")
