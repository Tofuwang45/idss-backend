"""Unit tests for the Decision & Auction Engine."""

import pytest

from app.deal_engine import (
    CEILING_BID_PCT,
    DealDecision,
    NEGOTIATE_TARGET_PCT,
    OVERPRICED_ABORT_PCT,
    RISK_DISCOUNT_PCT,
    SNIPE_WINDOW_SECONDS,
    TargetListing,
    decide,
)
from app.market_analysis import FMVResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmv(cents: int = 100000, score: str = "FAIR") -> FMVResult:
    return FMVResult(
        fair_market_value_cents=cents,
        price_delta_cents=0,
        deal_score=score,
        comparables_used=15,
        median_raw_cents=cents,
        weighted_median_cents=cents,
        confidence=1.0,
        iqr_low_cents=cents - 5000,
        iqr_high_cents=cents + 5000,
    )


def _listing(**kw) -> TargetListing:
    defaults = dict(
        title="Test Laptop",
        price_cents=100000,
        listing_type="FixedPrice",
    )
    defaults.update(kw)
    return TargetListing(**defaults)


# ── BIN: BUY_NOW path ────────────────────────────────────────────────────────

class TestBINBuyNow:
    @pytest.mark.asyncio
    async def test_price_below_fmv_minus_discount(self):
        fmv = _fmv(100000)
        listing = _listing(price_cents=90000)
        d = await decide(listing, fmv)
        assert d.action == "BUY_NOW"
        assert d.target_action_price_cents == 90000

    @pytest.mark.asyncio
    async def test_exact_threshold_is_buy(self):
        fmv = _fmv(100000)
        threshold = 100000 - round(100000 * RISK_DISCOUNT_PCT)
        listing = _listing(price_cents=threshold)
        d = await decide(listing, fmv)
        assert d.action == "BUY_NOW"


# ── BIN: NEGOTIATE path ──────────────────────────────────────────────────────

class TestBINNegotiate:
    @pytest.mark.asyncio
    async def test_price_above_threshold_but_not_overpriced(self):
        fmv = _fmv(100000)
        listing = _listing(price_cents=100000)
        d = await decide(listing, fmv)
        assert d.action == "NEGOTIATE"
        assert d.target_action_price_cents == round(100000 * NEGOTIATE_TARGET_PCT)


# ── BIN: WAIT path ───────────────────────────────────────────────────────────

class TestBINWait:
    @pytest.mark.asyncio
    async def test_severely_overpriced(self):
        fmv = _fmv(100000, score="OVERPRICED")
        listing = _listing(price_cents=120000)
        d = await decide(listing, fmv)
        assert d.action == "WAIT"
        assert d.target_action_price_cents is None


# ── Auction: SNIPE_BID path ──────────────────────────────────────────────────

class TestAuctionSnipe:
    @pytest.mark.asyncio
    async def test_ending_soon_bid_below_ceiling(self):
        fmv = _fmv(100000)
        ceiling = round(100000 * CEILING_BID_PCT)
        listing = _listing(
            listing_type="Auction",
            price_cents=100000,
            current_bid_cents=50000,
            time_remaining_seconds=120,
        )
        d = await decide(listing, fmv)
        assert d.action == "SNIPE_BID"
        assert d.target_action_price_cents == ceiling
        assert d.timing_metadata is not None


# ── Auction: WAIT paths ──────────────────────────────────────────────────────

class TestAuctionWait:
    @pytest.mark.asyncio
    async def test_bid_above_ceiling(self):
        fmv = _fmv(100000)
        listing = _listing(
            listing_type="Auction",
            price_cents=100000,
            current_bid_cents=95000,
            time_remaining_seconds=3600,
        )
        d = await decide(listing, fmv)
        assert d.action == "WAIT"

    @pytest.mark.asyncio
    async def test_early_auction_low_velocity(self):
        fmv = _fmv(100000)
        listing = _listing(
            listing_type="Auction",
            price_cents=100000,
            current_bid_cents=30000,
            time_remaining_seconds=7200,
        )
        d = await decide(listing, fmv)
        assert d.action == "WAIT"


# ── MRE Override ──────────────────────────────────────────────────────────────

class TestMREOverride:
    @pytest.mark.asyncio
    async def test_do_not_buy_seller_forces_wait(self):
        fmv = _fmv(100000)
        listing = _listing(
            price_cents=50000,
            merchant_report={"reliability_tier": "DO_NOT_BUY", "risk_flags": ["NEW_ACCOUNT"]},
        )
        d = await decide(listing, fmv)
        assert d.action == "WAIT"
        assert d.risk_level == "HIGH"

    @pytest.mark.asyncio
    async def test_high_tier_seller_allows_buy(self):
        fmv = _fmv(100000)
        listing = _listing(
            price_cents=90000,
            merchant_report={"reliability_tier": "HIGH", "risk_flags": []},
        )
        d = await decide(listing, fmv)
        assert d.action == "BUY_NOW"


# ── Risk level ────────────────────────────────────────────────────────────────

class TestRiskLevel:
    @pytest.mark.asyncio
    async def test_low_confidence_is_high_risk(self):
        fmv = _fmv(100000)
        fmv.confidence = 0.2
        listing = _listing(price_cents=90000)
        d = await decide(listing, fmv)
        assert d.risk_level == "HIGH"

    @pytest.mark.asyncio
    async def test_high_confidence_high_tier_is_low_risk(self):
        fmv = _fmv(100000)
        listing = _listing(
            price_cents=90000,
            merchant_report={"reliability_tier": "HIGH", "risk_flags": []},
        )
        d = await decide(listing, fmv)
        assert d.risk_level == "LOW"


class TestLowConfidenceGuard:
    @pytest.mark.asyncio
    async def test_zero_confidence_forces_wait(self):
        fmv = _fmv(100000)
        fmv.confidence = 0.0
        listing = _listing(price_cents=90000, merchant_report={"reliability_tier": "HIGH", "risk_flags": []})
        d = await decide(listing, fmv)
        assert d.action == "WAIT"
        assert "Insufficient sold comparables" in d.reasoning
