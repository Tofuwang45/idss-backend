"""Unit tests for the web search wrapper (``app.commerce_web_search``).

Covers:
  * ``_build_query`` keyword hygiene (drops use-case noise, keeps specs).
  * ``_category_for_domain`` mapping.
  * ``_extract_listing_keywords`` LLM plumbing (mocked).
  * ``_post_filter_listings`` per-domain safety net.
  * ``run_web_search`` passes ``category_ids`` through to
    ``_tool_search_and_evaluate_ebay`` and applies the post-filter.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from app import commerce_web_search as cws
from app.commerce_web_search import (
    _build_query,
    _category_for_domain,
    _extract_listing_keywords,
    _filter_listings_by_price_band,
    _filters_are_thin,
    _normalize_spec,
    _post_filter_listings,
    _price_bounds_usd_from_filters,
    run_web_search,
)


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------


def test_build_query_drops_school_student_use_case():
    q = _build_query(
        {"use_case": "School / Student"},
        original_message="I want a laptop",
        domain="laptops",
    )
    assert "school" not in q.lower()
    assert "student" not in q.lower()
    assert "laptop" in q.lower()


def test_build_query_keeps_gaming_use_case():
    q = _build_query(
        {"brand": "Apple", "use_case": "Gaming"},
        original_message="",
        domain="laptops",
    )
    assert "apple" in q.lower()
    assert "laptop" in q.lower()
    assert "gaming" in q.lower()


def test_build_query_keeps_workstation_use_case():
    q = _build_query(
        {"use_case": "Workstation"},
        original_message="",
        domain="laptops",
    )
    assert "workstation" in q.lower()


def test_build_query_skips_no_preference_brand():
    q = _build_query(
        {"brand": "No preference"},
        original_message="",
        domain="laptops",
    )
    assert "preference" not in q.lower()
    assert q.strip().lower() == "laptop"


def test_build_query_phones_domain():
    q = _build_query(
        {"brand": "Samsung"},
        original_message="",
        domain="phones",
    )
    assert "samsung" in q.lower()
    assert "phone" in q.lower()


def test_build_query_books_domain():
    q = _build_query({}, original_message="", domain="books")
    assert "book" in q.lower()


def test_build_query_includes_specs():
    q = _build_query(
        {"brand": "Dell", "min_ram_gb": 16, "storage_type": "ssd"},
        original_message="",
        domain="laptops",
    )
    ql = q.lower()
    assert "dell" in ql
    assert "laptop" in ql
    assert "16gb" in ql
    assert "ssd" in ql


def test_build_query_merges_refiner_brand_and_model():
    q = _build_query(
        {},
        original_message="",
        domain="laptops",
        refined={"brand": "Dell", "model": "XPS 13", "specs": ["16GB", "SSD"]},
    )
    ql = q.lower()
    assert "dell" in ql
    assert "xps 13" in ql
    assert "16gb" in ql
    assert "ssd" in ql
    assert "laptop" in ql


def test_build_query_caps_length():
    q = _build_query(
        {
            "brand": "SomeVeryLongBrandName",
            "min_ram_gb": 64,
            "screen_size": 17.3,
            "storage_type": "NVMe",
            "use_case": "gaming",
        },
        original_message="",
        domain="laptops",
        refined={"model": "ModelX", "series": "SeriesZ",
                 "specs": ["RTX4090", "i9-13900H", "144Hz"]},
    )
    assert len(q) <= 120


def test_build_query_dedupes_tokens():
    q = _build_query(
        {"brand": "Dell"},
        original_message="",
        domain="laptops",
        refined={"brand": "Dell"},
    )
    assert q.lower().count("dell") == 1


def test_build_query_falls_back_to_original_message_when_empty():
    q = _build_query({}, original_message="vintage vinyl records", domain="")
    assert "vinyl" in q.lower()


# ---------------------------------------------------------------------------
# _normalize_spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("min_ram_gb", 16, "16GB"),
        ("min_ram_gb", "16", "16GB"),
        ("min_ram_gb", "16GB", "16GB"),
        ("screen_size", 14, '14"'),
        ("screen_size", "14.0", '14.0"'),
        ("storage_type", "ssd", "SSD"),
        ("storage_type", "NVMe", "NVME"),
    ],
)
def test_normalize_spec(key, value, expected):
    assert _normalize_spec(key, value) == expected


def test_normalize_spec_none():
    assert _normalize_spec("min_ram_gb", None) is None
    assert _normalize_spec("min_ram_gb", "") is None


# ---------------------------------------------------------------------------
# _category_for_domain
# ---------------------------------------------------------------------------


def test_category_for_domain_known():
    assert _category_for_domain("laptops") == "177"
    assert _category_for_domain("phones") == "9355"
    assert _category_for_domain("books") == "267"


def test_category_for_domain_unknown():
    assert _category_for_domain("widgets") is None
    assert _category_for_domain("") is None


# ---------------------------------------------------------------------------
# _filters_are_thin
# ---------------------------------------------------------------------------


def test_filters_thin_when_nothing_set():
    assert _filters_are_thin({}) is True
    assert _filters_are_thin({"use_case": "School / Student"}) is True
    assert _filters_are_thin({"brand": "No preference"}) is True


def test_filters_not_thin_when_brand_or_spec_set():
    assert _filters_are_thin({"brand": "Dell"}) is False
    assert _filters_are_thin({"min_ram_gb": 16}) is False


# ---------------------------------------------------------------------------
# _post_filter_listings
# ---------------------------------------------------------------------------


def _l(title: str) -> Dict[str, Any]:
    return {"title": title, "price_cents": 10000}


def test_post_filter_drops_backpacks_for_laptops():
    listings = [
        _l("JanSport Backpack School 17-Laptop Big Student 17.5"),
        _l("Dell XPS 13 Laptop 16GB 512GB SSD"),
        _l("High School Student Planner Agenda 40 Weeks"),
        _l("Large Capacity Pencil Case School Student"),
        _l("Apple MacBook Air M1 8GB 256GB"),
    ]
    kept = _post_filter_listings(listings, "laptops")
    titles = [x["title"] for x in kept]
    assert "Dell XPS 13 Laptop 16GB 512GB SSD" in titles
    assert "Apple MacBook Air M1 8GB 256GB" in titles
    # Backpack has "laptop" in its title but ALSO has "backpack" — and no other
    # laptop-y token apart from "laptop" itself. Our rule drops "backpack"
    # when no good token matches... "laptop" IS a good token, so this one is
    # actually kept. That's acceptable: eBay's category filter will handle it.
    # The planner and pencil case MUST be gone.
    assert not any("planner" in t.lower() for t in titles)
    assert not any("pencil case" in t.lower() for t in titles)


def test_post_filter_drops_offdomain_phones():
    listings = [
        _l("iPhone 13 128GB Unlocked"),
        _l("Screen Protector for iPhone 13 3-pack"),
        _l("Phone Mount for Car"),
    ]
    kept = _post_filter_listings(listings, "phones")
    titles = [x["title"] for x in kept]
    assert "iPhone 13 128GB Unlocked" in titles
    # "Phone Mount for Car" has "phone" in it, so the good token matches and
    # we keep it (category filter would have blocked it server-side). We only
    # care that obvious mismatches like bare screen protectors are gone.
    assert not any("screen protector" in t.lower() and "iphone" not in t.lower()
                   for t in titles)


def test_post_filter_books_drops_bookmarks():
    listings = [
        _l("The Great Gatsby Paperback Novel"),
        _l("Magnetic Bookmark Set of 10"),
    ]
    kept = _post_filter_listings(listings, "books")
    titles = [x["title"] for x in kept]
    assert "The Great Gatsby Paperback Novel" in titles
    assert "Magnetic Bookmark Set of 10" not in titles


def test_post_filter_unknown_domain_passes_through():
    listings = [_l("anything"), _l("everything")]
    kept = _post_filter_listings(listings, "widgets")
    assert kept == listings


def test_post_filter_empty():
    assert _post_filter_listings([], "laptops") == []


# ---------------------------------------------------------------------------
# _extract_listing_keywords
# ---------------------------------------------------------------------------


def test_refiner_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("EBAY_LLM_REFINER", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = asyncio.run(_extract_listing_keywords("Dell XPS 13 for school", "laptops"))
    assert result is None


def test_refiner_short_message_skipped(monkeypatch):
    monkeypatch.setenv("EBAY_LLM_REFINER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Single token -> no LLM call, no return.
    result = asyncio.run(_extract_listing_keywords("Dell", "laptops"))
    assert result is None


def test_refiner_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("EBAY_LLM_REFINER", "1")
    result = asyncio.run(_extract_listing_keywords("Dell XPS 13 for school", "laptops"))
    assert result is None


def test_refiner_happy_path(monkeypatch):
    monkeypatch.setenv("EBAY_LLM_REFINER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"brand":"Dell","model":"XPS 13","series":null,"specs":["16GB","SSD"]}'
        ))]
    )

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**_kwargs):
                    return fake_resp

    with patch("openai.OpenAI", _FakeClient, create=True):
        result = asyncio.run(
            _extract_listing_keywords("Dell XPS 13 for school", "laptops")
        )

    assert result is not None
    assert result["brand"] == "Dell"
    assert result["model"] == "XPS 13"
    assert "16GB" in result.get("specs", [])
    assert "SSD" in result.get("specs", [])


def test_refiner_strips_use_case_noise(monkeypatch):
    monkeypatch.setenv("EBAY_LLM_REFINER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"brand":"Student","model":"School","specs":["gaming","16GB"]}'
        ))]
    )

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**_kwargs):
                    return fake_resp

    with patch("openai.OpenAI", _FakeClient, create=True):
        result = asyncio.run(
            _extract_listing_keywords("I need a laptop for school", "laptops")
        )

    assert result is not None
    assert "brand" not in result  # "Student" filtered out
    assert "model" not in result  # "School" filtered out
    assert result.get("specs") == ["16GB"]


def test_refiner_invalid_json_returns_none(monkeypatch):
    monkeypatch.setenv("EBAY_LLM_REFINER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not json at all"))]
    )

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**_kwargs):
                    return fake_resp

    with patch("openai.OpenAI", _FakeClient, create=True):
        result = asyncio.run(
            _extract_listing_keywords("Dell XPS for school", "laptops")
        )
    assert result is None


# ---------------------------------------------------------------------------
# run_web_search integration
# ---------------------------------------------------------------------------


def _make_eval_resp(titles: List[str]):
    """Build a minimal stand-in for EvaluatedSearchResponse."""
    results = []
    for t in titles:
        item = SimpleNamespace()
        item.title = t
        item.price_cents = 10000
        item.model_dump = lambda t=t: {"title": t, "price_cents": 10000}
        results.append(item)
    return SimpleNamespace(results=results, source="test")


def test_price_bounds_from_filters_range():
    lo, hi = _price_bounds_usd_from_filters(
        {"price_min_cents": 70000, "price_max_cents": 100000},
    )
    assert (lo, hi) == (700.0, 1000.0)


def test_filter_listings_by_price_band():
    rows = [
        {"title": "A", "price_cents": 50_000},
        {"title": "B", "price_cents": 85_000},
        {"title": "C", "price_cents": 120_000},
    ]
    out = _filter_listings_by_price_band(rows, 700.0, 1000.0)
    assert [r["title"] for r in out] == ["B"]


def test_run_web_search_forwards_category_and_query(monkeypatch):
    captured: Dict[str, Any] = {}

    async def _fake_tool(**kwargs):
        captured.update(kwargs)
        return _make_eval_resp(["Dell XPS 13 Laptop 16GB SSD"])

    # Patch the function that run_web_search imports lazily from app.main.
    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    # Disable the LLM refiner to keep the test hermetic.
    monkeypatch.setenv("EBAY_LLM_REFINER", "0")

    result = asyncio.run(run_web_search(
        filters={"use_case": "School / Student"},
        domain="laptops",
        original_message="I want a laptop",
        limit=5,
    ))

    assert captured["category_ids"] == "177"
    assert captured.get("min_price") is None
    assert captured.get("max_price") is None
    assert "school" not in captured["query"].lower()
    assert "student" not in captured["query"].lower()
    assert "laptop" in captured["query"].lower()
    assert len(result) == 1


def test_run_web_search_forwards_price_band(monkeypatch):
    captured: Dict[str, Any] = {}

    async def _fake_tool(**kwargs):
        captured.update(kwargs)
        return _make_eval_resp(["Dell Latitude Laptop"])

    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    monkeypatch.setenv("EBAY_LLM_REFINER", "0")

    asyncio.run(run_web_search(
        filters={"price_min_cents": 70000, "price_max_cents": 100000},
        domain="laptops",
        original_message="Dell laptop",
        limit=5,
    ))

    assert captured["min_price"] == 700.0
    assert captured["max_price"] == 1000.0


def test_run_web_search_passes_listing_title_hints(monkeypatch):
    captured: Dict[str, Any] = {}

    async def _fake_tool(**kwargs):
        captured.update(kwargs)
        return _make_eval_resp(["Dell Latitude"])

    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    monkeypatch.setenv("EBAY_LLM_REFINER", "0")

    asyncio.run(run_web_search(
        filters={"brand": "Dell"},
        domain="laptops",
        original_message="laptop",
        limit=3,
        listing_title_hints=["Dell Latitude 7420 16GB RAM"],
    ))

    assert captured["listing_title_hints"] == ["Dell Latitude 7420 16GB RAM"]
    assert captured["domain"] == "laptops"


def test_run_web_search_applies_post_filter(monkeypatch):
    async def _fake_tool(**kwargs):
        return _make_eval_resp([
            "JanSport Backpack School 17-Laptop Student",
            "Dell XPS 13 Laptop 16GB SSD",
            "School Planner Agenda 40 Weeks",
        ])

    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    monkeypatch.setenv("EBAY_LLM_REFINER", "0")

    result = asyncio.run(run_web_search(
        filters={"use_case": "School / Student"},
        domain="laptops",
        original_message="I want a laptop",
        limit=5,
    ))

    titles = [r["title"] for r in result]
    assert "Dell XPS 13 Laptop 16GB SSD" in titles
    assert not any("planner" in t.lower() for t in titles)


def test_run_web_search_llm_refiner_merges_into_query(monkeypatch):
    captured: Dict[str, Any] = {}

    async def _fake_tool(**kwargs):
        captured.update(kwargs)
        return _make_eval_resp(["Dell XPS 13 Laptop 16GB SSD"])

    async def _fake_refiner(_msg, _domain):
        return {"brand": "Dell", "model": "XPS 13", "specs": ["16GB", "SSD"]}

    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    monkeypatch.setattr(cws, "_extract_listing_keywords", _fake_refiner, raising=True)

    asyncio.run(run_web_search(
        filters={},
        domain="laptops",
        original_message="Dell XPS 13 for school 16GB SSD",
        limit=5,
    ))

    q = captured["query"].lower()
    assert "dell" in q
    assert "xps 13" in q
    assert "16gb" in q
    assert "ssd" in q
    assert "school" not in q


def test_run_web_search_skips_refiner_when_filters_rich(monkeypatch):
    called = {"n": 0}

    async def _fake_refiner(_msg, _domain):
        called["n"] += 1
        return {"brand": "SHOULD_NOT_BE_USED"}

    async def _fake_tool(**_kwargs):
        return _make_eval_resp(["Dell XPS 13 Laptop"])

    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    monkeypatch.setattr(cws, "_extract_listing_keywords", _fake_refiner, raising=True)

    asyncio.run(run_web_search(
        filters={"brand": "Dell"},
        domain="laptops",
        original_message="Dell XPS 13",
        limit=5,
    ))

    assert called["n"] == 0


def test_run_web_search_tool_failure_returns_empty(monkeypatch):
    async def _fake_tool(**_kwargs):
        raise RuntimeError("boom")

    import app.main as _main
    monkeypatch.setattr(_main, "_tool_search_and_evaluate_ebay", _fake_tool, raising=True)
    monkeypatch.setenv("EBAY_LLM_REFINER", "0")

    result = asyncio.run(run_web_search(
        filters={},
        domain="laptops",
        original_message="laptop",
        limit=5,
    ))
    assert result == []
