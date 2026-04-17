"""
Commerce Web Search — thin wrapper that converts agent interview filters
into an evaluated eBay search (MRE + FMV + deal engine).

Called from ``agent.chat_endpoint.process_chat`` when the user picks
**Web search** instead of the curated catalog.  Returns a list of dicts
that can be dropped straight into ``ChatResponse.web_market_listings``.

The label is "web search" (not "eBay search") because the same interface
will later support additional marketplaces.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mcp.commerce_web_search")


def _build_query(
    filters: Dict[str, Any],
    original_message: str,
    domain: str,
) -> str:
    """Build a concise keyword query from interview slots + raw user text."""
    parts: list[str] = []

    brand = filters.get("brand")
    if brand:
        parts.append(str(brand))

    if domain == "laptops":
        parts.append("laptop")
    elif domain == "phones":
        parts.append("phone")
    elif domain == "books":
        parts.append("book")

    use_case = filters.get("use_case")
    if use_case:
        parts.append(str(use_case))

    for spec_key in ("min_ram_gb", "screen_size", "storage_type"):
        v = filters.get(spec_key)
        if v:
            parts.append(str(v))

    query = " ".join(parts).strip()
    if len(query) < 8:
        query = original_message.strip()
    return query[:120]


def _max_price_from_filters(filters: Dict[str, Any]) -> Optional[float]:
    budget = filters.get("budget") or filters.get("price_max_cents")
    if budget is None:
        return None
    try:
        val = int(budget)
        return val / 100.0 if val > 10_000 else float(val)
    except (TypeError, ValueError):
        return None


async def run_web_search(
    filters: Dict[str, Any],
    domain: str,
    original_message: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Execute an evaluated eBay search and return serialised listing dicts.

    Returns an empty list on any unrecoverable error so the caller can
    degrade gracefully.
    """
    try:
        from app.main import _tool_search_and_evaluate_ebay
    except ImportError:
        logger.error("commerce_web_search: cannot import _tool_search_and_evaluate_ebay")
        return []

    query = _build_query(filters, original_message, domain)
    max_price = _max_price_from_filters(filters)

    condition = None
    if filters.get("condition"):
        condition = str(filters["condition"]).lower()

    logger.info(
        "web_search_start: query=%r max_price=%s condition=%s limit=%d",
        query, max_price, condition, limit,
    )

    try:
        resp = await _tool_search_and_evaluate_ebay(
            query=query,
            condition=condition,
            max_price=max_price,
            limit=limit,
        )
        listings = [item.model_dump() for item in resp.results]
        logger.info(
            "web_search_done: query=%r returned=%d source=%s",
            query, len(listings), resp.source,
        )
        return listings
    except Exception as exc:
        logger.warning("web_search_error: %s", exc, exc_info=True)
        return []
