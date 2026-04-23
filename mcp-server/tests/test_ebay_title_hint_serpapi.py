"""SerpAPI sold-comp augmentation using listing / product title hints."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

from app.ebay_seller import (
    augment_sold_rows_with_serpapi_title_hints,
    compact_listing_title_for_ebay_comp_query,
)


def test_compact_title_strips_bundle_suffix():
    raw = "Dell Latitude 7420 14 Laptop | 16GB RAM | 512GB SSD | Win 11"
    q = compact_listing_title_for_ebay_comp_query(raw)
    assert "|" not in q
    assert "latitude" in q.lower()
    assert "7420" in q.lower()


def test_augment_calls_serpapi_per_distinct_hint():
    existing: List[Dict[str, Any]] = []

    async def fake_serpapi(keywords: str, **kwargs: Any):
        return (
            [{
                "title": f"sold {keywords[:20]}",
                "sold_price_cents": 80000,
                "end_time": "2025-06-01T12:00:00Z",
                "listing_type": "FixedPrice",
                "source": "serpapi",
            }],
            {"stage": "serpapi_Sold", "final_count": 1},
        )

    with patch("app.serpapi_ebay.is_serpapi_configured", return_value=True):
        with patch("app.serpapi_ebay.fetch_sold_comps_serpapi", new=AsyncMock(side_effect=fake_serpapi)):
            out, diag = asyncio.run(
                augment_sold_rows_with_serpapi_title_hints(
                    base_query="dell laptop",
                    title_hints=[
                        "Dell Latitude 7420 14\" Laptop 16GB",
                        "Dell Inspiron 15 3000",  # distinct from base + each other
                    ],
                    existing_rows=existing,
                    condition=None,
                    min_price=None,
                    max_price=2000.0,
                    category_id="177",
                    domain="laptops",
                    max_hints=3,
                )
            )

    assert len(out) == 2
    assert diag["rows_added"] == 2
    assert len(diag["hints_used"]) == 2
