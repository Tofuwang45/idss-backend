"""
Merchant Reliability Engine - Evaluation Pipeline.

Two subcommands:

  golden  — Offline scoring eval against golden_sellers.json (no network).
  search  — Live eBay product search eval: hits Finding API / RSS, runs MRE,
             writes timestamped artifacts to results/.

Usage:
    python -m evaluation.merchant_reliability.run_eval golden
    python -m evaluation.merchant_reliability.run_eval golden --quiet
    python -m evaluation.merchant_reliability.run_eval search
    python -m evaluation.merchant_reliability.run_eval search --limit 3
    python -m evaluation.merchant_reliability.run_eval search --json-only
    python -m evaluation.merchant_reliability.run_eval search --sleep 2 --per-query-timeout 45
    python -m evaluation.merchant_reliability.run_eval search --no-mre
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Path & env bootstrap (before any app imports) ─────────────────────────────
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_MCP = os.path.join(_REPO_ROOT, "mcp-server")
for p in (_REPO_ROOT, _MCP):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.isfile(_env_path):
        load_dotenv(_env_path, override=True)
except ImportError:
    pass

os.environ.setdefault("MERCHANT_RELIABILITY_ENABLED", "1")
os.environ.setdefault("MCP_SKIP_PRELOAD", "1")

GOLDEN_PATH = Path(__file__).with_name("golden_sellers.json")
QUERIES_PATH = Path(__file__).with_name("queries.json")
RESULTS_DIR = Path(__file__).with_name("results")


# ── Common dataclass for source-agnostic search results ──────────────────────

@dataclass
class SearchEvalResult:
    query_id: str
    query: str
    source_requested: str
    source_used: str
    results: List[Dict[str, Any]]
    search_url: str
    latency_ms: float
    error: Optional[str] = None
    tier_distribution: Dict[str, int] = field(default_factory=dict)
    risk_flags_seen: List[str] = field(default_factory=list)
    results_count: int = 0
    sellers_found: int = 0
    reports_generated: int = 0
    passed: bool = True


# ── Startup banner ────────────────────────────────────────────────────────────

def _print_banner():
    finding = "YES" if os.getenv("EBAY_APP_ID") else "NO (will use RSS fallback)"
    browse_id = bool(os.getenv("EBAY_OAUTH_CLIENT_ID"))
    browse_secret = bool(os.getenv("EBAY_OAUTH_CLIENT_SECRET"))
    browse = "YES" if (browse_id and browse_secret) else "NO"
    mre = "ON" if os.getenv("MERCHANT_RELIABILITY_ENABLED") == "1" else "OFF"
    env = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    print("=" * 70, flush=True)
    print("  Merchant Reliability Engine - Evaluation Pipeline", flush=True)
    print("=" * 70, flush=True)
    print(f"  Finding API:  {finding}", flush=True)
    print(f"  Browse API:   {browse}", flush=True)
    print(f"  MRE scoring:  {mre}", flush=True)
    print(f"  Environment:  {env}", flush=True)
    print("=" * 70, flush=True)
    print(flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_results_dir() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def _write_csv(path: Path, rows: List[Dict], fieldnames: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _fmt_flags(flags: List[str]) -> str:
    if not flags:
        return "[]"
    return "[" + ", ".join(flags) + "]"


def _pct(latencies: List[float], pct: float) -> float:
    if not latencies:
        return 0.0
    s = sorted(latencies)
    idx = int(len(s) * pct)
    return s[min(idx, len(s) - 1)]


# ============================================================================
#  SEARCH subcommand — live eBay product search evaluation
# ============================================================================

# Source dispatcher: add new sources here
_SEARCH_DISPATCH: Dict[str, Any] = {}  # populated after function definitions


async def _run_ebay_query(
    query_spec: Dict[str, Any],
    mre_enabled: bool,
) -> SearchEvalResult:
    """Execute a single eBay search query through the production code path."""
    from app.main import search_ebay

    qid = query_spec["id"]
    q = query_spec["query"]
    max_price = query_spec.get("max_price")
    condition = query_spec.get("condition")
    expect_min = query_spec.get("expect_min_results", 1)

    prev_mre = os.environ.get("MERCHANT_RELIABILITY_ENABLED")
    os.environ["MERCHANT_RELIABILITY_ENABLED"] = "1" if mre_enabled else "0"

    t0 = time.perf_counter()
    try:
        response = await search_ebay(
            q=q,
            max_price=max_price,
            condition=condition,
            sort="best-match",
            limit=10,
        )
        latency_ms = (time.perf_counter() - t0) * 1000

        results_dicts = [r.model_dump() for r in response.results]
        results_count = len(results_dicts)
        sellers_found = sum(1 for r in results_dicts if r.get("seller"))
        reports_generated = sum(1 for r in results_dicts if r.get("merchant_report"))

        tier_dist: Dict[str, int] = Counter()
        all_flags: List[str] = []
        for r in results_dicts:
            mr = r.get("merchant_report")
            if mr:
                tier_dist[mr["reliability_tier"]] = tier_dist.get(mr["reliability_tier"], 0) + 1
                all_flags.extend(mr.get("risk_flags", []))

        return SearchEvalResult(
            query_id=qid,
            query=q,
            source_requested="ebay",
            source_used=response.source,
            results=results_dicts,
            search_url=response.search_url,
            latency_ms=round(latency_ms, 2),
            results_count=results_count,
            sellers_found=sellers_found,
            reports_generated=reports_generated,
            tier_distribution=dict(tier_dist),
            risk_flags_seen=list(set(all_flags)),
            passed=results_count >= expect_min,
        )
    except (Exception, asyncio.CancelledError) as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return SearchEvalResult(
            query_id=qid,
            query=q,
            source_requested="ebay",
            source_used="error",
            results=[],
            search_url="",
            latency_ms=round(latency_ms, 2),
            error=str(exc) or type(exc).__name__,
            passed=expect_min == 0,
        )
    finally:
        if prev_mre is not None:
            os.environ["MERCHANT_RELIABILITY_ENABLED"] = prev_mre
        elif "MERCHANT_RELIABILITY_ENABLED" in os.environ:
            os.environ["MERCHANT_RELIABILITY_ENABLED"] = "1"


_SEARCH_DISPATCH["ebay"] = _run_ebay_query


async def run_search_eval(
    queries_path: Path,
    verbose: bool = True,
    mre_enabled: bool = True,
    limit: Optional[int] = None,
    sleep_seconds: float = 0.0,
    per_query_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Run live search evaluation across all queries."""
    with open(queries_path) as f:
        queries: List[Dict[str, Any]] = json.load(f)

    if limit and limit > 0:
        queries = queries[:limit]

    total = len(queries)
    eval_results: List[SearchEvalResult] = []
    flat_rows: List[Dict[str, Any]] = []

    if verbose:
        print(f"Running {total} search queries...", flush=True)
        if sleep_seconds > 0:
            print(f"  Pace: {sleep_seconds}s sleep between queries (rate-limit friendly).", flush=True)
        if per_query_timeout and per_query_timeout > 0:
            print(f"  Per-query timeout: {per_query_timeout}s (prevents hanging).", flush=True)
        print("-" * 120, flush=True)
        hdr = (
            f"{'#':>3}  {'id':<6}  {'query':<35}  {'src':<8}  "
            f"{'results':>7}  {'sellers':>7}  {'reports':>7}  {'tiers':<30}  {'ms':>8}  {'pass':^5}"
        )
        print(hdr, flush=True)
        print("-" * 120, flush=True)

    for idx, q_spec in enumerate(queries, start=1):
        source = q_spec.get("source", "ebay")
        handler = _SEARCH_DISPATCH.get(source)
        if handler is None:
            r = SearchEvalResult(
                query_id=q_spec["id"], query=q_spec["query"],
                source_requested=source, source_used="unsupported",
                results=[], search_url="", latency_ms=0,
                error=f"Unsupported source: {source}", passed=False,
            )
        else:
            if per_query_timeout and per_query_timeout > 0:
                try:
                    r = await asyncio.wait_for(
                        handler(q_spec, mre_enabled),
                        timeout=per_query_timeout,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    exp = q_spec.get("expect_min_results", 1)
                    r = SearchEvalResult(
                        query_id=q_spec["id"],
                        query=q_spec["query"],
                        source_requested=source,
                        source_used="error",
                        results=[],
                        search_url="",
                        latency_ms=round(per_query_timeout * 1000, 2),
                        error=f"Timed out after {per_query_timeout}s",
                        passed=exp == 0,
                    )
            else:
                r = await handler(q_spec, mre_enabled)

        eval_results.append(r)

        for res in r.results:
            mr = res.get("merchant_report")
            seller = res.get("seller") or {}
            flat_rows.append({
                "query_id": r.query_id,
                "query": r.query,
                "source": r.source_used,
                "title": res.get("title", ""),
                "price": res.get("price", ""),
                "condition": res.get("condition", ""),
                "item_id": res.get("item_id", ""),
                "url": res.get("url", ""),
                "seller_username": seller.get("seller_username", ""),
                "feedback_score": seller.get("feedback_score", ""),
                "positive_feedback_pct": seller.get("positive_feedback_pct", ""),
                "top_rated": seller.get("top_rated_seller", ""),
                "tier": mr.get("reliability_tier", "") if mr else "",
                "risk_flags": ", ".join(mr.get("risk_flags", [])) if mr else "",
                "negotiation_context": mr.get("negotiation_context", "") if mr else "",
            })

        if verbose:
            tier_str = " ".join(f"{k}={v}" for k, v in sorted(r.tier_distribution.items())) or "-"
            if len(tier_str) > 28:
                tier_str = tier_str[:25] + "..."
            mark = "OK" if r.passed else "FAIL"
            if r.error:
                mark = "ERR"
            query_short = r.query[:33] + ".." if len(r.query) > 35 else r.query
            line = (
                f"{idx:3}  {r.query_id:<6}  {query_short:<35}  {r.source_used:<8}  "
                f"{r.results_count:>7}  {r.sellers_found:>7}  {r.reports_generated:>7}  "
                f"{tier_str:<30}  {r.latency_ms:8.0f}  {mark:^5}"
            )
            print(line, flush=True)

        if sleep_seconds > 0 and idx < total:
            await asyncio.sleep(sleep_seconds)

    if verbose:
        print("-" * 120, flush=True)
        print(flush=True)

    # ── Aggregate metrics ────────────────────────────────────────────────
    latencies = [r.latency_ms for r in eval_results]
    source_counts = Counter(r.source_used for r in eval_results)
    total_tiers: Dict[str, int] = Counter()
    total_flags: Counter = Counter()
    for r in eval_results:
        for tier, cnt in r.tier_distribution.items():
            total_tiers[tier] += cnt
        for flg in r.risk_flags_seen:
            total_flags[flg] += 1

    passed_count = sum(1 for r in eval_results if r.passed)
    error_count = sum(1 for r in eval_results if r.error)
    total_results = sum(r.results_count for r in eval_results)

    diagnosis: List[str] = []
    if total > 0:
        uo = source_counts.get("url_only", 0)
        api_n = source_counts.get("api", 0)
        browse_n = source_counts.get("browse", 0)
        rss_n = source_counts.get("rss", 0)
        has_app_id = bool(os.getenv("EBAY_APP_ID"))
        has_browse_oauth = bool(
            os.getenv("EBAY_OAUTH_CLIENT_ID") and os.getenv("EBAY_OAUTH_CLIENT_SECRET")
        )
        if uo == total:
            if has_app_id:
                diagnosis.append(
                    "All queries ended with source=url_only despite EBAY_APP_ID being set. "
                    "Typical cause: Finding API HTTP 500 + error 10001 (daily/call rate limit on findItemsByKeywords). "
                    "Check logs for ebay_finding_api_error / ebay_finding_http_error. "
                    "Fix: set EBAY_OAUTH_CLIENT_ID + EBAY_OAUTH_CLIENT_SECRET so Browse API keyword search can run "
                    "after Finding fails; or wait for Finding quota to reset. RSS may also time out (ebay_rss_error)."
                )
            else:
                diagnosis.append(
                    "All queries ended with source=url_only: no parsed listings. "
                    "Set EBAY_APP_ID for Finding API and/or Browse OAuth for Browse search fallback."
                )
        elif api_n == 0 and browse_n == 0 and rss_n == 0 and uo > 0:
            diagnosis.append(
                f"{uo}/{total} queries fell back to url_only. "
                "Finding may be rate-limited (10001); RSS may time out. "
                "With Browse OAuth configured, Browse search runs automatically between Finding and RSS."
            )
        elif browse_n > 0 and api_n == 0:
            diagnosis.append(
                f"{browse_n}/{total} query(s) used Browse API search (Finding returned no items or was rate-limited). "
                "Expected when Finding quota is exhausted but Browse OAuth is valid."
            )
        if not has_app_id:
            diagnosis.append(
                "EBAY_APP_ID is unset: Finding API is skipped; Browse search still works if OAuth credentials are set."
            )
        if uo > 0 and has_app_id and not has_browse_oauth:
            diagnosis.append(
                "Browse OAuth not configured: when Finding is rate-limited you only get RSS/url_only. "
                "Add EBAY_OAUTH_CLIENT_ID (same as App ID) and EBAY_OAUTH_CLIENT_SECRET (Cert ID)."
            )
        if mre_enabled and total_results > 0:
            sr = sum(r.sellers_found for r in eval_results)
            if sr == 0:
                diagnosis.append(
                    "Results returned but sellers_found=0: MRE reports need sellerInfo (Finding) or Browse; "
                    "RSS rows usually have no seller block."
                )
        if error_count:
            diagnosis.append(f"{error_count} query(s) raised errors; see per_query error fields in artifacts.")
    if sleep_seconds > 0:
        diagnosis.append(f"Pacing used: {sleep_seconds}s between queries.")
    if per_query_timeout and per_query_timeout > 0:
        diagnosis.append(f"Per-query timeout cap: {per_query_timeout}s.")

    summary = {
        "timestamp": datetime.now().isoformat(),
        "queries_run": total,
        "pass_count": passed_count,
        "pass_rate": round(passed_count / total, 4) if total else 0,
        "error_count": error_count,
        "total_results": total_results,
        "avg_results_per_query": round(total_results / total, 2) if total else 0,
        "source_distribution": dict(source_counts),
        "tier_distribution": dict(total_tiers),
        "risk_flags_seen": dict(total_flags),
        "latency_p50_ms": round(_pct(latencies, 0.50), 1),
        "latency_p95_ms": round(_pct(latencies, 0.95), 1),
        "mre_enabled": mre_enabled,
        "sleep_seconds_between_queries": sleep_seconds,
        "per_query_timeout_seconds": per_query_timeout,
        "diagnosis": diagnosis,
    }

    per_query = []
    for r in eval_results:
        row = asdict(r)
        row.pop("results", None)
        per_query.append(row)

    return {
        "summary": summary,
        "per_query": per_query,
        "flat_rows": flat_rows,
        "full_results": [asdict(r) for r in eval_results],
    }


def _print_search_summary(summary: Dict[str, Any]):
    sd = summary.get("source_distribution", {})
    td = summary.get("tier_distribution", {})
    rf = summary.get("risk_flags_seen", {})

    print("=" * 60, flush=True)
    print(f"  Queries run:         {summary['queries_run']}", flush=True)
    _src_labels = {
        "api": "Finding API",
        "browse": "Browse search",
        "rss": "RSS fallback",
        "url_only": "URL only",
        "error": "Errors",
    }
    for src in ("api", "browse", "rss", "url_only", "error"):
        if sd.get(src, 0) > 0:
            print(f"    {_src_labels.get(src, src):<16} {sd[src]:>4}", flush=True)
    for src in sd:
        if src not in _src_labels:
            print(f"    {src:<16} {sd[src]:>4}", flush=True)
    print(f"  Pass rate:           {summary['pass_rate']:.1%}  (>= expect_min_results)", flush=True)
    print(f"  Errors:              {summary['error_count']}", flush=True)
    print(f"  Avg results/query:   {summary['avg_results_per_query']}", flush=True)
    if td:
        parts = " ".join(f"{k}={v}" for k, v in sorted(td.items()))
        print(f"  Tier distribution:   {parts}", flush=True)
    if rf:
        parts = ", ".join(f"{k}({v})" for k, v in sorted(rf.items(), key=lambda x: -x[1]))
        print(f"  Risk flags seen:     {parts}", flush=True)
    print(f"  Latency p50:         {summary['latency_p50_ms']:.0f} ms", flush=True)
    print(f"  Latency p95:         {summary['latency_p95_ms']:.0f} ms", flush=True)
    print(f"  MRE enabled:         {summary['mre_enabled']}", flush=True)
    dx = summary.get("diagnosis") or []
    if dx:
        print(flush=True)
        print("  Diagnosis:", flush=True)
        for line in dx:
            print(f"    - {line}", flush=True)
    print("=" * 60, flush=True)


def _write_search_artifacts(data: Dict[str, Any], json_only: bool = False):
    out_dir = _ensure_results_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    summary_path = out_dir / f"search_summary_{ts}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(data["summary"], f, indent=2)
    print(f"  Summary:  {summary_path}", flush=True)

    full_path = out_dir / f"search_results_{ts}.json"
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(data["full_results"], f, indent=2, default=str)
    print(f"  Full:     {full_path}", flush=True)

    if data["flat_rows"]:
        csv_path = out_dir / f"search_results_{ts}.csv"
        fieldnames = [
            "query_id", "query", "source", "title", "price", "condition",
            "item_id", "url", "seller_username", "feedback_score",
            "positive_feedback_pct", "top_rated", "tier", "risk_flags",
            "negotiation_context",
        ]
        _write_csv(csv_path, data["flat_rows"], fieldnames)
        print(f"  CSV:      {csv_path}", flush=True)


# ============================================================================
#  GOLDEN subcommand — synthetic seller scoring eval (unchanged logic)
# ============================================================================

async def run_golden_eval(verbose: bool = True) -> Dict[str, Any]:
    from app.merchant_reliability import compute_merchant_report

    with open(GOLDEN_PATH) as f:
        sellers: List[Dict[str, Any]] = json.load(f)

    total = len(sellers)
    tier_correct = 0
    flag_tp = 0
    flag_expected_total = 0
    latencies: List[float] = []
    mismatches: List[Dict[str, Any]] = []
    per_seller: List[Dict[str, Any]] = []

    if verbose:
        print("Merchant Reliability - per-seller results (golden dataset)", flush=True)
        print("-" * 100, flush=True)
        hdr = (
            f"{'#':>3}  {'seller':<22}  {'exp tier':<12}  {'got tier':<12}  "
            f"{'tier':^6}  {'flags (got)':<40}  {'ms':>7}"
        )
        print(hdr, flush=True)
        print("-" * 100, flush=True)

    for idx, seller in enumerate(sellers, start=1):
        t0 = time.perf_counter()
        report = await compute_merchant_report(
            seller_username=seller["seller_username"],
            feedback_score=seller.get("feedback_score"),
            positive_feedback_pct=seller.get("positive_feedback_pct"),
            top_rated=seller.get("top_rated"),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)

        expected_tier = seller["expected_tier"]
        tier_ok = report.reliability_tier == expected_tier
        if tier_ok:
            tier_correct += 1
        else:
            mismatches.append({
                "seller": seller["seller_username"],
                "expected": expected_tier,
                "got": report.reliability_tier,
                "flags": report.risk_flags,
            })

        expected_flags = set(seller.get("expected_flags", []))
        flag_expected_total += len(expected_flags)
        flag_tp += len(expected_flags & set(report.risk_flags))

        row = {
            "index": idx,
            "seller_username": seller["seller_username"],
            "expected_tier": expected_tier,
            "got_tier": report.reliability_tier,
            "tier_match": tier_ok,
            "feedback_score": seller.get("feedback_score"),
            "positive_feedback_pct": seller.get("positive_feedback_pct"),
            "top_rated": seller.get("top_rated"),
            "risk_flags": report.risk_flags,
            "expected_flags": list(expected_flags),
            "negotiation_context": report.negotiation_context,
            "latency_ms": round(elapsed_ms, 3),
        }
        per_seller.append(row)

        if verbose:
            tier_mark = "OK" if tier_ok else "FAIL"
            flags_short = _fmt_flags(report.risk_flags)
            if len(flags_short) > 38:
                flags_short = flags_short[:35] + "..."
            line = (
                f"{idx:3}  {seller['seller_username']:<22}  {expected_tier:<12}  "
                f"{report.reliability_tier:<12}  {tier_mark:^6}  {flags_short:<40}  {elapsed_ms:7.2f}"
            )
            print(line, flush=True)

    if verbose:
        print("-" * 100, flush=True)
        print(flush=True)

    tier_accuracy = tier_correct / total if total else 0
    flag_recall = flag_tp / flag_expected_total if flag_expected_total else 1.0
    latencies.sort()

    return {
        "total_sellers": total,
        "tier_accuracy": round(tier_accuracy, 4),
        "tier_correct": tier_correct,
        "tier_wrong": total - tier_correct,
        "flag_recall": round(flag_recall, 4),
        "latency_p50_ms": round(_pct(latencies, 0.50), 2),
        "latency_p95_ms": round(_pct(latencies, 0.95), 2),
        "mismatches": mismatches,
        "per_seller": per_seller,
    }


# ============================================================================
#  CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Merchant Reliability Engine - Evaluation Pipeline",
    )
    sub = parser.add_subparsers(dest="command")

    # ── golden ────────────────────────────────────────────────────────────
    p_golden = sub.add_parser("golden", help="Offline golden-seller scoring eval")
    p_golden.add_argument("--quiet", action="store_true")
    p_golden.add_argument("--json-only", action="store_true")
    p_golden.add_argument("--include-context", action="store_true")

    # ── search ────────────────────────────────────────────────────────────
    p_search = sub.add_parser("search", help="Live eBay product search eval")
    p_search.add_argument("--queries", type=str, default=None,
                          help="Path to queries JSON (default: queries.json beside this script)")
    p_search.add_argument("--limit", type=int, default=None,
                          help="Max number of queries to run")
    p_search.add_argument("--quiet", action="store_true")
    p_search.add_argument("--json-only", action="store_true")
    p_search.add_argument("--no-mre", action="store_true",
                          help="Disable MRE scoring (search only, no merchant reports)")
    p_search.add_argument("--mre", action="store_true", default=True,
                          help="Enable MRE scoring (default)")
    p_search.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Seconds to sleep between queries (default: 2). Use 0 to disable.",
    )
    p_search.add_argument(
        "--per-query-timeout",
        type=float,
        default=45.0,
        metavar="SEC",
        help="Max seconds per query including HTTP (default: 45). Use 0 to disable.",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        print("\nPlease specify a subcommand: golden or search")
        sys.exit(1)

    if args.command == "golden":
        _cmd_golden(args)
    elif args.command == "search":
        _cmd_search(args)


def _cmd_golden(args):
    verbose = not args.quiet and not args.json_only
    if verbose:
        _print_banner()

    results = asyncio.run(run_golden_eval(verbose=verbose))

    if args.json_only:
        out = dict(results)
        if not args.include_context:
            out["per_seller"] = [
                {k: v for k, v in row.items() if k != "negotiation_context"}
                for row in results["per_seller"]
            ]
        print(json.dumps(out, indent=2))
    else:
        summary = {k: v for k, v in results.items() if k != "per_seller"}
        print(json.dumps(summary, indent=2))
        print()

    passed = results["tier_accuracy"] >= 0.90 and results["latency_p95_ms"] < 2000
    status = "PASS" if passed else "FAIL"
    if not args.json_only:
        print(f"{'='*50}")
        print(f"  Tier accuracy:  {results['tier_accuracy']:.1%}  (target >= 90%)")
        print(f"  Flag recall:    {results['flag_recall']:.1%}")
        print(f"  Latency p95:    {results['latency_p95_ms']:.1f} ms  (target < 2000 ms)")
        print(f"  Result: {status}")
        print(f"{'='*50}")

        if results["mismatches"]:
            print(f"\n  Mismatches ({len(results['mismatches'])}):")
            for m in results["mismatches"]:
                print(f"    {m['seller']}: expected={m['expected']}, got={m['got']}, flags={m['flags']}")

    sys.exit(0 if passed else 1)


def _cmd_search(args):
    verbose = not args.quiet and not args.json_only
    mre_enabled = not args.no_mre
    queries_path = Path(args.queries) if args.queries else QUERIES_PATH

    if not queries_path.exists():
        print(f"ERROR: Queries file not found: {queries_path}")
        sys.exit(1)

    if verbose:
        _print_banner()

    sleep_sec = max(0.0, args.sleep)
    pq_timeout = args.per_query_timeout if args.per_query_timeout and args.per_query_timeout > 0 else None

    data = asyncio.run(run_search_eval(
        queries_path=queries_path,
        verbose=verbose,
        mre_enabled=mre_enabled,
        limit=args.limit,
        sleep_seconds=sleep_sec,
        per_query_timeout=pq_timeout,
    ))

    if args.json_only:
        print(json.dumps(data, indent=2, default=str))
    else:
        _print_search_summary(data["summary"])
        print(flush=True)
        print("Writing artifacts...", flush=True)
        _write_search_artifacts(data, json_only=args.json_only)
        print(flush=True)

    passed = data["summary"]["pass_rate"] >= 0.80
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
