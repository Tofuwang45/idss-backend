"""
Natural-language helpers for eBay search queries.

Extracts max price (and optional condition) from free text so callers can pass
a single string like "Dell laptop under 1000" and still get structured filters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ParsedEbayQuery:
    """Result of parsing a natural-language eBay search string."""

    clean_query: str
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    condition: Optional[str] = None


def _collapse_ws(s: str) -> str:
    return " ".join(s.split()).strip()


def _money_to_float(whole: str, frac: Optional[str], k_suffix: Optional[str]) -> float:
    w = whole.replace(",", "")
    if frac:
        val = float(f"{w}.{frac}")
    else:
        val = float(w)
    if k_suffix and k_suffix.lower() == "k":
        val *= 1000.0
    return val


# Optional $, number with optional commas, optional cents, optional "k" thousands
_NUM = (
    r"\$?\s*"
    r"(?P<whole>\d{1,3}(?:,\d{3})*|\d+)"
    r"(?:\.(?P<frac>\d{1,2}))?"
    r"\s*(?P<k>[kK])?"
)


def _price_from_match(m: re.Match) -> float:
    d = m.groupdict()
    return _money_to_float(d["whole"], d.get("frac"), d.get("k"))


def _budget_patterns() -> List[Tuple[re.Pattern, str]]:
    """
    (compiled_regex, label) — label only for tests/debug.
    Longer / more specific phrases first where it matters.
    """
    n = _NUM
    specs = [
        rf"\bno\s+more\s+than\s+{n}\b",
        rf"\bless\s+than\s+{n}\b",
        rf"\bat\s+most\s+{n}\b",
        rf"\bup\s+to\s+{n}\b",
        rf"\bmaximum\s+{n}\b",
        rf"\bmax\s+{n}\b",
        rf"\bbudget\s+of\s+{n}\b",
        rf"\bbudget\s+{n}\b",
        rf"\bbelow\s+{n}\b",
        rf"\bunder\s+{n}\b",
        rf"{n}\s+or\s+less\b",
        rf"\bcheaper\s+than\s+{n}\b",
    ]
    return [(re.compile(p, re.IGNORECASE), p[:40]) for p in specs]


_COND_STRIPS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bbrand\s+new\b", re.I), "new"),
    (re.compile(r"\bfactory\s+refurbished\b", re.I), "refurbished"),
    (re.compile(r"\bcertified\s+refurbished\b", re.I), "refurbished"),
    (re.compile(r"\brefurbished\b", re.I), "refurbished"),
    (re.compile(r"\bused\b", re.I), "used"),
    (re.compile(r"\bnew\b", re.I), "new"),
]


def parse_natural_ebay_query(
    query: str,
    explicit_max_price: Optional[float] = None,
    explicit_condition: Optional[str] = None,
    explicit_min_price: Optional[float] = None,
) -> ParsedEbayQuery:
    """
    Strip budget / condition phrases from *query* and return structured fields.

    - If explicit_max_price is set, it wins over any parsed ceiling.
    - If explicit_min_price is set, it wins over any parsed floor.
    - If explicit_condition is set, it wins over parsed condition.
    - Multiple budget phrases: use the **minimum** (tightest ceiling).
    """
    original = (query or "").strip()
    if not original:
        return ParsedEbayQuery(
            clean_query="",
            min_price=explicit_min_price,
            max_price=explicit_max_price,
            condition=explicit_condition,
        )

    text = original
    prices: List[float] = []

    for pat, _ in _budget_patterns():
        while True:
            m = pat.search(text)
            if not m:
                break
            try:
                prices.append(_price_from_match(m))
            except (ValueError, TypeError, KeyError):
                pass
            text = text[: m.start()] + " " + text[m.end() :]

    parsed_max = min(prices) if prices else None
    max_price = explicit_max_price if explicit_max_price is not None else parsed_max
    min_price = explicit_min_price

    parsed_cond: Optional[str] = None
    for cre, cond_val in _COND_STRIPS:
        mm = cre.search(text)
        if mm:
            parsed_cond = cond_val
            text = cre.sub(" ", text)
            break

    condition = explicit_condition if explicit_condition else parsed_cond

    cleaned = _collapse_ws(text)
    if not cleaned:
        cleaned = "items"

    return ParsedEbayQuery(
        clean_query=cleaned,
        min_price=min_price,
        max_price=max_price,
        condition=condition,
    )
