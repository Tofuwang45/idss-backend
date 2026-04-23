"""Sold-comp / SerpAPI primary keywords must follow the live eBay search query."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

from app.main import EbayResult, EbaySearchResponse, _tool_search_and_evaluate_ebay


def _sold_row(title: str, cents: int, day: str = "15") -> Dict[str, Any]:
    return {
        "title": title,
        "sold_price_cents": cents,
        "condition": "used",
        "end_time": f"2025-06-{day}T12:00:00Z",
        "listing_type": "FixedPrice",
        "selling_state": "EndedWithSales",
        "source": "ebay_finding",
    }


def test_fetch_sold_comps_uses_parsed_ebay_query_not_raw_input():
    """NL parser may rewrite `q`; Finding + primary SerpAPI must use `search_resp.query`."""
    parsed = "dell latitude laptop"
    captured: Dict[str, Any] = {}

    async def fake_search_ebay(**kwargs: Any) -> EbaySearchResponse:
        assert kwargs.get("q") == "raw user string budget $900"
        return EbaySearchResponse(
            query=parsed,
            min_price=50.0,
            max_price=900.0,
            condition=None,
            results=[
                EbayResult(
                    title="Dell Latitude 7420 14",
                    price="$450.00",
                    price_cents=45000,
                    condition="Used",
                    url="https://www.ebay.com/itm/1",
                    item_id="1",
                ),
            ],
            search_url="https://www.ebay.com/sch/i.html",
            source="api",
        )

    async def fake_fetch_sold(
        keywords: str,
        condition: Any = None,
        max_price: Any = None,
        limit: int = 50,
        *,
        min_price: Any = None,
        category_id: Any = None,
        domain: Any = None,
        min_comps_threshold: int = 5,
    ) -> Any:
        captured["keywords"] = keywords
        rows = [
            _sold_row("sold a", 40000, "01"),
            _sold_row("sold b", 42000, "02"),
            _sold_row("sold c", 44000, "03"),
            _sold_row("sold d", 46000, "04"),
            _sold_row("sold e", 48000, "05"),
        ]
        return (
            rows,
            {
                "stage": "strict",
                "query_used": "canonicalized_different",
                "failure_reason": None,
                "sources": [{"source": "ebay_finding", "count": 5}],
            },
        )

    async def run() -> None:
        with patch("app.main.search_ebay", new=AsyncMock(side_effect=fake_search_ebay)):
            with patch(
                "app.ebay_seller.fetch_sold_comps_multi_source",
                new=AsyncMock(side_effect=fake_fetch_sold),
            ):
                with patch("app.ebay_seller.augment_sold_rows_with_serpapi_title_hints", new=AsyncMock()):
                    resp = await _tool_search_and_evaluate_ebay(
                        query="raw user string budget $900",
                        min_price=50.0,
                        max_price=900.0,
                        limit=5,
                        category_ids="177",
                        domain="laptops",
                    )

        assert captured.get("keywords") == parsed
        assert resp.query == parsed
        assert resp.results
        assert resp.results[0].fmv_query_used == parsed

    asyncio.run(run())
