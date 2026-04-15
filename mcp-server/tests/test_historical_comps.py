"""Regression tests for sold-comparable diagnostics and fallback stages."""

from unittest.mock import MagicMock, patch

import pytest

from app.ebay_seller import fetch_completed_items_with_diagnostics


def _completed_items_response(titles: list[str], price: str = "999.00") -> dict:
    items = []
    for idx, t in enumerate(titles, start=1):
        items.append(
            {
                "itemId": [f"X{idx:03d}"],
                "title": [t],
                "sellingStatus": [
                    {
                        "sellingState": ["EndedWithSales"],
                        "currentPrice": [{"@currencyId": "USD", "__value__": price}],
                    }
                ],
                "listingInfo": [{"listingType": ["FixedPrice"], "endTime": ["2026-04-01T00:00:00.000Z"]}],
                "condition": [{"conditionDisplayName": ["Used"]}],
            }
        )
    return {"findCompletedItemsResponse": [{"searchResult": [{"item": items}]}]}


class TestComparableDiagnostics:
    @pytest.mark.asyncio
    async def test_stage_fallback_recovers_comps(self):
        """
        strict (condition=new) returns zero, no_condition returns items.
        Expect stage=no_condition and non-empty comps.
        """
        strict_resp = MagicMock()
        strict_resp.status_code = 200
        strict_resp.json.return_value = _completed_items_response([])

        relaxed_resp = MagicMock()
        relaxed_resp.status_code = 200
        relaxed_resp.json.return_value = _completed_items_response(
            ["Apple MacBook Pro 14 M3 16GB 512GB", "MacBook Pro 14 M3 Space Black"],
            "1499.00",
        )

        with patch.dict("os.environ", {"EBAY_APP_ID": "test-key"}, clear=False):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = mock_cls.return_value.__aenter__.return_value
                mock_client.get.side_effect = [strict_resp, relaxed_resp]
                rows, diag = await fetch_completed_items_with_diagnostics(
                    keywords="MacBook Pro 14 M3",
                    condition="new",
                    max_price=1600.0,
                    limit=50,
                    min_comps_threshold=1,
                )

        assert len(rows) >= 1
        assert diag["stage"] == "no_condition"
        assert diag["final_count"] >= 1
        assert isinstance(diag.get("stages"), list)
        assert len(diag["stages"]) >= 2

    @pytest.mark.asyncio
    async def test_all_filtered_by_relevance_reason(self):
        bad_resp = MagicMock()
        bad_resp.status_code = 200
        bad_resp.json.return_value = _completed_items_response(["Tempered glass screen protector"], "19.99")

        with patch.dict("os.environ", {"EBAY_APP_ID": "test-key"}, clear=False):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = mock_cls.return_value.__aenter__.return_value
                # All stages return unrelated items.
                mock_client.get.side_effect = [bad_resp, bad_resp, bad_resp, bad_resp]
                rows, diag = await fetch_completed_items_with_diagnostics(
                    keywords="MacBook Pro 14 M3",
                    condition="used",
                    max_price=2000.0,
                    limit=50,
                    min_comps_threshold=5,
                )

        assert rows == []
        assert diag["failure_reason"] == "all_filtered_by_relevance"
        assert diag["relevance_kept_count"] == 0

    @pytest.mark.asyncio
    async def test_missing_app_id_reason(self):
        with patch.dict("os.environ", {}, clear=True):
            rows, diag = await fetch_completed_items_with_diagnostics(
                keywords="MacBook Pro 14 M3",
                condition="used",
                max_price=2000.0,
                limit=50,
            )
        assert rows == []
        assert diag["failure_reason"] == "missing_app_id"
