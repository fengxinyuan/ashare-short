from ashare_decision_support.adapters.mapping import NormalizationError, PayloadNormalizer


def test_normalize_auction_payload_with_aliases_and_derived_amount() -> None:
    normalizer = PayloadNormalizer()
    features = normalizer.normalize_auction(
        {
            "symbol": "300001.SZ",
            "MatchedQty": 1000,
            "UnmatchedQty": 100,
            "UnmatchedSide": "B",
            "BidPrice1": 12.5,
            "PrevClose": 12.0,
            "sourceTs": "2026-07-31T09:24:58",
        }
    )
    assert features.matched_amount == 12_500.0
    assert round(features.open_gap_pct, 4) == round((12.5 / 12.0 - 1.0) * 100.0, 4)
    assert features.unmatched_side == "B"


def test_normalize_open_payload_with_camel_case() -> None:
    normalizer = PayloadNormalizer()
    features = normalizer.normalize_open(
        {
            "symbol": "300001.SZ",
            "minutesSinceOpen": 3,
            "priceVsOpenPct": 0.45,
            "priceVsVwapPct": 0.2,
            "volumeRatio": 1.1,
            "openGapPct": 3.4,
            "sourceTs": "2026-07-31T09:33:00",
        }
    )
    assert features.minutes_since_open == 3
    assert features.open_gap_pct == 3.4


def test_missing_auction_required_field_raises() -> None:
    normalizer = PayloadNormalizer()
    try:
        normalizer.normalize_auction(
            {
                "symbol": "300001.SZ",
                "MatchedQty": 1000,
                "UnmatchedQty": 100,
                "sourceTs": "2026-07-31T09:24:58",
            }
        )
    except NormalizationError as exc:
        assert "indicative_match_price" in str(exc)
    else:
        raise AssertionError("NormalizationError was expected")
