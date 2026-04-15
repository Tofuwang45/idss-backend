"""
Decision & Auction Engine.

Determines the optimal buyer action (BUY_NOW, NEGOTIATE, WAIT, SNIPE_BID)
based on the FMV result and the listing format.

BIN (FixedPrice / AuctionWithBIN):
  - BUY_NOW  when price <= FMV - 5% risk discount
  - NEGOTIATE otherwise, targeting 90% of FMV
  - WAIT     when significantly overpriced (>115% FMV)

Auction:
  - SNIPE_BID  when <5 min remain and current bid < ceiling (93% FMV)
  - WAIT       when bid velocity is low or already at/above ceiling

MRE override: DO_NOT_BUY seller always results in WAIT.
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.market_analysis import FMVResult

logger = logging.getLogger("mcp.deal_engine")

# ── Schemas ───────────────────────────────────────────────────────────────────

class TargetListing(BaseModel):
    item_id: Optional[str] = None
    title: str
    price_cents: int
    condition: Optional[str] = None
    listing_type: str = "FixedPrice"  # "Auction" | "FixedPrice" | "AuctionWithBIN"
    current_bid_cents: Optional[int] = None
    bid_count: Optional[int] = None
    time_remaining_seconds: Optional[int] = None
    seller_username: Optional[str] = None
    merchant_report: Optional[Dict[str, Any]] = None
    accessories: Optional[List[str]] = None


class DealDecision(BaseModel):
    action: str  # "BUY_NOW" | "NEGOTIATE" | "WAIT" | "SNIPE_BID"
    target_action_price_cents: Optional[int] = None
    reasoning: str
    risk_level: str  # "LOW" | "MEDIUM" | "HIGH"
    timing_metadata: Optional[str] = None
    fmv: FMVResult


# ── Constants ─────────────────────────────────────────────────────────────────

RISK_DISCOUNT_PCT = 0.05      # 5% buyer protection buffer
NEGOTIATE_TARGET_PCT = 0.90   # counter-offer at 90% of FMV
OVERPRICED_ABORT_PCT = 1.15   # >115% FMV → WAIT
CEILING_BID_PCT = 0.93        # max auction bid at 93% of FMV
SNIPE_WINDOW_SECONDS = 300    # 5 minutes
LOW_BID_RATIO = 0.80          # current bid below 80% FMV → still early


# ── Risk level ────────────────────────────────────────────────────────────────

def _assess_risk(listing: TargetListing, fmv: FMVResult) -> str:
    mre = listing.merchant_report or {}
    tier = mre.get("reliability_tier", "")
    if tier == "DO_NOT_BUY":
        return "HIGH"
    if tier == "LOW" or fmv.confidence < 0.33:
        return "HIGH"
    if tier == "MEDIUM" or fmv.confidence < 0.67:
        return "MEDIUM"
    return "LOW"


# ── Timing metadata ──────────────────────────────────────────────────────────

def _format_time_remaining(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ── Core decision logic ──────────────────────────────────────────────────────

async def decide(listing: TargetListing, fmv: FMVResult) -> DealDecision:
    """Produce a DealDecision for the given listing + FMV analysis."""

    risk = _assess_risk(listing, fmv)

    mre = listing.merchant_report or {}
    if mre.get("reliability_tier") == "DO_NOT_BUY":
        return DealDecision(
            action="WAIT",
            target_action_price_cents=None,
            reasoning="Seller does not meet reliability thresholds. Recommend finding an alternative listing.",
            risk_level="HIGH",
            timing_metadata=None,
            fmv=fmv,
        )

    # When we have zero sold comparables, avoid assertive price actions.
    if fmv.confidence == 0.0:
        return DealDecision(
            action="WAIT",
            target_action_price_cents=None,
            reasoning=(
                "Insufficient sold comparables for reliable fair-market valuation. "
                "Recommend waiting for more data or broadening the search."
            ),
            risk_level="HIGH",
            timing_metadata=None,
            fmv=fmv,
        )

    fmv_cents = fmv.fair_market_value_cents
    price = listing.price_cents

    if listing.listing_type in ("FixedPrice", "AuctionWithBIN"):
        return _bin_decision(price, fmv_cents, fmv, risk)

    if listing.listing_type == "Auction":
        return _auction_decision(listing, fmv_cents, fmv, risk)

    return _bin_decision(price, fmv_cents, fmv, risk)


def _bin_decision(
    price: int,
    fmv_cents: int,
    fmv: FMVResult,
    risk: str,
) -> DealDecision:
    risk_discount = round(fmv_cents * RISK_DISCOUNT_PCT)
    buy_threshold = fmv_cents - risk_discount

    if price <= buy_threshold:
        return DealDecision(
            action="BUY_NOW",
            target_action_price_cents=price,
            reasoning=(
                f"Listed at ${price/100:.2f}, which is ${(fmv_cents - price)/100:.2f} "
                f"below fair market value (${fmv_cents/100:.2f}). Good deal."
            ),
            risk_level=risk,
            fmv=fmv,
        )

    if fmv.deal_score == "OVERPRICED" and price > round(fmv_cents * OVERPRICED_ABORT_PCT):
        return DealDecision(
            action="WAIT",
            target_action_price_cents=None,
            reasoning=(
                f"Listed at ${price/100:.2f}, which is {((price / fmv_cents) - 1) * 100:.0f}% "
                f"above FMV (${fmv_cents/100:.2f}). Recommend waiting for a better listing."
            ),
            risk_level=risk,
            fmv=fmv,
        )

    target = round(fmv_cents * NEGOTIATE_TARGET_PCT)
    return DealDecision(
        action="NEGOTIATE",
        target_action_price_cents=target,
        reasoning=(
            f"Listed at ${price/100:.2f} vs FMV ${fmv_cents/100:.2f}. "
            f"Suggest offering ${target/100:.2f} (10% below FMV)."
        ),
        risk_level=risk,
        fmv=fmv,
    )


def _auction_decision(
    listing: TargetListing,
    fmv_cents: int,
    fmv: FMVResult,
    risk: str,
) -> DealDecision:
    ceiling = round(fmv_cents * CEILING_BID_PCT)
    current_bid = listing.current_bid_cents or 0
    remaining = listing.time_remaining_seconds

    time_str = _format_time_remaining(remaining)

    if current_bid >= ceiling:
        return DealDecision(
            action="WAIT",
            target_action_price_cents=None,
            reasoning=(
                f"Current bid ${current_bid/100:.2f} is at or above the ceiling "
                f"(${ceiling/100:.2f}). Not worth bidding higher."
            ),
            risk_level=risk,
            timing_metadata=f"Auction ends in {time_str}; current bid already at ceiling." if time_str else None,
            fmv=fmv,
        )

    if remaining is not None and remaining < SNIPE_WINDOW_SECONDS:
        return DealDecision(
            action="SNIPE_BID",
            target_action_price_cents=ceiling,
            reasoning=(
                f"Auction ending soon ({time_str}). Current bid ${current_bid/100:.2f} "
                f"is below ceiling ${ceiling/100:.2f}. Place a snipe bid."
            ),
            risk_level=risk,
            timing_metadata=f"Auction ends in {time_str}; current bid is {((fmv_cents - current_bid) / fmv_cents * 100):.0f}% below FMV.",
            fmv=fmv,
        )

    if current_bid < round(fmv_cents * LOW_BID_RATIO):
        return DealDecision(
            action="WAIT",
            target_action_price_cents=ceiling,
            reasoning=(
                f"Auction has {time_str or 'time'} left. Current bid ${current_bid/100:.2f} is "
                f"{((fmv_cents - current_bid) / fmv_cents * 100):.0f}% below FMV — bid velocity is low. "
                f"Wait and snipe closer to the end."
            ),
            risk_level=risk,
            timing_metadata=f"Auction ends in {time_str}; current bid is {((fmv_cents - current_bid) / fmv_cents * 100):.0f}% below FMV.",
            fmv=fmv,
        )

    return DealDecision(
        action="SNIPE_BID",
        target_action_price_cents=ceiling,
        reasoning=(
            f"Current bid ${current_bid/100:.2f} approaching FMV. "
            f"Max bid ${ceiling/100:.2f} protects 7% margin below FMV."
        ),
        risk_level=risk,
        timing_metadata=f"Auction ends in {time_str}; current bid is {((fmv_cents - current_bid) / fmv_cents * 100):.0f}% below FMV." if time_str else None,
        fmv=fmv,
    )
