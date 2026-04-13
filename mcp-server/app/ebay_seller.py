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
from typing import Any, Dict, Optional

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
) -> Optional[httpx.Response]:
    """GET with exponential backoff on transient failures."""
    for attempt in range(max_retries):
        try:
            resp = await client.get(url, headers=headers)
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
    url = f"{browse_base}/v1|{item_id}"
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
