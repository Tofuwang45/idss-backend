"""
eBay Browse API client for seller profile enrichment.

Manages OAuth Application Access Tokens (client credentials grant) and
fetches item-level seller data via GET /buy/browse/v1/item/{itemId}.

Env vars:
  EBAY_OAUTH_CLIENT_ID      — eBay OAuth app client ID
  EBAY_OAUTH_CLIENT_SECRET  — eBay OAuth app client secret
  EBAY_ENVIRONMENT          — SANDBOX | PRODUCTION (default SANDBOX)
"""

import os
import time
import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, Tuple

import re

import httpx

logger = logging.getLogger("mcp.ebay_seller")


# ── Relevance filtering ──────────────────────────────────────────────────────

_STOP_WORDS = frozenset({
    "a", "an", "the", "for", "and", "or", "in", "on", "of", "to", "with",
    "is", "it", "by", "at", "from", "as", "be", "this", "that", "new", "used",
})

_RE_NONALPHA = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lower-case, strip punctuation, remove stop words."""
    words = _RE_NONALPHA.sub(" ", text.lower()).split()
    return {w for w in words if w and w not in _STOP_WORDS}


_ACCESSORY_INDICATORS = frozenset({
    "case", "cover", "protector", "screen", "film", "skin", "sleeve",
    "charger", "cable", "adapter", "stand", "mount", "holder", "strap",
    "replacement", "repair", "part", "lcd", "digitizer", "battery",
    "tool", "kit", "tempered", "glass",
})


def relevance_score(query: str, title: str) -> float:
    """
    Relevance between search query and item title.
    Uses query recall + Jaccard, with a penalty if the title contains
    accessory-indicator tokens that are absent from the query.
    Returns 0.0–1.0.
    """
    q_tokens = _tokenize(query)
    t_tokens = _tokenize(title)
    if not q_tokens:
        return 1.0
    overlap = q_tokens & t_tokens
    recall = len(overlap) / len(q_tokens)
    union = q_tokens | t_tokens
    jaccard = len(overlap) / len(union) if union else 0.0
    base = 0.6 * recall + 0.4 * jaccard

    extra = t_tokens - q_tokens
    accessory_hits = extra & _ACCESSORY_INDICATORS
    if accessory_hits:
        penalty = min(len(accessory_hits) * 0.25, 0.5)
        base *= (1.0 - penalty)

    return base


def filter_relevant(
    rows: List[Dict[str, Any]],
    query: str,
    threshold: float = 0.45,
    title_key: str = "title",
) -> List[Dict[str, Any]]:
    """Drop rows whose title has low relevance to the search query."""
    if not query:
        return rows
    return [r for r in rows if relevance_score(query, r.get(title_key, "")) >= threshold]


def _canonicalize_comparable_query(query: str) -> str:
    """
    Relaxed fallback query for sold-comparable retrieval.
    Removes noisy modifiers while preserving core brand/model/storage tokens.
    """
    tokens = [t for t in _tokenize(query) if t]
    if not tokens:
        return query
    remove = {
        "mint", "excellent", "bundle", "sealed", "open", "box",
        "edition", "brand", "new", "used", "refurbished", "latest",
    }
    keep = [t for t in tokens if t not in remove and not t.isdigit()]
    # Keep query usable even if aggressive removal empties token list.
    if not keep:
        keep = tokens
    return " ".join(keep[:8])

_EBAY_URLS = {
    "PRODUCTION": {
        "auth": "https://api.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.ebay.com/buy/browse/v1/item",
    },
    "SANDBOX": {
        "auth": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.sandbox.ebay.com/buy/browse/v1/item",
    },
}

_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}
_comps_cache: Dict[Tuple[str, Optional[str], Optional[float], int, int], Dict[str, Any]] = {}


def _get_env() -> str:
    return os.getenv("EBAY_ENVIRONMENT", "SANDBOX").upper()


def _urls() -> Dict[str, str]:
    env = _get_env()
    return _EBAY_URLS.get(env, _EBAY_URLS["SANDBOX"])


def _browse_api_root() -> str:
    """REST root for Buy APIs (Browse search, item detail)."""
    return "https://api.sandbox.ebay.com" if _get_env() == "SANDBOX" else "https://api.ebay.com"


def is_browse_configured() -> bool:
    return bool(
        os.getenv("EBAY_OAUTH_CLIENT_ID")
        and os.getenv("EBAY_OAUTH_CLIENT_SECRET")
    )


async def get_application_access_token() -> Optional[str]:
    """
    Obtain (or return cached) eBay Application Access Token via
    client_credentials grant.  Tokens are valid for ~2 hours.
    """
    client_id = os.getenv("EBAY_OAUTH_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    auth_url = _urls()["auth"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                auth_url,
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.warning("ebay_oauth_failed: %d %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 7200) - 120
        return _token_cache["token"]
    except Exception as exc:
        logger.warning("ebay_oauth_error: %s", exc)
        return None


async def _request_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    max_retries: int = 3,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[httpx.Response]:
    """GET with exponential backoff on transient failures."""
    for attempt in range(max_retries):
        try:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.info("ebay_browse_rate_limit: retry in %.1fs", wait)
                await asyncio.sleep(wait)
                continue
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt == max_retries - 1:
                logger.warning("ebay_browse_request_failed: %s", exc)
                return None
            wait = (2 ** attempt) + random.uniform(0, 1)
            await asyncio.sleep(wait)
        return None


def _item_summary_listing_url(item: Dict[str, Any]) -> str:
    """Buyer-facing URL for an item summary row."""
    web = item.get("itemWebUrl")
    if isinstance(web, str) and web.startswith("http"):
        return web
    iid = item.get("itemId")
    if isinstance(iid, str) and "|" in iid:
        parts = iid.split("|")
        if len(parts) >= 2 and parts[1].isdigit():
            return f"https://www.ebay.com/itm/{parts[1]}"
    href = item.get("itemHref")
    return href if isinstance(href, str) else ""


def _rows_from_item_summaries(summaries: List[Any], lim: int) -> List[Dict[str, Any]]:
    """Map Browse API itemSummaries to row dicts for EbayResult construction."""
    rows: List[Dict[str, Any]] = []
    for item in summaries[:lim]:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        price_block = item.get("price") or {}
        pval = price_block.get("value")
        price_str = None
        if pval is not None:
            try:
                price_str = f"${float(pval):,.2f}"
            except (TypeError, ValueError):
                price_str = str(pval)

        ship_label = None
        for opt in item.get("shippingOptions") or []:
            if not isinstance(opt, dict):
                continue
            if opt.get("shippingCostType") == "FREE":
                ship_label = "Free"
                break
            sc = opt.get("shippingCost") or {}
            if str(sc.get("value", "")) in ("0", "0.00", "0.0"):
                ship_label = "Free"
                break

        seller = item.get("seller") or {}
        uname = seller.get("username") if isinstance(seller, dict) else None
        fb_pct = None
        fb_raw = seller.get("feedbackPercentage") if isinstance(seller, dict) else None
        if fb_raw is not None:
            try:
                fb_pct = float(fb_raw)
            except (TypeError, ValueError):
                pass
        fb_score = None
        fs_raw = seller.get("feedbackScore") if isinstance(seller, dict) else None
        if fs_raw is not None:
            try:
                fb_score = int(fs_raw)
            except (TypeError, ValueError):
                pass

        price_cents = None
        if pval is not None:
            try:
                price_cents = round(float(pval) * 100)
            except (TypeError, ValueError):
                pass

        iid = item.get("itemId")
        rows.append({
            "title": title,
            "price": price_str,
            "price_cents": price_cents,
            "condition": item.get("condition"),
            "url": _item_summary_listing_url(item),
            "shipping": ship_label,
            "item_id": str(iid) if iid else None,
            "seller_username": uname if uname else None,
            "feedback_score": fb_score,
            "positive_feedback_pct": fb_pct,
            "top_rated_seller": item.get("topRatedBuyingExperience"),
        })
    return rows


async def search_item_summaries(
    q: str,
    limit: int = 10,
    max_price: Optional[float] = None,
    condition: Optional[str] = None,
    sort: Optional[str] = "best-match",
) -> List[Dict[str, Any]]:
    """
    Keyword search via Browse API GET /buy/browse/v1/item_summary/search.

    Uses OAuth application token (same as item detail). Separate quota from
    the legacy Finding API — useful when Finding returns error 10001 rate limits.
    """
    if not is_browse_configured():
        return []

    token = await get_application_access_token()
    if not token:
        return []

    root = _browse_api_root()
    url = f"{root}/buy/browse/v1/item_summary/search"
    lim = min(max(int(limit), 1), 200)
    params: Dict[str, Any] = {
        "q": (q or "")[:100],
        "limit": str(lim),
    }
    if sort == "price-low":
        params["sort"] = "price"

    filter_parts: List[str] = []
    if max_price is not None:
        filter_parts.append(f"price:[0..{int(max_price)}]")
        filter_parts.append("priceCurrency:USD")
    if condition == "new":
        filter_parts.append("conditions:{NEW}")
    elif condition == "used":
        filter_parts.append("conditions:{USED}")
    if filter_parts:
        params["filter"] = ",".join(filter_parts)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }

    async def _do_search(
        client: httpx.AsyncClient,
        req_params: Dict[str, Any],
    ) -> tuple[Optional[httpx.Response], Optional[Dict[str, Any]]]:
        r = await _request_with_backoff(client, url, headers, params=req_params)
        if r is None or r.status_code != 200:
            return r, None
        try:
            return r, r.json()
        except Exception as exc:
            logger.warning("ebay_browse_search_json: %s", exc)
            return r, None

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp, payload = await _do_search(client, params)
        if resp is None:
            logger.warning("ebay_browse_search_failed: no response for query=%r", (q or "")[:80])
            return []
        if resp.status_code != 200:
            logger.warning(
                "ebay_browse_search_http: status=%s query=%r snippet=%s",
                resp.status_code,
                (q or "")[:80],
                (resp.text or "")[:500].replace("\n", " "),
            )
            return []
        if payload is None:
            return []

        summaries = payload.get("itemSummaries") or []
        if not summaries and filter_parts:
            logger.warning(
                "ebay_browse_search_retry_loose: query=%r strict filters returned no items; retrying keyword-only",
                (q or "")[:80],
            )
            loose: Dict[str, Any] = {
                "q": (q or "")[:100],
                "limit": str(lim),
            }
            if sort == "price-low":
                loose["sort"] = "price"
            resp2, payload2 = await _do_search(client, loose)
            if resp2 is not None and resp2.status_code == 200 and payload2 is not None:
                summaries = payload2.get("itemSummaries") or []

        if not summaries:
            logger.warning(
                "ebay_browse_search_zero_items: query=%r total_field=%r",
                (q or "")[:80],
                (payload.get("total", "0") if payload else "0"),
            )
            return []

    rows = _rows_from_item_summaries(summaries, lim)
    rows = filter_relevant(rows, q)
    logger.info("ebay_browse_search_ok: query=%r rows=%d", (q or "")[:80], len(rows))
    return rows


def _finding_host() -> str:
    """Return the correct eBay Finding API host for the current environment."""
    return (
        "svcs.sandbox.ebay.com" if _get_env() == "SANDBOX"
        else "svcs.ebay.com"
    )


def _build_comparable_diagnostics(
    *,
    stage: str,
    query_used: str,
    condition_used: Optional[str],
    max_price_used: Optional[float],
) -> Dict[str, Any]:
    return {
        "stage": stage,
        "query_used": query_used,
        "condition_used": condition_used,
        "max_price_used": max_price_used,
        "api_items_raw_count": 0,
        "sold_state_kept_count": 0,
        "price_parse_kept_count": 0,
        "relevance_kept_count": 0,
        "final_count": 0,
        "failure_reason": None,
        "error_id": None,
        "error_message": None,
    }


def _extract_finding_error(resp: httpx.Response) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort parse of eBay Finding error payload."""
    try:
        data = resp.json()
    except Exception:
        return None, None

    # eBay Finding often returns:
    # {"errorMessage":[{"error":[{"errorId":["10001"],"message":[...]}]}]}
    for block in data.get("errorMessage") or []:
        if not isinstance(block, dict):
            continue
        for err in block.get("error") or []:
            if not isinstance(err, dict):
                continue
            eid = err.get("errorId")
            if isinstance(eid, list) and eid:
                eid = str(eid[0])
            elif eid is not None:
                eid = str(eid)
            else:
                eid = None
            msg = err.get("message")
            if isinstance(msg, list) and msg:
                msg = str(msg[0])
            elif msg is not None:
                msg = str(msg)
            else:
                msg = None
            return eid, msg

    return None, None


async def _fetch_completed_items_once(
    *,
    keywords: str,
    condition: Optional[str],
    max_price: Optional[float],
    limit: int,
    relevance_threshold: float,
    stage: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Single-stage fetch for sold comparables with detailed diagnostics."""
    diag = _build_comparable_diagnostics(
        stage=stage,
        query_used=keywords,
        condition_used=condition,
        max_price_used=max_price,
    )

    app_id = os.getenv("EBAY_APP_ID", "")
    if not app_id:
        diag["failure_reason"] = "missing_app_id"
        logger.warning("fetch_completed_items: EBAY_APP_ID not set")
        return [], diag

    host = _finding_host()
    url = f"https://{host}/services/search/FindingService/v1"
    params: Dict[str, str] = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": (keywords or "")[:350],
        "paginationInput.entriesPerPage": str(min(int(limit), 100)),
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
    }

    filt_idx = 1
    if max_price is not None:
        params[f"itemFilter({filt_idx}).name"] = "MaxPrice"
        params[f"itemFilter({filt_idx}).value"] = str(max_price)
        params[f"itemFilter({filt_idx}).paramName"] = "Currency"
        params[f"itemFilter({filt_idx}).paramValue"] = "USD"
        filt_idx += 1
    if condition == "new":
        params[f"itemFilter({filt_idx}).name"] = "Condition"
        params[f"itemFilter({filt_idx}).value"] = "New"
    elif condition == "used":
        params[f"itemFilter({filt_idx}).name"] = "Condition"
        params[f"itemFilter({filt_idx}).value"] = "Used"

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, params=params)
    except Exception as exc:
        diag["failure_reason"] = "request_error"
        logger.warning("fetch_completed_items_error: %s", exc)
        return [], diag

    if resp.status_code != 200:
        err_id, err_msg = _extract_finding_error(resp)
        diag["error_id"] = err_id
        diag["error_message"] = err_msg
        diag["failure_reason"] = "http_error"
        if err_id == "10001":
            diag["failure_reason"] = "rate_limited"
        logger.warning(
            "fetch_completed_items_http: status=%s error_id=%s message=%s snippet=%s",
            resp.status_code,
            err_id,
            err_msg,
            (resp.text or "")[:300].replace("\n", " "),
        )
        return [], diag

    try:
        data = resp.json()
    except Exception:
        diag["failure_reason"] = "json_error"
        logger.warning("fetch_completed_items_json_error")
        return [], diag

    items_raw: List[Any] = []
    for resp_block in data.get("findCompletedItemsResponse") or []:
        if not isinstance(resp_block, dict):
            continue
        sr = resp_block.get("searchResult")
        if isinstance(sr, list):
            for sr_block in sr:
                if isinstance(sr_block, dict):
                    items_raw.extend(sr_block.get("item") or [])
    diag["api_items_raw_count"] = len(items_raw)

    sold_state_items: List[Dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        selling_status = item.get("sellingStatus")
        if isinstance(selling_status, list) and selling_status:
            selling_status = selling_status[0]
        if not isinstance(selling_status, dict):
            continue
        selling_state_raw = selling_status.get("sellingState")
        if isinstance(selling_state_raw, list) and selling_state_raw:
            selling_state_raw = selling_state_raw[0]
        if selling_state_raw == "EndedWithSales":
            sold_state_items.append(item)
    diag["sold_state_kept_count"] = len(sold_state_items)

    parsed_rows: List[Dict[str, Any]] = []
    for item in sold_state_items:
        selling_status = item.get("sellingStatus")
        if isinstance(selling_status, list) and selling_status:
            selling_status = selling_status[0]
        if not isinstance(selling_status, dict):
            continue

        price_block = selling_status.get("currentPrice")
        if isinstance(price_block, list) and price_block:
            price_block = price_block[0]
        price_val = price_block.get("__value__") if isinstance(price_block, dict) else None
        price_cents = None
        if price_val is not None:
            try:
                price_cents = round(float(price_val) * 100)
            except (TypeError, ValueError):
                pass
        if price_cents is None:
            continue

        title_raw = item.get("title")
        if isinstance(title_raw, list) and title_raw:
            title_raw = title_raw[0]

        cond_raw = item.get("condition")
        if isinstance(cond_raw, list) and cond_raw:
            cond_raw = cond_raw[0]
        if isinstance(cond_raw, dict):
            cond_raw = cond_raw.get("conditionDisplayName")
            if isinstance(cond_raw, list) and cond_raw:
                cond_raw = cond_raw[0]

        listing_info = item.get("listingInfo")
        if isinstance(listing_info, list) and listing_info:
            listing_info = listing_info[0]
        end_time_raw = None
        listing_type_raw = None
        if isinstance(listing_info, dict):
            et = listing_info.get("endTime")
            if isinstance(et, list) and et:
                et = et[0]
            end_time_raw = et
            lt = listing_info.get("listingType")
            if isinstance(lt, list) and lt:
                lt = lt[0]
            listing_type_raw = lt

        parsed_rows.append({
            "title": str(title_raw or ""),
            "sold_price_cents": price_cents,
            "condition": str(cond_raw) if cond_raw else None,
            "end_time": str(end_time_raw) if end_time_raw else None,
            "listing_type": str(listing_type_raw) if listing_type_raw else "FixedPrice",
            "selling_state": "EndedWithSales",
        })
    diag["price_parse_kept_count"] = len(parsed_rows)

    filtered_rows = filter_relevant(parsed_rows, keywords, threshold=relevance_threshold)
    diag["relevance_kept_count"] = len(filtered_rows)
    diag["final_count"] = len(filtered_rows)

    if not filtered_rows:
        if diag["api_items_raw_count"] == 0:
            diag["failure_reason"] = "no_items"
        elif diag["sold_state_kept_count"] == 0:
            diag["failure_reason"] = "no_sold_state_items"
        elif diag["price_parse_kept_count"] == 0:
            diag["failure_reason"] = "no_priced_items"
        elif diag["relevance_kept_count"] == 0:
            diag["failure_reason"] = "all_filtered_by_relevance"
        else:
            diag["failure_reason"] = "unknown_empty"

    return filtered_rows, diag


async def fetch_completed_items_with_diagnostics(
    keywords: str,
    condition: Optional[str] = None,
    max_price: Optional[float] = None,
    limit: int = 50,
    min_comps_threshold: int = 5,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Fetch sold comparables with bounded fallback stages and diagnostics.
    Stages: strict -> no_condition -> no_max_price -> canonicalized.
    """
    cache_key = (
        keywords or "",
        condition,
        max_price,
        int(limit),
        int(min_comps_threshold),
        os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper(),
        os.getenv("EBAY_APP_ID") or "",
    )
    now = time.time()
    cache_ttl = int(os.getenv("FMV_COMPS_CACHE_TTL", "900"))
    cache_fail_ttl = int(os.getenv("FMV_COMPS_CACHE_FAIL_TTL", "90"))
    cached = _comps_cache.get(cache_key)
    if cached and cached.get("expires_at", 0) > now:
        return cached["rows"], cached["diag"]

    stages: List[Tuple[str, str, Optional[str], Optional[float], float]] = [
        ("strict", keywords, condition, max_price, 0.30),
        ("no_condition", keywords, None, max_price, 0.30),
        ("no_max_price", keywords, None, None, 0.28),
        ("canonicalized", _canonicalize_comparable_query(keywords), None, None, 0.25),
    ]

    stage_diags: List[Dict[str, Any]] = []
    best_rows: List[Dict[str, Any]] = []
    best_diag: Optional[Dict[str, Any]] = None

    for stage_name, q, cond, mx, rel_th in stages:
        rows, diag = await _fetch_completed_items_once(
            keywords=q,
            condition=cond,
            max_price=mx,
            limit=limit,
            relevance_threshold=rel_th,
            stage=stage_name,
        )
        stage_diags.append(diag)
        if best_diag is None or len(rows) > len(best_rows):
            best_rows = rows
            best_diag = diag
        if len(rows) >= min_comps_threshold:
            diag["stages"] = stage_diags
            logger.info("fetch_completed_items_ok: stage=%s keywords=%r count=%d", stage_name, q[:80], len(rows))
            _comps_cache[cache_key] = {
                "rows": rows,
                "diag": diag,
                "expires_at": now + cache_ttl,
            }
            return rows, diag

        # On hard HTTP/rate-limit failures, retry stages are unlikely to help.
        if diag.get("failure_reason") in {"http_error", "rate_limited"}:
            break

    if best_diag is None:
        best_diag = _build_comparable_diagnostics(
            stage="strict",
            query_used=keywords,
            condition_used=condition,
            max_price_used=max_price,
        )
        best_diag["failure_reason"] = "no_stage_executed"

    best_diag["stages"] = stage_diags
    if best_diag.get("failure_reason") is None and not best_rows:
        best_diag["failure_reason"] = "no_comparables_after_fallback"
    logger.info(
        "fetch_completed_items_fallback: keywords=%r best_stage=%s best_count=%d reason=%s",
        keywords[:80],
        best_diag.get("stage"),
        len(best_rows),
        best_diag.get("failure_reason"),
    )
    _comps_cache[cache_key] = {
        "rows": best_rows,
        "diag": best_diag,
        "expires_at": now + (cache_fail_ttl if len(best_rows) == 0 else cache_ttl),
    }
    return best_rows, best_diag


async def fetch_completed_items(
    keywords: str,
    condition: Optional[str] = None,
    max_price: Optional[float] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Backwards-compatible wrapper: returns rows only."""
    rows, _diag = await fetch_completed_items_with_diagnostics(
        keywords=keywords,
        condition=condition,
        max_price=max_price,
        limit=limit,
    )
    return rows


class BrowseSellerProfile:
    """Parsed seller data from Browse API item response."""

    def __init__(self, raw: Dict[str, Any]):
        seller = raw.get("seller", {})
        self.username: str = seller.get("username", "")
        self.feedback_percentage: Optional[float] = (
            float(seller["feedbackPercentage"])
            if seller.get("feedbackPercentage") is not None
            else None
        )
        self.feedback_score: Optional[int] = (
            int(seller["feedbackScore"])
            if seller.get("feedbackScore") is not None
            else None
        )
        self.seller_account_type: Optional[str] = seller.get("sellerAccountType")

        self.has_return_policy: bool = bool(raw.get("returnTerms"))
        self.condition: Optional[str] = raw.get("condition")
        self.item_id: str = raw.get("itemId", "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "username": self.username,
            "feedback_percentage": self.feedback_percentage,
            "feedback_score": self.feedback_score,
            "seller_account_type": self.seller_account_type,
            "has_return_policy": self.has_return_policy,
            "condition": self.condition,
            "item_id": self.item_id,
        }


async def get_item_detail(item_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the full Browse API item response as a raw dict.
    Returns None if OAuth is not configured or the request fails.
    Contains title, price, condition, seller, returnTerms, etc.
    """
    token = await get_application_access_token()
    if not token:
        return None

    browse_base = _urls()["browse"]
    browse_item_id = item_id if "|" in item_id else f"v1|{item_id}|0"
    url = f"{browse_base}/{browse_item_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await _request_with_backoff(client, url, headers)

    if resp is None or resp.status_code != 200:
        return None

    try:
        return resp.json()
    except Exception as exc:
        logger.warning("ebay_browse_item_detail_error: %s", exc)
        return None


async def get_item_seller_profile(item_id: str) -> Optional[BrowseSellerProfile]:
    """
    Fetch seller profile for an eBay item via the Browse API.
    Returns None if OAuth is not configured or the request fails.
    """
    token = await get_application_access_token()
    if not token:
        return None

    browse_base = _urls()["browse"]
    browse_item_id = item_id if "|" in item_id else f"v1|{item_id}|0"
    url = f"{browse_base}/{browse_item_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await _request_with_backoff(client, url, headers)

    if resp is None or resp.status_code != 200:
        return None

    try:
        return BrowseSellerProfile(resp.json())
    except Exception as exc:
        logger.warning("ebay_browse_parse_error: %s", exc)
        return None


async def enrich_items_with_browse(
    item_ids: list[str],
    concurrency: int = 3,
) -> Dict[str, BrowseSellerProfile]:
    """
    Fetch Browse API seller profiles for multiple items with concurrency cap.
    Returns a dict of item_id -> BrowseSellerProfile for successful fetches.
    """
    if not is_browse_configured() or not item_ids:
        return {}

    sem = asyncio.Semaphore(concurrency)

    async def _fetch(iid: str) -> tuple[str, Optional[BrowseSellerProfile]]:
        async with sem:
            await asyncio.sleep(random.uniform(0.1, 0.5))
            profile = await get_item_seller_profile(iid)
            return iid, profile

    tasks = [_fetch(iid) for iid in item_ids if iid]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    profiles: Dict[str, BrowseSellerProfile] = {}
    for r in results:
        if isinstance(r, tuple) and r[1] is not None:
            profiles[r[0]] = r[1]
    return profiles
