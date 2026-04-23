"""
Fair Market Value (FMV) Calculator.

Uses recently-sold eBay data (from findCompletedItems) to produce a
statistically grounded fair-market-value estimate for a target listing.

Pipeline:
  1. Parse sold comparables.
  2. IQR outlier rejection on sold prices.
  3. Exponential time-decay weighting (recent sales matter more).
  4. Weighted median calculation.
  5. Optional condition-based feature adjustment.
  6. Deal scoring against the target price.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
from pydantic import BaseModel

logger = logging.getLogger("mcp.market_analysis")

# ── Schemas ───────────────────────────────────────────────────────────────────

class SoldComparable(BaseModel):
    title: str
    sold_price_cents: int
    condition: Optional[str] = None
    end_time: datetime
    listing_type: str  # "Auction" | "FixedPrice"


class FMVResult(BaseModel):
    fair_market_value_cents: int
    price_delta_cents: int
    deal_score: str  # "GOOD" | "FAIR" | "OVERPRICED"
    comparables_used: int
    median_raw_cents: int
    weighted_median_cents: int
    confidence: float  # 0.0–1.0
    iqr_low_cents: int
    iqr_high_cents: int


# ── Configuration ─────────────────────────────────────────────────────────────

DECAY_LAMBDA = 0.03  # half-life ≈ 23 days

CONDITION_MODIFIERS: Dict[str, float] = {
    "new": 1.05,
    "new with tags": 1.05,
    "new without tags": 1.02,
    "new other": 1.02,
    "certified refurbished": 1.00,
    "seller refurbished": 0.97,
    "like new": 0.98,
    "excellent - refurbished": 0.97,
    "very good - refurbished": 0.95,
    "good - refurbished": 0.93,
    "used": 0.93,
    "for parts or not working": 0.60,
}

DEAL_GOOD_THRESHOLD = 0.92
DEAL_FAIR_THRESHOLD = 1.05

CONFIDENCE_FULL_AT = 15  # 15+ comparables → confidence 1.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_comparables(raw_items: List[Dict[str, Any]]) -> List[SoldComparable]:
    """Convert raw dicts from fetch_completed_items into typed SoldComparables."""
    comps: List[SoldComparable] = []
    for item in raw_items:
        try:
            et = item.get("end_time")
            if isinstance(et, str):
                if et.endswith("Z"):
                    et = et[:-1] + "+00:00"
                end_dt = datetime.fromisoformat(et)
            elif isinstance(et, datetime):
                end_dt = et
            else:
                end_dt = datetime.now(timezone.utc)

            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)

            comps.append(SoldComparable(
                title=item.get("title", ""),
                sold_price_cents=int(item["sold_price_cents"]),
                condition=item.get("condition"),
                end_time=end_dt,
                listing_type=item.get("listing_type", "FixedPrice"),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("parse_comparable_skip: %s", exc)
    return comps


def narrow_comparables_to_price_band(
    comparables: List[SoldComparable],
    min_usd: Optional[float],
    max_usd: Optional[float],
    *,
    keep_at_least: int = 3,
    slack: float = 0.12,
) -> List[SoldComparable]:
    """Drop sold comps outside the shopper's budget band when enough comps remain.

    Prevents FMV from being anchored by $200 clearance units when the user asked
    for a $700–$1000 machine. If filtering would leave too few points, returns
    the original list unchanged.
    """
    if not comparables or (min_usd is None and max_usd is None):
        return comparables
    lo_c = int(min_usd * (1.0 - slack) * 100.0) if min_usd is not None else None
    hi_c = int(max_usd * (1.0 + slack) * 100.0) if max_usd is not None else None
    filtered = [
        c
        for c in comparables
        if (lo_c is None or c.sold_price_cents >= lo_c)
        and (hi_c is None or c.sold_price_cents <= hi_c)
    ]
    return filtered if len(filtered) >= keep_at_least else comparables


def iqr_filter(prices: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Remove outliers using 1.5×IQR rule. Returns (filtered, q1, q3)."""
    if len(prices) < 4:
        return prices, float(np.min(prices)), float(np.max(prices))
    q1, q3 = float(np.percentile(prices, 25)), float(np.percentile(prices, 75))
    iqr = q3 - q1
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    mask = (prices >= low) & (prices <= high)
    filtered = prices[mask]
    if len(filtered) == 0:
        return prices, q1, q3
    return filtered, q1, q3


def time_decay_weights(
    end_times: List[datetime],
    now: Optional[datetime] = None,
    lam: float = DECAY_LAMBDA,
) -> np.ndarray:
    """Exponential decay: weight = exp(-λ × days_since_sale)."""
    if now is None:
        now = datetime.now(timezone.utc)
    days = np.array([
        max((now - et).total_seconds() / 86400.0, 0.0) for et in end_times
    ])
    return np.exp(-lam * days)


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted median via sorted cumulative weights."""
    order = np.argsort(values)
    s_vals = values[order]
    s_wts = weights[order]
    cum = np.cumsum(s_wts)
    half = cum[-1] / 2.0
    idx = np.searchsorted(cum, half)
    idx = min(idx, len(s_vals) - 1)
    return float(s_vals[idx])


def _condition_multiplier(condition: Optional[str]) -> float:
    if not condition:
        return 1.0
    key = condition.strip().lower()
    return CONDITION_MODIFIERS.get(key, 1.0)


# ── Main calculator ──────────────────────────────────────────────────────────

async def compute_fmv(
    target_price_cents: int,
    comparables: List[SoldComparable],
    target_condition: Optional[str] = None,
    confidence_cap: float = 1.0,
) -> FMVResult:
    """
    Calculate fair market value from sold comparables.

    Returns an FMVResult with deal scoring relative to target_price_cents.
    """
    if not comparables:
        return FMVResult(
            fair_market_value_cents=target_price_cents,
            price_delta_cents=0,
            deal_score="FAIR",
            comparables_used=0,
            median_raw_cents=target_price_cents,
            weighted_median_cents=target_price_cents,
            confidence=0.0,
            iqr_low_cents=target_price_cents,
            iqr_high_cents=target_price_cents,
        )

    prices = np.array([c.sold_price_cents for c in comparables], dtype=np.float64)

    filtered, q1, q3 = iqr_filter(prices)

    kept_indices = []
    for i, c in enumerate(comparables):
        if q1 - 1.5 * (q3 - q1) <= c.sold_price_cents <= q3 + 1.5 * (q3 - q1):
            kept_indices.append(i)
    if not kept_indices:
        kept_indices = list(range(len(comparables)))

    kept_comps = [comparables[i] for i in kept_indices]
    kept_prices = np.array([c.sold_price_cents for c in kept_comps], dtype=np.float64)

    raw_median = float(np.median(kept_prices))

    weights = time_decay_weights([c.end_time for c in kept_comps])
    w_med = weighted_median(kept_prices, weights)

    cond_mult = _condition_multiplier(target_condition)
    fmv = w_med * cond_mult

    fmv_cents = round(fmv)
    delta = target_price_cents - fmv_cents

    if target_price_cents <= fmv_cents * DEAL_GOOD_THRESHOLD:
        deal_score = "GOOD"
    elif target_price_cents <= fmv_cents * DEAL_FAIR_THRESHOLD:
        deal_score = "FAIR"
    else:
        deal_score = "OVERPRICED"

    confidence = min(len(kept_comps) / CONFIDENCE_FULL_AT, 1.0, confidence_cap)

    return FMVResult(
        fair_market_value_cents=fmv_cents,
        price_delta_cents=delta,
        deal_score=deal_score,
        comparables_used=len(kept_comps),
        median_raw_cents=round(raw_median),
        weighted_median_cents=round(w_med),
        confidence=round(confidence, 3),
        iqr_low_cents=round(q1),
        iqr_high_cents=round(q3),
    )
