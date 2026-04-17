#!/usr/bin/env python3
"""
eBay pipeline diagnostics — no OpenAI, no agent.

Loads repo-root .env, probes:
  1) Finding API: findItemsByKeywords + findCompletedItems (same app id as production code)
  2) Browse API: OAuth client_credentials + item_summary/search

Usage (from repo root):
  set PYTHONPATH=mcp-server
  .venv\\Scripts\\python scripts\\diagnose_ebay_api.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-server"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402


def _load_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)


def _finding_host() -> str:
    return (
        "svcs.sandbox.ebay.com"
        if os.getenv("EBAY_ENVIRONMENT", "SANDBOX").upper() == "SANDBOX"
        else "svcs.ebay.com"
    )


def _browse_root() -> str:
    return (
        "https://api.sandbox.ebay.com"
        if os.getenv("EBAY_ENVIRONMENT", "SANDBOX").upper() == "SANDBOX"
        else "https://api.ebay.com"
    )


async def _finding_call(
    operation: str,
    app_id: str,
    extra_params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    params: Dict[str, str] = {
        "OPERATION-NAME": operation,
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "GLOBAL-ID": "EBAY-US",
        "keywords": "Dell laptop",
        "paginationInput.entriesPerPage": "3",
    }
    if extra_params:
        params.update(extra_params)
    url = f"https://{_finding_host()}/services/search/FindingService/v1"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
    out: Dict[str, Any] = {
        "operation": operation,
        "http_status": r.status_code,
        "host": _finding_host(),
    }
    try:
        data = r.json()
    except Exception:
        out["parse_error"] = True
        out["text_snippet"] = (r.text or "")[:400]
        return out

    errs = data.get("errorMessage") or []
    if errs:
        err0 = errs[0].get("error", [{}])[0] if isinstance(errs[0], dict) else {}
        out["finding_error"] = {
            "errorId": (err0.get("errorId") or [None])[0] if isinstance(err0.get("errorId"), list) else err0.get("errorId"),
            "message": (err0.get("message") or [""])[0] if isinstance(err0.get("message"), list) else err0.get("message"),
            "subdomain": (err0.get("subdomain") or [""])[0] if isinstance(err0.get("subdomain"), list) else err0.get("subdomain"),
        }
        return out

    key = (
        "findItemsByKeywordsResponse"
        if operation == "findItemsByKeywords"
        else "findCompletedItemsResponse"
    )
    blk = (data.get(key) or [{}])[0]
    ack = (blk.get("ack") or ["?"])[0] if isinstance(blk.get("ack"), list) else blk.get("ack")
    out["ack"] = ack
    sr = blk.get("searchResult") or [{}]
    items = sr[0].get("item") if sr and isinstance(sr[0], dict) else None
    out["item_count"] = len(items) if isinstance(items, list) else 0
    return out


async def _browse_probe() -> Dict[str, Any]:
    cid = os.getenv("EBAY_OAUTH_CLIENT_ID", "")
    sec = os.getenv("EBAY_OAUTH_CLIENT_SECRET", "")
    if not cid or not sec:
        return {"configured": False, "reason": "missing EBAY_OAUTH_CLIENT_ID or EBAY_OAUTH_CLIENT_SECRET"}

    auth_url = f"{_browse_root()}/identity/v1/oauth2/token"
    async with httpx.AsyncClient(timeout=15.0) as client:
        tr = await client.post(
            auth_url,
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            auth=(cid, sec),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if tr.status_code != 200:
        return {
            "configured": True,
            "oauth_http": tr.status_code,
            "oauth_snippet": (tr.text or "")[:300],
        }
    token = tr.json().get("access_token")
    if not token:
        return {"configured": True, "oauth_http": 200, "error": "no access_token in body"}

    search_url = f"{_browse_root()}/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        sr = await client.get(search_url, headers=headers, params={"q": "Dell laptop", "limit": "3"})
    body: Dict[str, Any] = {
        "configured": True,
        "oauth_http": 200,
        "search_http": sr.status_code,
    }
    if sr.status_code == 200:
        jd = sr.json()
        items = jd.get("itemSummaries") or []
        body["browse_item_count"] = len(items)
        body["browse_total"] = jd.get("total")
    else:
        body["search_snippet"] = (sr.text or "")[:400]
    return body


async def _search_ebay_route() -> Dict[str, Any]:
    """Call in-process search_ebay (same logic as GET /search/ebay)."""
    logging.getLogger("mcp.main").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
    from app.main import search_ebay  # noqa: E402

    resp = await search_ebay(q="Dell laptop", limit=3)
    return {
        "route_query": resp.query,
        "route_source": resp.source,
        "route_result_count": len(resp.results),
        "route_search_url_host": "ebay.com" if "ebay.com" in resp.search_url else resp.search_url[:40],
    }


async def main() -> None:
    _load_env()
    app_id = os.getenv("EBAY_APP_ID", "")
    report: Dict[str, Any] = {
        "env": {
            "EBAY_ENVIRONMENT": os.getenv("EBAY_ENVIRONMENT"),
            "EBAY_APP_ID_set": bool(app_id),
            "EBAY_APP_ID_prefix": (app_id[:12] + "…") if len(app_id) > 12 else app_id or "(empty)",
            "EBAY_OAUTH_set": bool(
                os.getenv("EBAY_OAUTH_CLIENT_ID") and os.getenv("EBAY_OAUTH_CLIENT_SECRET")
            ),
        },
        "finding_host": _finding_host(),
        "browse_root": _browse_root(),
    }

    if not app_id:
        report["finding"] = {"skipped": True, "reason": "EBAY_APP_ID not set"}
    else:
        report["finding_keywords"] = await _finding_call("findItemsByKeywords", app_id)
        report["finding_completed"] = await _finding_call(
            "findCompletedItems",
            app_id,
            {
                "itemFilter(0).name": "SoldItemsOnly",
                "itemFilter(0).value": "true",
            },
        )

    report["browse"] = await _browse_probe()

    try:
        report["search_ebay_route"] = await _search_ebay_route()
    except Exception as exc:
        report["search_ebay_route"] = {"error": str(exc)}

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
