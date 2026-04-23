"""Regression tests for app.price_history.build_price_history_summary.

These lock the schema that the frontend `PriceHistoryChart` already consumes.
Any change here must be paired with a change to
``idss-web/src/types/chat.ts::PriceHistorySummary``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market_analysis import SoldComparable
from app.price_history import (
    MIN_COMPARABLES,
    build_price_history_summary,
)


def _comp(days_ago: int, price_cents: int, title: str = "MacBook Pro 14 M3") -> SoldComparable:
    end = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return SoldComparable(
        title=title,
        sold_price_cents=price_cents,
        condition="Used",
        end_time=end,
        listing_type="FixedPrice",
    )


class TestMinimumSampleGuards:
    def test_returns_none_below_min_comparables(self):
        comps = [_comp(i, 100_000) for i in range(MIN_COMPARABLES - 1)]
        assert build_price_history_summary(comps) is None

    def test_returns_none_when_all_comps_missing_timestamps(self):
        # Construct comparables with times that fall outside the window so
        # everything is rejected.
        comps = [_comp(365, 100_000) for _ in range(5)]
        assert build_price_history_summary(comps, window_days=30) is None

    def test_returns_none_when_only_one_bucket_has_data(self):
        # All 4 comps fall within the same week → only one non-empty bucket.
        comps = [_comp(2, 100_000 + i * 1000) for i in range(4)]
        assert build_price_history_summary(comps, window_days=90, bucket_days=7) is None


class TestHappyPath:
    def test_schema_matches_frontend_contract(self):
        # Spread 12 comps across ~8 weeks so multiple buckets are non-empty.
        prices = [90_000, 95_000, 100_000, 105_000, 110_000, 115_000,
                  92_000, 98_000, 102_000, 108_000, 112_000, 118_000]
        comps = [_comp(days_ago=i * 6 + 1, price_cents=p) for i, p in enumerate(prices)]

        out = build_price_history_summary(
            comps,
            window_days=90,
            bucket_days=7,
            sources=[
                {"source": "ebay_finding", "count": 8},
                {"source": "serpapi", "count": 4},
            ],
        )
        assert out is not None

        # Top-level fields the chart reads.
        for key in (
            "window_days", "first_observed", "last_observed", "n",
            "p10_cents", "p50_cents", "p90_cents", "min_cents", "max_cents",
            "trend_pct", "buckets", "sources",
        ):
            assert key in out, f"missing key: {key}"

        assert out["window_days"] == 90
        assert out["n"] == len(prices)
        assert out["min_cents"] == min(prices)
        assert out["max_cents"] == max(prices)
        assert out["p10_cents"] <= out["p50_cents"] <= out["p90_cents"]
        assert out["sources"] == ["ebay_finding", "serpapi"]
        assert len(out["buckets"]) >= 2

        # Bucket schema.
        for b in out["buckets"]:
            for key in ("bucket_start", "bucket_end", "n", "median_cents",
                        "min_cents", "max_cents"):
                assert key in b
            assert b["n"] >= 1
            assert b["min_cents"] <= b["median_cents"] <= b["max_cents"]

    def test_trend_pct_sign_reflects_direction(self):
        # Recent comps more expensive than older comps → positive trend.
        comps_rising = (
            [_comp(days_ago=75 - i, price_cents=90_000) for i in range(4)]
            + [_comp(days_ago=15 - i, price_cents=120_000) for i in range(4)]
        )
        out = build_price_history_summary(comps_rising, window_days=90, bucket_days=7)
        assert out is not None and out["trend_pct"] is not None
        assert out["trend_pct"] > 0

        # And the reverse.
        comps_falling = (
            [_comp(days_ago=75 - i, price_cents=120_000) for i in range(4)]
            + [_comp(days_ago=15 - i, price_cents=90_000) for i in range(4)]
        )
        out2 = build_price_history_summary(comps_falling, window_days=90, bucket_days=7)
        assert out2 is not None and out2["trend_pct"] is not None
        assert out2["trend_pct"] < 0

    def test_source_fallback_when_sources_diag_is_empty(self):
        comps = [_comp(days_ago=i * 6 + 1, price_cents=100_000 + i * 2000) for i in range(8)]
        out = build_price_history_summary(
            comps, source="sold_history", sources=None,
        )
        assert out is not None
        assert out["sources"] == ["sold_history"]

    def test_zero_count_sources_are_filtered(self):
        comps = [_comp(days_ago=i * 6 + 1, price_cents=100_000 + i * 2000) for i in range(8)]
        out = build_price_history_summary(
            comps,
            sources=[
                {"source": "ebay_finding", "count": 8},
                {"source": "serpapi", "count": 0},
            ],
        )
        assert out is not None
        assert out["sources"] == ["ebay_finding"]
