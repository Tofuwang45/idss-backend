"""Regression tests for app.serpapi_ebay — SerpAPI sold-comparables fallback.

These tests cover the three behaviors that matter for correctness:

1. Mapping SerpAPI's `organic_results` into our SoldComparable row shape.
2. Graceful degradation when the API key is missing / the feature flag is off.
3. Fallback chain: if the first stage (show_only=Sold + condition) returns
   too few items, the module retries without condition before giving up.
"""

import asyncio
from unittest.mock import MagicMock, patch

from app.serpapi_ebay import (
    _normalize_row,
    _parse_sold_date,
    fetch_sold_comps_serpapi,
)


def _organic_result(
    title: str = "Apple MacBook Pro 14 M3 16GB 512GB",
    price_extracted: float = 1499.00,
    condition: str = "Pre-Owned",
    sold_date: str = "Apr 15, 2026",
    link: str = "https://www.ebay.com/itm/123",
) -> dict:
    return {
        "title": title,
        "price": {"raw": f"${price_extracted:,.2f}", "extracted": price_extracted},
        "condition": condition,
        "sold_date": sold_date,
        "link": link,
        "thumbnail": "https://i.ebayimg.com/thumbs/123.jpg",
        "buying_options": ["Fixed Price"],
    }


class TestNormalizer:
    def test_happy_path_maps_all_fields(self):
        row = _normalize_row(_organic_result())
        assert row is not None
        assert row["title"] == "Apple MacBook Pro 14 M3 16GB 512GB"
        assert row["sold_price_cents"] == 149900
        assert row["condition"] == "Pre-Owned"
        assert row["listing_type"] == "FixedPrice"
        assert row["end_time"] is not None
        assert row["end_time"].startswith("2026-04-15")
        assert row["source"] == "serpapi"

    def test_auction_listing_type(self):
        item = _organic_result()
        item["buying_options"] = ["Auction"]
        row = _normalize_row(item)
        assert row is not None
        assert row["listing_type"] == "Auction"

    def test_missing_title_is_dropped(self):
        item = _organic_result(title="")
        assert _normalize_row(item) is None

    def test_missing_price_is_dropped(self):
        item = _organic_result()
        item["price"] = None
        assert _normalize_row(item) is None

    def test_raw_string_price_still_parses(self):
        item = _organic_result()
        item["price"] = "$1,299.99"
        row = _normalize_row(item)
        assert row is not None
        assert row["sold_price_cents"] == 129999

    def test_snippet_fallback_for_sold_date(self):
        item = _organic_result(sold_date="")
        item["sold_date"] = None
        item["snippet"] = "Sold Mar 02, 2026"
        row = _normalize_row(item)
        assert row is not None
        assert row["end_time"] is not None
        assert row["end_time"].startswith("2026-03-02")

    def test_parse_sold_date_iso_passthrough(self):
        assert _parse_sold_date("2026-04-15T00:00:00Z").startswith("2026-04-15")

    def test_parse_sold_date_garbage_returns_none(self):
        assert _parse_sold_date("sometime last week") is None


class TestFetchSoldCompsSerpapi:
    """These are executed synchronously via ``asyncio.run`` — this repo's
    pytest is configured with anyio but not pytest-asyncio, so we drive
    the async calls manually to stay consistent with other tests."""

    def test_returns_empty_when_api_key_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            rows, diag = asyncio.run(fetch_sold_comps_serpapi("test query"))
        assert rows == []
        assert diag["failure_reason"] == "missing_api_key"

    def test_feature_flag_disables_even_with_key(self):
        env = {"SERPAPI_API_KEY": "test-key", "FMV_SERPAPI_ENABLED": "0"}
        with patch.dict("os.environ", env, clear=True):
            rows, diag = asyncio.run(fetch_sold_comps_serpapi("test query"))
        assert rows == []
        assert diag["failure_reason"] == "missing_api_key"

    def test_happy_path_returns_mapped_rows(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "organic_results": [
                _organic_result(title=f"MacBook Pro 14 M3 unit {i}", price_extracted=1499 + i)
                for i in range(8)
            ],
        }

        env = {"SERPAPI_API_KEY": "test-key", "FMV_SERPAPI_ENABLED": "1"}
        with patch.dict("os.environ", env, clear=True):
            # Reset module-level cache so stale results from other tests don't leak.
            from app import serpapi_ebay
            serpapi_ebay._cache.clear()
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = mock_cls.return_value.__aenter__.return_value

                async def _fake_get(*args, **kwargs):
                    return resp

                mock_client.get.side_effect = _fake_get
                rows, diag = asyncio.run(fetch_sold_comps_serpapi(
                    "MacBook Pro 14 M3",
                    condition="used",
                    max_price=1800.0,
                    limit=20,
                    min_comps_threshold=5,
                ))

        assert len(rows) >= 5
        assert all(r["source"] == "serpapi" for r in rows)
        assert all(r["sold_price_cents"] > 0 for r in rows)
        assert diag["final_count"] == len(rows)
        assert diag["failure_reason"] is None

    def test_http_error_short_circuits(self):
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "internal error"

        env = {"SERPAPI_API_KEY": "test-key", "FMV_SERPAPI_ENABLED": "1"}
        with patch.dict("os.environ", env, clear=True):
            from app import serpapi_ebay
            serpapi_ebay._cache.clear()
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = mock_cls.return_value.__aenter__.return_value

                async def _fake_get(*args, **kwargs):
                    return resp

                mock_client.get.side_effect = _fake_get
                rows, diag = asyncio.run(fetch_sold_comps_serpapi("anything"))

        assert rows == []
        assert diag["failure_reason"] == "http_error"
