"""
Price History Summary — turn a bag of SoldComparables into a compact
time-series payload that the frontend `PriceHistoryChart` consumes.

Design goals:
- Cheap to compute (no extra API calls; reuses comparables already fetched
  for FMV).
- Degrades gracefully: <3 comparables → returns None so the UI hides the
  widget instead of drawing a misleading flat line.
- Small JSON footprint (typically ~6-12 buckets).
- Same units as the rest of the pipeline: all prices are integer cents.

The output schema is locked to what the frontend already renders — see
`idss-web/src/types/chat.ts::PriceHistorySummary`. Changing field names
here breaks the chart, so any schema change must be coordinated across
both ends.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.market_analysis import SoldComparable

MIN_COMPARABLES = 3
MIN_BUCKETS_WITH_DATA = 2
DEFAULT_WINDOW_DAYS = 90
DEFAULT_BUCKET_DAYS = 7


def _percentile_int(values: Sequence[int], pct: float) -> int:
    if not values:
        return 0
    return int(round(float(np.percentile(np.asarray(values, dtype=np.float64), pct))))


def _extract_sources(sources_diag: Any) -> List[str]:
    """Turn the ebay_seller/serpapi `sources` diagnostic into a simple list.

    Accepts either:
      - A list of dicts like ``{"source": "ebay_finding", "count": 5}`` — only
        sources with count > 0 are kept.
      - A list of bare strings.
      - None / anything else → ``[]``.
    """
    if not isinstance(sources_diag, list):
        return []
    out: List[str] = []
    for s in sources_diag:
        if isinstance(s, dict):
            if int(s.get("count", 0)) > 0 and s.get("source"):
                out.append(str(s["source"]))
        elif isinstance(s, str) and s:
            out.append(s)
    return out


def build_price_history_summary(
    comparables: List[SoldComparable],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    bucket_days: int = DEFAULT_BUCKET_DAYS,
    source: Optional[str] = None,
    sources: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return a JSON-serializable summary shaped for the frontend chart.

    Returns ``None`` when there are fewer than ``MIN_COMPARABLES`` in-window
    points, or fewer than ``MIN_BUCKETS_WITH_DATA`` non-empty buckets.

    Output schema (matches `PriceHistorySummary` in the frontend):
        {
            "window_days": 90,
            "first_observed": "2026-02-…" | null,
            "last_observed":  "2026-04-…" | null,
            "n": 22,
            "p10_cents": 18999,
            "p50_cents": 22499,
            "p90_cents": 27999,
            "min_cents": 15999,
            "max_cents": 31999,
            "trend_pct":  -3.4,          # % change older-half → newer-half median
            "buckets": [
                {
                    "bucket_start": "2026-02-01T…",
                    "bucket_end":   "2026-02-08T…",
                    "n": 4,
                    "median_cents": 22999,
                    "min_cents":    20999,
                    "max_cents":    25499,
                },
                ...                      # oldest → newest, empty buckets omitted
            ],
            "sources": ["ebay_finding", "serpapi"],
        }
    """
    if not comparables or len(comparables) < MIN_COMPARABLES:
        return None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    windowed: List[SoldComparable] = []
    for c in comparables:
        et = c.end_time
        if et is None:
            continue
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        # Skip obvious garbage timestamps (future-dated > 1d or outside window).
        if et < cutoff or et > now + timedelta(days=1):
            continue
        windowed.append(c)

    if len(windowed) < MIN_COMPARABLES:
        return None

    prices_all = [int(c.sold_price_cents) for c in windowed if c.sold_price_cents]
    if len(prices_all) < MIN_COMPARABLES:
        return None

    times_all = [
        (c.end_time if c.end_time.tzinfo else c.end_time.replace(tzinfo=timezone.utc))
        for c in windowed
    ]
    first_observed = min(times_all)
    last_observed = max(times_all)

    # Bucketize. Anchor buckets on `now` so the newest bucket is always "this
    # week"; that matches the frontend's mental model of the right edge.
    bucket_delta = timedelta(days=bucket_days)
    n_buckets = max(1, window_days // bucket_days)
    bucket_edges: List[datetime] = [
        now - bucket_delta * i for i in range(n_buckets, 0, -1)
    ] + [now]

    buckets_out: List[Dict[str, Any]] = []
    bucket_medians: List[int] = []   # parallel to buckets_out (non-empty only)

    for i in range(len(bucket_edges) - 1):
        start = bucket_edges[i]
        end = bucket_edges[i + 1]
        bucket_prices: List[int] = []
        for c, et in zip(windowed, times_all):
            if start <= et < end:
                bucket_prices.append(int(c.sold_price_cents))
        if not bucket_prices:
            continue  # frontend chart expects every bucket to have a median

        arr = np.asarray(bucket_prices, dtype=np.float64)
        median_cents = int(round(float(np.median(arr))))
        buckets_out.append({
            "bucket_start": start.isoformat(),
            "bucket_end": end.isoformat(),
            "n": len(bucket_prices),
            "median_cents": median_cents,
            "min_cents": int(min(bucket_prices)),
            "max_cents": int(max(bucket_prices)),
        })
        bucket_medians.append(median_cents)

    if len(buckets_out) < MIN_BUCKETS_WITH_DATA:
        return None

    # Trend: compare the median of the older half to the median of the newer
    # half of *bucket medians* (resistant to bucket-count noise).
    trend_pct: Optional[float] = None
    if len(bucket_medians) >= 2:
        mid = len(bucket_medians) // 2
        older = bucket_medians[:mid] if mid > 0 else [bucket_medians[0]]
        newer = bucket_medians[mid:] if mid < len(bucket_medians) else [bucket_medians[-1]]
        older_med = float(np.median(older))
        newer_med = float(np.median(newer))
        if older_med > 0:
            trend_pct = round((newer_med - older_med) / older_med * 100.0, 2)

    # Resolve sources. Prefer the detailed diag list when provided.
    sources_out = _extract_sources(sources)
    if not sources_out and source:
        sources_out = [source]

    return {
        "window_days": int(window_days),
        "first_observed": first_observed.isoformat(),
        "last_observed": last_observed.isoformat(),
        "n": len(prices_all),
        "p10_cents": _percentile_int(prices_all, 10),
        "p50_cents": _percentile_int(prices_all, 50),
        "p90_cents": _percentile_int(prices_all, 90),
        "min_cents": int(min(prices_all)),
        "max_cents": int(max(prices_all)),
        "trend_pct": trend_pct,
        "buckets": buckets_out,
        "sources": sources_out,
    }
