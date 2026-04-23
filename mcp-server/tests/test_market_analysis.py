"""Unit tests for the Fair Market Value Calculator."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.market_analysis import (
    FMVResult,
    SoldComparable,
    compute_fmv,
    iqr_filter,
    narrow_comparables_to_price_band,
    parse_comparables,
    time_decay_weights,
    weighted_median,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_comp(price_cents: int, days_ago: int = 5, condition: str | None = None) -> SoldComparable:
    return SoldComparable(
        title="Test Item",
        sold_price_cents=price_cents,
        condition=condition,
        end_time=datetime.now(timezone.utc) - timedelta(days=days_ago),
        listing_type="FixedPrice",
    )


# ── IQR filter ────────────────────────────────────────────────────────────────

class TestIQRFilter:
    def test_removes_outliers(self):
        prices = np.array([100, 102, 105, 108, 110, 500])
        filtered, q1, q3 = iqr_filter(prices)
        assert 500 not in filtered
        assert len(filtered) == 5

    def test_keeps_all_when_no_outliers(self):
        prices = np.array([100, 102, 105, 108, 110])
        filtered, _, _ = iqr_filter(prices)
        assert len(filtered) == 5

    def test_small_sample_no_crash(self):
        prices = np.array([100, 200])
        filtered, q1, q3 = iqr_filter(prices)
        assert len(filtered) >= 1

    def test_single_value(self):
        prices = np.array([100])
        filtered, q1, q3 = iqr_filter(prices)
        assert len(filtered) == 1
        assert q1 == 100 and q3 == 100


# ── Time decay weights ───────────────────────────────────────────────────────

class TestTimeDecay:
    def test_recent_heavier(self):
        now = datetime.now(timezone.utc)
        times = [now - timedelta(days=1), now - timedelta(days=30)]
        w = time_decay_weights(times, now=now)
        assert w[0] > w[1]

    def test_same_day_weight_near_one(self):
        now = datetime.now(timezone.utc)
        w = time_decay_weights([now], now=now)
        assert abs(w[0] - 1.0) < 0.01


# ── Weighted median ──────────────────────────────────────────────────────────

class TestWeightedMedian:
    def test_equal_weights_matches_median(self):
        vals = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        wts = np.ones(5)
        result = weighted_median(vals, wts)
        assert result == 30.0

    def test_heavy_weight_pulls_median(self):
        vals = np.array([10.0, 50.0])
        wts = np.array([1.0, 10.0])
        result = weighted_median(vals, wts)
        assert result == 50.0


# ── compute_fmv ──────────────────────────────────────────────────────────────

class TestComputeFMV:
    @pytest.mark.asyncio
    async def test_no_comparables_returns_target(self):
        result = await compute_fmv(target_price_cents=10000, comparables=[])
        assert result.fair_market_value_cents == 10000
        assert result.confidence == 0.0
        assert result.deal_score == "FAIR"

    @pytest.mark.asyncio
    async def test_good_deal(self):
        comps = [_make_comp(10000, days_ago=i) for i in range(1, 16)]
        result = await compute_fmv(target_price_cents=8000, comparables=comps)
        assert result.deal_score == "GOOD"
        assert result.price_delta_cents < 0
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_overpriced(self):
        comps = [_make_comp(5000, days_ago=i) for i in range(1, 16)]
        result = await compute_fmv(target_price_cents=7000, comparables=comps)
        assert result.deal_score == "OVERPRICED"
        assert result.price_delta_cents > 0

    @pytest.mark.asyncio
    async def test_fair_deal(self):
        comps = [_make_comp(10000, days_ago=i) for i in range(1, 16)]
        result = await compute_fmv(target_price_cents=10000, comparables=comps)
        assert result.deal_score == "FAIR"

    @pytest.mark.asyncio
    async def test_low_confidence_few_comps(self):
        comps = [_make_comp(10000, days_ago=1), _make_comp(10500, days_ago=2)]
        result = await compute_fmv(target_price_cents=10000, comparables=comps)
        assert result.confidence < 0.33

    @pytest.mark.asyncio
    async def test_outliers_rejected(self):
        comps = [_make_comp(10000, days_ago=i) for i in range(1, 11)]
        comps.append(_make_comp(99999, days_ago=1))
        result = await compute_fmv(target_price_cents=10000, comparables=comps)
        assert result.fair_market_value_cents < 15000

    @pytest.mark.asyncio
    async def test_condition_adjustment(self):
        comps = [_make_comp(10000, days_ago=i) for i in range(1, 16)]
        result_new = await compute_fmv(target_price_cents=10000, comparables=comps, target_condition="New")
        result_used = await compute_fmv(target_price_cents=10000, comparables=comps, target_condition="Used")
        assert result_new.fair_market_value_cents > result_used.fair_market_value_cents


# ── parse_comparables ────────────────────────────────────────────────────────

class TestParseComparables:
    def test_basic_parse(self):
        raw = [{
            "title": "Test",
            "sold_price_cents": 5000,
            "end_time": "2025-01-01T12:00:00Z",
            "listing_type": "FixedPrice",
        }]
        comps = parse_comparables(raw)
        assert len(comps) == 1
        assert comps[0].sold_price_cents == 5000

    def test_skip_invalid(self):
        raw = [{"title": "Bad"}, {"title": "OK", "sold_price_cents": 3000, "end_time": "2025-01-01T00:00:00Z"}]
        comps = parse_comparables(raw)
        assert len(comps) == 1


class TestNarrowComparablesToPriceBand:
    def test_filters_cheap_outliers_when_enough_remain(self):
        comps = [
            _make_comp(20_000),
            _make_comp(75_000),
            _make_comp(80_000),
            _make_comp(85_000),
        ]
        out = narrow_comparables_to_price_band(comps, 700.0, 1000.0, keep_at_least=3)
        assert len(out) == 3
        assert all(c.sold_price_cents >= 60_000 for c in out)

    def test_noop_when_too_few_after_filter(self):
        comps = [_make_comp(20_000), _make_comp(25_000)]
        out = narrow_comparables_to_price_band(comps, 700.0, 1000.0, keep_at_least=3)
        assert out == comps
