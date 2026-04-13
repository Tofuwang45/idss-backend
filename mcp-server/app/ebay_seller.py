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
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("mcp.ebay_seller")

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

        iid = item.get("itemId")
        rows.append({
            "title": title,
            "price": price_str,
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
    logger.info("ebay_browse_search_ok: query=%r rows=%d", (q or "")[:80], len(rows))
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
