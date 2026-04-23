"""
SerpAPI eBay engine — sold / completed comparables fetcher.

Complements `app.ebay_seller.fetch_completed_items_with_diagnostics` with a
second-opinion source that works even when the eBay Finding API is
rate-limited (error 10001) or scoped too strictly.

Returns rows in the exact schema that `app.market_analysis.parse_comparables`
expects, so the FMV pipeline can consume SerpAPI data with no changes:

    {
        "title": str,
        "sold_price_cents": int,
        "condition": Optional[str],
        "end_time": str (ISO-8601) | None,
        "listing_type": "FixedPrice" | "Auction",
        "selling_state": "EndedWithSales",
        "source": "serpapi",
        "thumbnail": Optional[str],
        "url": Optional[str],
    }

Env:
    SERPAPI_API_KEY        — required; if unset, fetches short-circuit to []
    FMV_SERPAPI_ENABLED    — "1" (default) to allow fallback, "0" to disable
    SERPAPI_TIMEOUT        — request timeout seconds (default 10)
    SERPAPI_CACHE_TTL      — cache TTL seconds for successful responses (default 900)
    SERPAPI_CACHE_FAIL_TTL — cache TTL seconds for empty / failed (default 90)
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.ebay_seller import filter_relevant, _canonicalize_comparable_query

logger = logging.getLogger("mcp.serpapi_ebay")

_SERPAPI_URL = "https://serpapi.com/search.json"

# Domain -> eBay leaf category used when the caller passes our internal
# domain string (e.g. "laptops"). Numeric strings are forwarded verbatim.
_DOMAIN_CATEGORY_FALLBACK: Dict[str, str] = {
    "laptops": "177",
    "phones": "9355",
    "books": "267",
}

# eBay condition → SerpAPI LH_ItemCondition id.
# https://developer.ebay.com/devzone/finding/callref/enums/conditionIdList.html
_CONDITION_TO_ID: Dict[str, str] = {
    "new": "1000",
    "used": "3000",
    "refurbished": "2000|2500",
}

# Module-level cache keyed on (keywords, show_only, category_id, condition, min_price, max_price).
_cache: Dict[
    Tuple[str, str, Optional[str], Optional[str], Optional[float], Optional[float]],
    Dict[str, Any],
] = {}


def _serpapi_api_key() -> str:
    """Return trimmed API key; tolerate quoted values in .env."""
    raw = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def is_serpapi_configured() -> bool:
    """True when the feature flag is enabled and an API key is present."""
    enabled = os.getenv("FMV_SERPAPI_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return False
    return bool(_serpapi_api_key())


def _build_diag(
    *,
    stage: str,
    query_used: str,
    condition_used: Optional[str],
    min_price_used: Optional[float] = None,
    max_price_used: Optional[float] = None,
    category_id: Optional[str],
) -> Dict[str, Any]:
    """Shape matches ebay_seller._build_comparable_diagnostics for symmetry."""
    return {
        "stage": stage,
        "source": "serpapi",
        "query_used": query_used,
        "condition_used": condition_used,
        "min_price_used": min_price_used,
        "max_price_used": max_price_used,
        "category_id": category_id,
        "api_items_raw_count": 0,
        "price_parse_kept_count": 0,
        "relevance_kept_count": 0,
        "final_count": 0,
        "failure_reason": None,
        "error_message": None,
    }


def _resolve_category_id(category_id: Optional[str], domain: Optional[str]) -> Optional[str]:
    """Accept either a raw eBay category id or our internal domain string."""
    if category_id and str(category_id).strip():
        return str(category_id).strip()
    if domain:
        return _DOMAIN_CATEGORY_FALLBACK.get(domain.strip().lower())
    return None


_SOLD_DATE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # "Sold Apr 15, 2026"
    re.compile(r"sold\s+(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+(?P<yr>\d{4})", re.IGNORECASE),
    # "Apr 15, 2026"
    re.compile(r"^(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+(?P<yr>\d{4})$"),
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_sold_date(raw: Any) -> Optional[str]:
    """Best-effort ISO-8601 string from a free-form SerpAPI sold-date field."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    s = str(raw).strip()
    if not s:
        return None
    # Already ISO-ish?
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        pass
    for pat in _SOLD_DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        mon = _MONTHS.get(m.group("mon").lower())
        if not mon:
            continue
        try:
            dt = datetime(int(m.group("yr")), mon, int(m.group("day")), tzinfo=timezone.utc)
            return dt.isoformat()
        except (TypeError, ValueError):
            continue
    return None


def _extract_price_cents(item: Dict[str, Any]) -> Optional[int]:
    """SerpAPI returns `price` either as a dict `{raw, extracted}` or a string."""
    price = item.get("price")
    if isinstance(price, dict):
        val = price.get("extracted")
        if val is not None:
            try:
                return round(float(val) * 100)
            except (TypeError, ValueError):
                pass
        raw = price.get("raw")
        if isinstance(raw, str):
            m = re.search(r"[\d,]+\.?\d*", raw)
            if m:
                try:
                    return round(float(m.group(0).replace(",", "")) * 100)
                except ValueError:
                    return None
    elif isinstance(price, (int, float)):
        try:
            return round(float(price) * 100)
        except (TypeError, ValueError):
            return None
    elif isinstance(price, str):
        m = re.search(r"[\d,]+\.?\d*", price)
        if m:
            try:
                return round(float(m.group(0).replace(",", "")) * 100)
            except ValueError:
                return None
    return None


def _normalize_row(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a SerpAPI organic_result row to our comparable shape, or None."""
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    price_cents = _extract_price_cents(item)
    if not title or not price_cents:
        return None

    # Prefer explicit sold date when available; SerpAPI wording varies across
    # engine versions (`sold_date`, `ended_date`, `date`, ...). Fall back to a
    # bare snippet string that may include "Sold <date>".
    end_time_raw = (
        item.get("sold_date")
        or item.get("ended_date")
        or item.get("date")
        or item.get("time")
    )
    if not end_time_raw:
        # Some responses put the sold date into a snippet / subtitle.
        for key in ("snippet", "subtitle", "secondary_info"):
            val = item.get(key)
            if isinstance(val, str) and ("sold" in val.lower() or "ended" in val.lower()):
                end_time_raw = val
                break
    end_time_iso = _parse_sold_date(end_time_raw)

    condition = item.get("condition")
    if isinstance(condition, dict):
        condition = condition.get("name") or condition.get("value")
    if condition is not None:
        condition = str(condition).strip() or None

    listing_type_raw = (item.get("buying_options") or item.get("buying_format") or "")
    if isinstance(listing_type_raw, list):
        listing_type_raw = " ".join(str(x) for x in listing_type_raw)
    listing_type = (
        "Auction" if "auction" in str(listing_type_raw).lower() else "FixedPrice"
    )

    return {
        "title": title,
        "sold_price_cents": int(price_cents),
        "condition": condition,
        "end_time": end_time_iso,
        "listing_type": listing_type,
        "selling_state": "EndedWithSales",
        "source": "serpapi",
        "thumbnail": item.get("thumbnail") or item.get("image"),
        "url": item.get("link") or item.get("product_link"),
    }


async def _serpapi_get(
    *,
    keywords: str,
    show_only: str,
    category_id: Optional[str],
    condition: Optional[str],
    limit: int,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """One SerpAPI request + normalized rows + diagnostics."""
    diag = _build_diag(
        stage=f"serpapi_{show_only.lower()}",
        query_used=keywords,
        condition_used=condition,
        min_price_used=min_price,
        max_price_used=max_price,
        category_id=category_id,
    )
    api_key = _serpapi_api_key()
    if not api_key:
        diag["failure_reason"] = "missing_api_key"
        return [], diag

    # _sop must be a SerpAPI-supported code (see https://serpapi.com/ebay-sort-options).
    # eBay URL sort "13" is not supported and returns 400 Unsupported _sop: 13.
    _sop = (os.getenv("FMV_SERPAPI_EBAY_SOP") or "12").strip() or "12"
    params: Dict[str, Any] = {
        "engine": "ebay",
        "ebay_domain": "ebay.com",
        "api_key": api_key,
        "show_only": show_only,          # "Sold" or "Complete"
        "_sop": _sop,                    # default 12 = Best Match; 1 = ending soonest
        "_ipg": str(min(max(int(limit), 1), 100)),
    }
    if keywords:
        params["_nkw"] = keywords[:120]
    if category_id:
        params["category_id"] = str(category_id)
    if min_price is not None:
        params["_udlo"] = str(int(min_price))
    if max_price is not None:
        params["_udhi"] = str(int(max_price))
    cond_id = _CONDITION_TO_ID.get((condition or "").strip().lower())
    if cond_id:
        params["LH_ItemCondition"] = cond_id

    timeout = float(os.getenv("SERPAPI_TIMEOUT", "10"))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(_SERPAPI_URL, params=params)
    except Exception as exc:
        diag["failure_reason"] = "request_error"
        diag["error_message"] = str(exc)
        logger.warning("serpapi_request_error: %s", exc)
        return [], diag

    if resp.status_code != 200:
        diag["failure_reason"] = "http_error"
        diag["error_message"] = f"status={resp.status_code} body={(resp.text or '')[:200]}"
        logger.warning("serpapi_http: status=%s snippet=%s",
                       resp.status_code, (resp.text or "")[:200].replace("\n", " "))
        return [], diag

    try:
        payload = resp.json()
    except Exception as exc:
        diag["failure_reason"] = "json_error"
        diag["error_message"] = str(exc)
        return [], diag

    if isinstance(payload.get("error"), str):
        diag["failure_reason"] = "api_error"
        diag["error_message"] = payload["error"]
        logger.warning("serpapi_api_error: %s", payload["error"])
        return [], diag

    organic = payload.get("organic_results") or []
    diag["api_items_raw_count"] = len(organic)

    parsed: List[Dict[str, Any]] = []
    for row in organic:
        norm = _normalize_row(row)
        if norm:
            parsed.append(norm)
    diag["price_parse_kept_count"] = len(parsed)

    relevant = filter_relevant(parsed, keywords, threshold=0.28) if keywords else parsed
    diag["relevance_kept_count"] = len(relevant)
    diag["final_count"] = len(relevant)

    if not relevant:
        if diag["api_items_raw_count"] == 0:
            diag["failure_reason"] = "no_items"
        elif diag["price_parse_kept_count"] == 0:
            diag["failure_reason"] = "no_priced_items"
        else:
            diag["failure_reason"] = "all_filtered_by_relevance"

    return relevant, diag


async def fetch_sold_comps_serpapi(
    keywords: str,
    condition: Optional[str] = None,
    max_price: Optional[float] = None,
    limit: int = 50,
    *,
    min_price: Optional[float] = None,
    category_id: Optional[str] = None,
    domain: Optional[str] = None,
    min_comps_threshold: int = 5,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Fetch sold comparables via SerpAPI with bounded fallback stages.

    Stages (in order), first to hit ``min_comps_threshold`` wins:
      1. show_only=Sold, with condition + max_price
      2. show_only=Sold, no condition
      3. show_only=Complete (includes ended-without-sale)
      4. show_only=Sold with canonicalized query
    """
    if not is_serpapi_configured():
        diag = _build_diag(
            stage="serpapi_disabled",
            query_used=keywords,
            condition_used=condition,
            min_price_used=min_price,
            max_price_used=max_price,
            category_id=category_id,
        )
        diag["failure_reason"] = "missing_api_key"
        return [], diag

    cat_id = _resolve_category_id(category_id, domain)
    cache_key = (
        (keywords or "").lower(),
        "serpapi",
        cat_id,
        (condition or "").lower() or None,
        float(min_price) if min_price is not None else None,
        float(max_price) if max_price is not None else None,
    )
    now = time.time()
    ttl_ok = int(os.getenv("SERPAPI_CACHE_TTL", "900"))
    ttl_fail = int(os.getenv("SERPAPI_CACHE_FAIL_TTL", "90"))
    cached = _cache.get(cache_key)
    if cached and cached.get("expires_at", 0) > now:
        return cached["rows"], cached["diag"]

    canon = _canonicalize_comparable_query(keywords)
    stages: List[Tuple[str, str, Optional[str], Optional[float], Optional[float]]] = [
        ("Sold", keywords, condition, min_price, max_price),
        ("Sold", keywords, None, min_price, max_price),
        ("Complete", keywords, None, min_price, max_price),
        ("Sold", canon, None, None, None),
    ]

    best_rows: List[Dict[str, Any]] = []
    best_diag: Optional[Dict[str, Any]] = None
    stage_diags: List[Dict[str, Any]] = []

    for show_only, q, cond, mn, mx in stages:
        rows, diag = await _serpapi_get(
            keywords=q,
            show_only=show_only,
            category_id=cat_id,
            condition=cond,
            limit=limit,
            min_price=mn,
            max_price=mx,
        )
        stage_diags.append(diag)
        if best_diag is None or len(rows) > len(best_rows):
            best_rows = rows
            best_diag = diag
        if len(rows) >= min_comps_threshold:
            diag["stages"] = stage_diags
            _cache[cache_key] = {
                "rows": rows,
                "diag": diag,
                "expires_at": now + ttl_ok,
            }
            return rows, diag
        if diag.get("failure_reason") in {"missing_api_key", "http_error", "api_error"}:
            # Transient / configuration errors won't be fixed by retrying.
            break

    if best_diag is None:
        best_diag = _build_diag(
            stage="serpapi_Sold",
            query_used=keywords,
            condition_used=condition,
            min_price_used=min_price,
            max_price_used=max_price,
            category_id=cat_id,
        )
        best_diag["failure_reason"] = "no_stage_executed"
    best_diag["stages"] = stage_diags
    if not best_rows and not best_diag.get("failure_reason"):
        best_diag["failure_reason"] = "no_comparables_after_fallback"

    _cache[cache_key] = {
        "rows": best_rows,
        "diag": best_diag,
        "expires_at": now + (ttl_fail if not best_rows else ttl_ok),
    }
    return best_rows, best_diag
