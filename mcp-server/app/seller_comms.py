"""
LLM Communication Pipeline — draft buyer→seller messages.

Detects missing listing attributes, then uses an LLM (gpt-4o-mini) to
generate a polite, professional message the buyer can send.  Falls back
to template interpolation if the LLM call fails or is disabled.

Feature flag:
  DEAL_LLM_COMMS  — 0 (default, template only) | 1 (LLM enabled)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.deal_engine import DealDecision, TargetListing

logger = logging.getLogger("mcp.seller_comms")

# ── Schemas ───────────────────────────────────────────────────────────────────

class CommsRequest(BaseModel):
    target_listing: TargetListing
    decision: DealDecision
    missing_attributes: List[str]


class CommsResponse(BaseModel):
    suggested_message_text: str
    flagged_risks: List[str]
    tone: str  # "inquiry" | "negotiation" | "caution"


# ── Ideal-listing attribute schemas per product category ──────────────────────

IDEAL_ATTRIBUTES: Dict[str, List[str]] = {
    "phone": [
        "battery_health", "icloud_lock_status", "original_accessories",
        "carrier_unlock_status", "screen_condition", "warranty_status",
    ],
    "laptop": [
        "battery_cycle_count", "screen_condition", "keyboard_layout",
        "warranty_status", "original_charger", "storage_type",
    ],
    "tablet": [
        "battery_health", "screen_condition", "original_accessories",
        "warranty_status", "activation_lock_status",
    ],
    "default": [
        "warranty_status", "original_packaging", "return_accepted",
        "item_condition_detail",
    ],
}


def _is_llm_enabled() -> bool:
    return os.getenv("DEAL_LLM_COMMS", "0") == "1"


def detect_missing_attributes(
    listing: TargetListing,
    category: str = "default",
) -> List[str]:
    """Compare listing fields against the ideal schema and return missing ones."""
    ideal = IDEAL_ATTRIBUTES.get(category, IDEAL_ATTRIBUTES["default"])

    known: set[str] = set()
    if listing.condition:
        known.add("item_condition_detail")
    if listing.accessories:
        known.add("original_accessories")
        known.add("original_charger")
        known.add("original_packaging")

    return [attr for attr in ideal if attr not in known]


def _determine_tone(decision: DealDecision) -> str:
    if decision.action == "NEGOTIATE":
        return "negotiation"
    mre = decision.fmv
    if mre and mre.deal_score == "OVERPRICED":
        return "caution"
    return "inquiry"


# ── Template fallback ─────────────────────────────────────────────────────────

def _template_message(
    listing: TargetListing,
    decision: DealDecision,
    missing: List[str],
) -> CommsResponse:
    """Generate a message without calling an LLM."""
    tone = _determine_tone(decision)
    lines: List[str] = []
    risks: List[str] = []

    lines.append(f"Hi, I'm interested in your listing \"{listing.title}\".")

    if missing:
        pretty = ", ".join(a.replace("_", " ") for a in missing)
        lines.append(f"Before I proceed, could you provide details on: {pretty}?")

    if decision.action == "NEGOTIATE" and decision.target_action_price_cents:
        target_usd = decision.target_action_price_cents / 100.0
        lines.append(
            f"Based on recent market data, I'd like to propose ${target_usd:.2f}. "
            "Would you be open to discussing the price?"
        )

    mre = listing.merchant_report or {}
    risk_flags = mre.get("risk_flags", [])
    if risk_flags:
        risks.extend(risk_flags)
        if "NO_RETURNS" in risk_flags:
            lines.append("I noticed there's no return policy listed — could you confirm whether returns are accepted?")

    lines.append("Thank you for your time!")

    return CommsResponse(
        suggested_message_text="\n\n".join(lines),
        flagged_risks=risks,
        tone=tone,
    )


# ── LLM path ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a buyer's assistant drafting a message to an eBay seller.
You MUST only use the data provided. Do not invent facts.

Context:
- Item: {title}
- Action: {action}
- Target price: ${target_price}
- Fair Market Value: ${fmv}
- Missing info needed: {missing}
- Seller tier: {tier}
- Risk flags: {risks}

Generate a polite, professional message that:
1. Asks about the missing attributes listed above
2. If action is NEGOTIATE, proposes the target price with justification
3. If risk flags are non-empty, tactfully asks for verification

Return ONLY valid JSON: {{"message": "<string>", "risks": ["<string>", ...]}}
"""


async def _llm_message(
    listing: TargetListing,
    decision: DealDecision,
    missing: List[str],
) -> Optional[CommsResponse]:
    """Call gpt-4o-mini to draft a seller message. Returns None on failure."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI()

        mre = listing.merchant_report or {}
        target_price = (
            f"{decision.target_action_price_cents / 100:.2f}"
            if decision.target_action_price_cents else "N/A"
        )

        prompt = _SYSTEM_PROMPT.format(
            title=listing.title,
            action=decision.action,
            target_price=target_price,
            fmv=f"{decision.fmv.fair_market_value_cents / 100:.2f}",
            missing=", ".join(missing) if missing else "none",
            tier=mre.get("reliability_tier", "UNKNOWN"),
            risks=", ".join(mre.get("risk_flags", [])) or "none",
        )

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Draft the message now."},
            ],
        )

        raw = json.loads(resp.choices[0].message.content or "{}")
        msg = raw.get("message", "")
        risks = raw.get("risks", [])
        if not isinstance(risks, list):
            risks = []

        tone = _determine_tone(decision)
        return CommsResponse(
            suggested_message_text=msg,
            flagged_risks=[str(r) for r in risks],
            tone=tone,
        )
    except Exception as exc:
        logger.warning("seller_comms_llm_error: %s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_comms(
    listing: TargetListing,
    decision: DealDecision,
    missing_attributes: Optional[List[str]] = None,
    category: str = "default",
) -> CommsResponse:
    """
    Generate a buyer→seller communication message.

    Uses LLM when DEAL_LLM_COMMS=1 and falls back to templates on failure.
    """
    if missing_attributes is None:
        missing_attributes = detect_missing_attributes(listing, category)

    if _is_llm_enabled():
        result = await _llm_message(listing, decision, missing_attributes)
        if result is not None:
            return result
        logger.info("seller_comms_llm_fallback: using template")

    return _template_message(listing, decision, missing_attributes)
