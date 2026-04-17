#!/usr/bin/env python3
"""
Full eBay deal pipeline — run each step with visible output (no OpenAI required).

Mirrors the logic in mcp-server/app/main.py::_tool_search_and_evaluate_ebay:
  Step 1  search_ebay (Finding → Browse → RSS fallbacks; MRE enrichment on results)
  Step 2  Sold comparables via fetch_completed_items_with_diagnostics
  Step 3  parse_comparables + optional active_market pseudo-comps
  Step 4  compute_fmv + decide per listing (first N with prices)

Usage (repo root):
  set PYTHONPATH=mcp-server
  set MCP_SKIP_PRELOAD=1
  .venv\\Scripts\\python scripts\\run_ebay_pipeline_verbose.py --query "MacBook Pro 14"

Optional HTTP mode (server must be running; one JSON blob, fewer steps):
  .venv\\Scripts\\python scripts\\run_ebay_pipeline_verbose.py --http http://127.0.0.1:8001 --query "Dell laptop"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Heavy preload off before importing the FastAPI app stack
os.environ.setdefault("MCP_SKIP_PRELOAD", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-server"))


def _banner(title: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n  {title}\n{line}")


def _j(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    return json.dumps(obj, indent=2, default=str)


async def run_inline(query: str, limit: int, max_price: Optional[float], eval_first: int, quiet: bool) -> None:
    from dotenv import load_dotenv

    if quiet:
        logging.basicConfig(level=logging.ERROR, force=True)
        for name in ("mcp.main", "mcp.ebay_seller", "httpx", "uvicorn"):
            logging.getLogger(name).setLevel(logging.ERROR)

    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    from app.main import search_ebay, _ebay_result_to_evaluated
    from app.ebay_seller import fetch_completed_items_with_diagnostics
    from app.market_analysis import compute_fmv, parse_comparables, SoldComparable
    from app.deal_engine import TargetListing, decide

    _banner("STEP 1 - search_ebay (live listings + relevance filter + MRE when enabled)")
    search_resp = await search_ebay(q=query, max_price=max_price, condition=None, limit=limit)
    print(f"  query          : {search_resp.query!r}")
    print(f"  source         : {search_resp.source}")
    print(f"  max_price      : {search_resp.max_price}")
    print(f"  results_count  : {len(search_resp.results)}")
    for i, r in enumerate(search_resp.results, 1):
        tier = (r.merchant_report or {}).get("reliability_tier") if r.merchant_report else None
        print(f"  [{i}] {r.title[:70]!r}")
        print(f"       price_cents={r.price_cents!r}  item_id={r.item_id!r}  trust={tier!r}")

    _banner("STEP 2 - fetch_completed_items_with_diagnostics (sold comps for FMV)")
    raw_comps, comp_diag = await fetch_completed_items_with_diagnostics(
        keywords=query,
        condition=None,
        max_price=(max_price * 2) if max_price else None,
        limit=50,
    )
    print("  diagnostics:")
    for k, v in comp_diag.items():
        print(f"    {k}: {v!r}")
    print(f"  raw_comps_count: {len(raw_comps)}")

    _banner("STEP 3 - parse_comparables (+ active_market fallback if empty)")
    comparables = parse_comparables(raw_comps)
    fmv_source = "sold_history"
    confidence_cap = 1.0
    if comparables:
        print(f"  comparables (sold): {len(comparables)}")
        for c in comparables[:5]:
            print(f"    ${c.sold_price_cents/100:.2f}  end={c.end_time}  cond={c.condition!r}")
    else:
        print("  sold comparables: EMPTY")
        priced = [r for r in search_resp.results if r.price_cents]
        if len(priced) >= 2:
            now = datetime.now(timezone.utc)
            comparables = [
                SoldComparable(
                    title=r.title or "",
                    sold_price_cents=r.price_cents,
                    condition=r.condition,
                    end_time=now,
                    listing_type="FixedPrice",
                )
                for r in priced
            ]
            fmv_source = "active_market"
            confidence_cap = 0.5
            print(f"  FALLBACK => built {len(comparables)} pseudo-comps from active listings")
            print(f"  fmv_source={fmv_source!r}  confidence_cap={confidence_cap}")
        else:
            print("  FALLBACK skipped (need at least 2 priced active results)")

    _banner(f"STEP 4 - compute_fmv + decide (first {eval_first} priced listings)")
    n_done = 0
    for r in search_resp.results:
        if not r.price_cents:
            continue
        if n_done >= eval_first:
            break
        n_done += 1
        print(f"\n  --- Listing #{n_done}: {r.title[:60]!r} ---")
        fmv_result = await compute_fmv(
            target_price_cents=r.price_cents,
            comparables=comparables,
            target_condition=r.condition,
            confidence_cap=confidence_cap,
        )
        print("  FMVResult:")
        print(_j(fmv_result))

        listing = TargetListing(
            item_id=r.item_id,
            title=r.title,
            price_cents=r.price_cents,
            condition=r.condition,
            listing_type="FixedPrice",
            seller_username=r.seller.seller_username if r.seller else None,
            merchant_report=r.merchant_report,
        )
        decision = await decide(listing, fmv_result)
        print("  DealDecision:")
        print(_j(decision))

        fb = None
        if fmv_result.confidence == 0.0:
            fb = comp_diag.get("failure_reason") or "no_comparables"
        ev = _ebay_result_to_evaluated(
            r,
            fmv_result,
            decision,
            fmv_source=fmv_source,
            fmv_stage=comp_diag.get("stage"),
            fmv_fallback_reason=fb,
            fmv_query_used=comp_diag.get("query_used"),
        )
        print("  EvaluatedListing (API shape):")
        print(_j(ev))

    if n_done == 0:
        print("  (no priced listings to evaluate)")

    _banner("DONE")


async def run_http(base: str, query: str, limit: int, max_price: Optional[float]) -> None:
    import httpx

    url = f"{base.rstrip('/')}/tools/execute"
    params: Dict[str, Any] = {"query": query, "limit": limit}
    if max_price is not None:
        params["max_price"] = max_price
    body = {"tool_name": "search_and_evaluate_ebay", "parameters": params}
    _banner("HTTP - POST /tools/execute search_and_evaluate_ebay (single response)")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, json=body)
    print(f"  http_status: {r.status_code}")
    try:
        data = r.json()
    except Exception:
        print(r.text[:2000])
        return
    print(json.dumps(data, indent=2, default=str)[:12000])


def main() -> None:
    p = argparse.ArgumentParser(description="Verbose eBay FMV/MRE/deal pipeline")
    p.add_argument("--query", default="Dell laptop", help="Search keywords")
    p.add_argument("--limit", type=int, default=5, help="Max active listings (1-20)")
    p.add_argument("--max-price", type=float, default=None, help="Optional USD ceiling")
    p.add_argument("--eval-first", type=int, default=3, help="How many priced listings to FMV+decide")
    p.add_argument("--http", type=str, default=None, help="If set, call this base URL (e.g. http://127.0.0.1:8001)")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress eBay/HTTP log lines so only step banners and JSON remain",
    )
    args = p.parse_args()

    lim = max(1, min(args.limit, 20))
    if args.http:
        asyncio.run(run_http(args.http, args.query, lim, args.max_price))
    else:
        asyncio.run(run_inline(args.query, lim, args.max_price, args.eval_first, args.quiet))


if __name__ == "__main__":
    main()
