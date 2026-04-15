"""
Merchant Reliability Engine — deterministic scoring + optional LLM sentiment.

Computes a MerchantReport from eBay Finding API seller fields and optional
Browse API enrichment data.  Tier assignment follows strict precedence:

  1. DO_NOT_BUY  — positive_feedback_pct < 90 or critical risk flags
  2. HIGH        — topRatedSeller AND pct >= 98 AND feedbackScore >= 500
  3. MEDIUM      — pct >= 95 AND feedbackScore >= 50
  4. LOW         — everything else (including unknowns)

Feature flags:
  MERCHANT_RELIABILITY_ENABLED  — master on/off (default on; set to 0 to disable)
  MERCHANT_LLM_SENTIMENT        — enable LLM sentiment layer (default 0)
"""

import os
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("mcp.merchant_reliability")

# ── Feature flags ─────────────────────────────────────────────────────────────

def is_mre_enabled() -> bool:
    return os.getenv("MERCHANT_RELIABILITY_ENABLED", "1") == "1"


def is_llm_sentiment_enabled() -> bool:
    return is_mre_enabled() and os.getenv("MERCHANT_LLM_SENTIMENT", "0") == "1"


# ── Schema ────────────────────────────────────────────────────────────────────

TIER_HIGH = "HIGH"
TIER_MEDIUM = "MEDIUM"
TIER_LOW = "LOW"
TIER_DNB = "DO_NOT_BUY"

TIER_RANK = {TIER_HIGH: 0, TIER_MEDIUM: 1, TIER_LOW: 2, TIER_DNB: 3}


class MerchantReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seller_id: str
    seller_username: str
    account_type: Optional[str] = None
    feedback_score: Optional[int] = None
    positive_feedback_pct: Optional[float] = None
    top_rated: Optional[bool] = None
    reliability_tier: str
    risk_flags: List[str]
    negotiation_context: str
    raw_signals: Dict[str, Any]


# ── Risk flags ────────────────────────────────────────────────────────────────

def _compute_risk_flags(
    feedback_score: Optional[int],
    positive_feedback_pct: Optional[float],
    has_return_policy: Optional[bool],
    account_type: Optional[str],
    price_usd: Optional[float] = None,
) -> List[str]:
    flags: List[str] = []
    if feedback_score is not None and feedback_score < 10:
        flags.append("LOW_VOLUME")
    if feedback_score is not None and feedback_score < 5:
        flags.append("NEW_ACCOUNT")
    if has_return_policy is False:
        flags.append("NO_RETURNS")
    if (
        account_type
        and account_type.upper() == "INDIVIDUAL"
        and price_usd is not None
        and price_usd >= 500
    ):
        flags.append("INDIVIDUAL_HIGH_VALUE")
    return flags


# ── Tier logic (strict precedence) ───────────────────────────────────────────

def _compute_tier(
    top_rated: Optional[bool],
    positive_feedback_pct: Optional[float],
    feedback_score: Optional[int],
    risk_flags: List[str],
) -> str:
    pct = positive_feedback_pct
    score = feedback_score

    if pct is not None and pct < 90:
        return TIER_DNB
    if "NEW_ACCOUNT" in risk_flags and (pct is None or pct < 95):
        return TIER_DNB

    if top_rated and pct is not None and pct >= 98 and score is not None and score >= 500:
        return TIER_HIGH
    if pct is not None and pct >= 95 and score is not None and score >= 50:
        return TIER_MEDIUM
    return TIER_LOW


# ── Negotiation context (deterministic template) ─────────────────────────────

_CONTEXT_TEMPLATES = {
    TIER_HIGH: "Highly trusted seller ({pct}% positive, {score} reviews, Top Rated). Safe to buy with confidence.",
    TIER_MEDIUM: "Moderately trusted seller ({pct}% positive, {score} reviews). Consider reviewing return policy before purchase.",
    TIER_LOW: "Low confidence seller ({pct_display} positive, {score_display} reviews). Proceed with caution; verify item details independently.",
    TIER_DNB: "Seller does not meet reliability thresholds ({flags}). Consider alternative listings.",
}


def _build_negotiation_context(
    tier: str,
    positive_feedback_pct: Optional[float],
    feedback_score: Optional[int],
    risk_flags: List[str],
) -> str:
    pct_display = f"{positive_feedback_pct}%" if positive_feedback_pct is not None else "unknown"
    score_display = str(feedback_score) if feedback_score is not None else "unknown"
    flags_str = ", ".join(risk_flags) if risk_flags else "low feedback"
    return _CONTEXT_TEMPLATES.get(tier, _CONTEXT_TEMPLATES[TIER_LOW]).format(
        pct=positive_feedback_pct,
        score=feedback_score,
        pct_display=pct_display,
        score_display=score_display,
        flags=flags_str,
    )


# ── LLM sentiment (Phase 4, feature-flagged) ─────────────────────────────────

async def _llm_sentiment_flags(description_text: str) -> List[str]:
    """
    Optional LLM call to detect nuanced risks from item/seller text.
    Returns risk category strings or empty list on failure.
    """
    if not is_llm_sentiment_enabled() or not description_text.strip():
        return []

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a fraud-detection assistant. Analyze the seller/item text below and return "
                        'a JSON object with a single key "risks" whose value is a list of zero or more strings '
                        "from: COUNTERFEIT_RISK, SHIPPING_DELAY, DESCRIPTION_MISMATCH, COMMUNICATION_ISSUE. "
                        "Return {\"risks\": []} if none apply."
                    ),
                },
                {"role": "user", "content": description_text[:2000]},
            ],
        )
        import json
        data = json.loads(resp.choices[0].message.content or "{}")
        valid = {"COUNTERFEIT_RISK", "SHIPPING_DELAY", "DESCRIPTION_MISMATCH", "COMMUNICATION_ISSUE"}
        return [r for r in data.get("risks", []) if r in valid]
    except Exception as exc:
        logger.warning("merchant_llm_sentiment_error: %s", exc)
        return []


# ── Public API ────────────────────────────────────────────────────────────────

async def compute_merchant_report(
    seller_username: str,
    feedback_score: Optional[int] = None,
    positive_feedback_pct: Optional[float] = None,
    top_rated: Optional[bool] = None,
    feedback_rating_star: Optional[str] = None,
    account_type: Optional[str] = None,
    has_return_policy: Optional[bool] = None,
    price_usd: Optional[float] = None,
    description_text: Optional[str] = None,
) -> MerchantReport:
    """Build a MerchantReport from Finding + optional Browse signals."""
    risk_flags = _compute_risk_flags(
        feedback_score=feedback_score,
        positive_feedback_pct=positive_feedback_pct,
        has_return_policy=has_return_policy,
        account_type=account_type,
        price_usd=price_usd,
    )

    llm_flags = await _llm_sentiment_flags(description_text or "")
    risk_flags.extend(llm_flags)
    risk_flags = [f for f in risk_flags if f]

    tier = _compute_tier(top_rated, positive_feedback_pct, feedback_score, risk_flags)

    if llm_flags and "COUNTERFEIT_RISK" in llm_flags and tier != TIER_DNB:
        tier = TIER_DNB

    context = _build_negotiation_context(tier, positive_feedback_pct, feedback_score, risk_flags)

    return MerchantReport(
        seller_id=seller_username,
        seller_username=seller_username,
        account_type=account_type,
        feedback_score=feedback_score,
        positive_feedback_pct=positive_feedback_pct,
        top_rated=top_rated,
        reliability_tier=tier,
        risk_flags=risk_flags,
        negotiation_context=context,
        raw_signals={
            "feedback_rating_star": feedback_rating_star,
            "has_return_policy": has_return_policy,
            "account_type": account_type,
            "llm_sentiment_flags": llm_flags or None,
        },
    )


def tier_sort_key(report: Optional[MerchantReport]) -> int:
    """Sort key: lower is better. Items without a report sort last."""
    if report is None:
        return 99
    return TIER_RANK.get(report.reliability_tier, 99)
