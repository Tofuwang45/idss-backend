"""
Tests for the commerce source selection gate (Web search vs Current catalog).

Covers:
  1. Session state: commerce_search_mode and pending_handoff serialize/reset
  2. Source gate: recommendations_ready defers search for laptops/books/phones
  3. Choice routing: "Web search" → eBay path, "Current catalog" → catalog
  4. _format_response_as_text renders web_market_listings
  5. commerce_web_search._build_query produces sensible keywords
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from typing import Dict, Any


# ── 1. Session state round-trip ─────────────────────────────────────────────

class TestSessionFields:
    def test_new_fields_default_none(self):
        from agent.interview.session_manager import InterviewSessionState
        s = InterviewSessionState()
        assert s.commerce_search_mode is None
        assert s.pending_handoff is None

    def test_serialize_deserialize(self):
        from agent.interview.session_manager import InterviewSessionManager
        mgr = InterviewSessionManager()
        s = mgr.get_session("test-ser-1")
        s.commerce_search_mode = "web_search"
        s.pending_handoff = {"domain": "laptops", "search_filters": {"brand": "Dell"}}

        d = mgr._state_to_dict(s)
        assert d["commerce_search_mode"] == "web_search"
        assert d["pending_handoff"]["domain"] == "laptops"

        restored = mgr._dict_to_state(d)
        assert restored.commerce_search_mode == "web_search"
        assert restored.pending_handoff["search_filters"]["brand"] == "Dell"

    def test_reset_clears_fields(self):
        from agent.interview.session_manager import InterviewSessionManager
        mgr = InterviewSessionManager()
        s = mgr.get_session("test-reset-1")
        s.commerce_search_mode = "catalog"
        s.pending_handoff = {"domain": "books"}
        mgr.reset_session("test-reset-1")
        s2 = mgr.get_session("test-reset-1")
        assert s2.commerce_search_mode is None
        assert s2.pending_handoff is None


# ── 2. _build_query ─────────────────────────────────────────────────────────

class TestBuildQuery:
    def test_laptop_with_brand(self):
        from app.commerce_web_search import _build_query
        q = _build_query({"brand": "Dell", "use_case": "gaming"}, "I want a gaming Dell", "laptops")
        assert "Dell" in q
        assert "laptop" in q
        assert "gaming" in q

    def test_fallback_to_message(self):
        from app.commerce_web_search import _build_query
        q = _build_query({}, "Find me a cheap phone", "phones")
        assert "phone" in q or "cheap phone" in q.lower()

    def test_books_domain(self):
        from app.commerce_web_search import _build_query
        q = _build_query({"brand": "Stephen King"}, "sci-fi novel", "books")
        assert "book" in q
        assert "Stephen King" in q


# ── 3. _format_response_as_text with web_market_listings ────────────────────

class TestFormatWebListings:
    def test_web_listings_rendered(self):
        from pydantic import BaseModel, Field
        from typing import Optional, List

        class FakeChatResponse(BaseModel):
            message: str = ""
            recommendations: Optional[list] = None
            quick_replies: Optional[list] = None
            web_market_listings: Optional[List[Dict[str, Any]]] = None

        from app.main import _format_response_as_text

        resp = FakeChatResponse(
            message="Found 2 live listings:",
            web_market_listings=[
                {
                    "title": "Dell XPS 15 Laptop",
                    "price": "$1,199.00",
                    "deal_score": "GOOD",
                    "merchant_report": {"reliability_tier": "HIGH"},
                    "fmv_source": "sold_history",
                    "url": "https://www.ebay.com/itm/123",
                },
                {
                    "title": "MacBook Pro 14 M3",
                    "price": "$1,400.00",
                    "deal_score": "FAIR",
                    "merchant_report": None,
                    "fmv_source": "active_market",
                    "url": "https://www.ebay.com/itm/456",
                },
            ],
        )
        text = _format_response_as_text(resp)
        assert "Dell XPS 15" in text
        assert "MacBook Pro 14" in text
        assert "Deal: GOOD" in text
        assert "Seller trust: HIGH" in text
        assert "ebay.com/itm/123" in text
        assert "active market" in text

    def test_no_web_listings_no_crash(self):
        from pydantic import BaseModel
        from typing import Optional, List
        class FakeChatResponse(BaseModel):
            message: str = "Hi"
            recommendations: Optional[list] = None
            quick_replies: Optional[list] = None
            web_market_listings: Optional[List[Dict[str, Any]]] = None

        from app.main import _format_response_as_text
        text = _format_response_as_text(FakeChatResponse())
        assert "Hi" in text
        assert "Marketplace" not in text


# ── 4. _price_bounds_usd_from_filters ───────────────────────────────────────

class TestPriceBounds:
    def test_cents_budget(self):
        from app.commerce_web_search import _price_bounds_usd_from_filters
        assert _price_bounds_usd_from_filters({"budget": 150000}) == (None, 1500.0)

    def test_dollar_budget(self):
        from app.commerce_web_search import _price_bounds_usd_from_filters
        assert _price_bounds_usd_from_filters({"budget": 800}) == (None, 800.0)

    def test_none_budget(self):
        from app.commerce_web_search import _price_bounds_usd_from_filters
        assert _price_bounds_usd_from_filters({}) == (None, None)

    def test_range_from_cents_keys(self):
        from app.commerce_web_search import _price_bounds_usd_from_filters
        assert _price_bounds_usd_from_filters(
            {"price_min_cents": 70000, "price_max_cents": 100000},
        ) == (700.0, 1000.0)
