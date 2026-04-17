"""Unit tests for natural-language eBay query parsing."""

from app.ebay_query_parser import parse_natural_ebay_query


def test_dell_laptop_under_1000():
    p = parse_natural_ebay_query("Dell laptop under 1000")
    assert p.clean_query == "Dell laptop"
    assert p.max_price == 1000.0
    assert p.condition is None


def test_under_dollar_with_commas():
    p = parse_natural_ebay_query("under $1,200 macbook pro")
    assert "macbook" in p.clean_query.lower()
    assert "under" not in p.clean_query.lower()
    assert p.max_price == 1200.0


def test_explicit_max_price_overrides_parsed():
    p = parse_natural_ebay_query("Dell laptop under 1000", explicit_max_price=800)
    assert p.clean_query == "Dell laptop"
    assert p.max_price == 800


def test_min_of_two_budgets():
    p = parse_natural_ebay_query("phone under 900 below 700")
    assert p.max_price == 700.0


def test_used_strips_and_sets_condition():
    p = parse_natural_ebay_query("used ThinkPad x1 under 500")
    assert p.condition == "used"
    assert "used" not in p.clean_query.lower()
    assert p.max_price == 500.0


def test_explicit_condition_overrides():
    p = parse_natural_ebay_query("used macbook", explicit_condition="new")
    assert p.condition == "new"
    # "used" may still be stripped as word before explicit wins on condition field
    assert "macbook" in p.clean_query.lower()


def test_k_suffix():
    p = parse_natural_ebay_query("monitor under 2k")
    assert p.max_price == 2000.0


def test_empty_after_strip_fallback():
    p = parse_natural_ebay_query("under 400")
    assert p.clean_query == "items"
    assert p.max_price == 400.0
