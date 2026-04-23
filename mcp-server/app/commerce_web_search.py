"""
Commerce Web Search — thin wrapper that converts agent interview filters
into an evaluated eBay search (MRE + FMV + deal engine).

Called from ``agent.chat_endpoint.process_chat`` when the user picks
**Web search** instead of the curated catalog.  Returns a list of dicts
that can be dropped straight into ``ChatResponse.web_market_listings``.

The label is "web search" (not "eBay search") because the same interface
will later support additional marketplaces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("mcp.commerce_web_search")


def _ensure_repo_env_loaded() -> None:
    """Load repo-root ``.env`` so SerpAPI/eBay keys exist when cwd is not the repo."""
    try:
        from pathlib import Path as _Path
        from dotenv import load_dotenv as _load_dotenv

        _env = _Path(__file__).resolve().parents[2] / ".env"
        if _env.is_file():
            _load_dotenv(_env)
    except Exception:
        pass


# --- Domain -> eBay leaf category --------------------------------------------
# https://pages.ebay.com/sellerinformation/news/categorychanges.html
_DOMAIN_TO_CATEGORY: Dict[str, str] = {
    "laptops": "177",   # PC Laptops & Netbooks
    "phones": "9355",   # Cell Phones & Smartphones
    "books": "267",     # Books
}

# ``use_case`` slot values that are genuinely discriminating on eBay
# (everything else — "student", "school", "work", "creative", "ML/AI" — is
# keyword noise that pulls in backpacks / planners / pencil cases).
_USEFUL_USE_CASE_TOKENS: Dict[str, str] = {
    "gaming": "gaming",
    "workstation": "workstation",
}

# Tokens we consider "laptop-y" when filtering titles post-search.
_LAPTOP_TITLE_TOKENS = {
    "laptop", "notebook", "macbook", "chromebook", "thinkpad", "ideapad",
    "pavilion", "inspiron", "xps", "zenbook", "rog", "latitude", "vostro",
    "elitebook", "probook", "surface", "yoga", "aspire", "nitro", "omen",
    "legion", "predator", "galaxy book",
}

# Titles that contain any of these terms but none of the laptop-y tokens are
# almost certainly not laptops.
_LAPTOP_OFFDOMAIN_TOKENS = {
    "backpack", "pencil case", "planner", "agenda", "tote bag",
    "school bag", "book bag", "case only", "sleeve only", "mousepad",
    "mouse pad", "stand only", "charger only", "cable only", "adapter only",
}

_PHONE_TITLE_TOKENS = {
    "phone", "smartphone", "iphone", "galaxy", "pixel", "oneplus",
    "xiaomi", "redmi", "nokia", "moto",
}
_PHONE_OFFDOMAIN_TOKENS = {
    "case only", "screen protector", "charger only", "cable only",
    "phone mount", "phone holder", "pouch only", "ring light",
}

_BOOK_TITLE_TOKENS = {
    "book", "paperback", "hardcover", "hardback", "novel", "textbook",
    "edition",
}
_BOOK_OFFDOMAIN_TOKENS = {
    "bookmark", "book stand", "book cover only", "book sleeve",
    "book light", "book bag",
}

# Specs that genuinely move the needle in an eBay title (kept short).
_SPEC_KEYS = ("min_ram_gb", "screen_size", "storage_type")


def _normalize_spec(key: str, value: Any) -> Optional[str]:
    """Turn a slot value into a token that's likely to appear in eBay titles."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if key == "min_ram_gb":
        # "16" -> "16GB"; "16GB" kept as-is
        m = re.match(r"^\s*(\d+)\s*(?:gb)?\s*$", s, re.IGNORECASE)
        if m:
            return f"{m.group(1)}GB"
        return s
    if key == "screen_size":
        # "14" or "14 in" or "14\"" -> '14"'
        m = re.match(r"^\s*(\d{2}(?:\.\d)?)", s)
        if m:
            return f'{m.group(1)}"'
        return s
    if key == "storage_type":
        return s.upper() if s.lower() in {"ssd", "hdd", "nvme"} else s
    return s


def _build_query(
    filters: Dict[str, Any],
    original_message: str,
    domain: str,
    refined: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a concise keyword query from interview slots + raw user text.

    Rules (see plan `fix_ebay_laptop_false_positives_e4f8aa06`):
      * Always include the product noun (laptop/phone/book).
      * Include brand when set and not "No preference".
      * Drop ``use_case`` from keywords except the narrow gaming/workstation
        case — "student"/"school"/"work" all poison eBay relevance.
      * Keep hard specs (RAM, screen size, storage).
      * Merge model-name tokens extracted by the LLM refiner if provided.
      * Cap at ~12 tokens / 120 chars.
    """
    parts: List[str] = []
    seen: set[str] = set()

    def _push(token: Optional[str]) -> None:
        if not token:
            return
        t = str(token).strip()
        if not t:
            return
        low = t.lower()
        if low in {"no preference", "none", "any"}:
            return
        if low in seen:
            return
        seen.add(low)
        parts.append(t)

    # 1. Brand from filters or refiner.
    brand_candidates = [
        (refined or {}).get("brand"),
        filters.get("brand"),
    ]
    for b in brand_candidates:
        _push(b)

    # 2. Model / series from the LLM refiner (before the product noun so the
    #    eBay relevance engine favours titles that contain the model name).
    for key in ("model", "series"):
        _push((refined or {}).get(key))

    # 3. Product noun.
    if domain == "laptops":
        _push("laptop")
    elif domain == "phones":
        _push("phone")
    elif domain == "books":
        _push("book")

    # 4. Useful use_case tokens only.
    use_case = filters.get("use_case")
    if use_case:
        low = str(use_case).strip().lower()
        # "gaming", "workstation", "gaming laptop" etc. — check tokens.
        for want, out in _USEFUL_USE_CASE_TOKENS.items():
            if want in low:
                _push(out)

    # 5. Hard specs from interview slots.
    for spec_key in _SPEC_KEYS:
        _push(_normalize_spec(spec_key, filters.get(spec_key)))

    # 6. Refiner-provided specs (e.g. ["16GB", "SSD"]).
    for spec in (refined or {}).get("specs", []) or []:
        _push(spec)

    # Cap tokens + chars.
    parts = parts[:12]
    query = " ".join(parts).strip()

    # If we still have nothing meaningful, fall back to the raw user message
    # (legacy behaviour). This mostly protects against empty filter bags.
    if len(query) < 4:
        query = (original_message or "").strip()

    return query[:120]


def _price_bounds_usd_from_filters(filters: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Derive (min_usd, max_usd) for live eBay search from catalog-style filters.

    Prefer ``price_min_cents`` / ``price_max_cents`` (from ``get_search_filters``).
    If those are absent, best-effort-parse ``budget`` (range, ``under $X``, etc.).
    """
    min_usd: Optional[float] = None
    max_usd: Optional[float] = None
    pmin = filters.get("price_min_cents")
    pmax = filters.get("price_max_cents")
    if pmin is not None:
        try:
            min_usd = float(int(pmin)) / 100.0
        except (TypeError, ValueError):
            pass
    if pmax is not None:
        try:
            max_usd = float(int(pmax)) / 100.0
        except (TypeError, ValueError):
            pass

    if min_usd is not None and max_usd is not None and min_usd > max_usd:
        min_usd, max_usd = max_usd, min_usd

    if min_usd is not None or max_usd is not None:
        return min_usd, max_usd

    budget = filters.get("budget")
    if budget is None:
        return None, None

    if isinstance(budget, (int, float)):
        try:
            v = int(budget)
            mx = v / 100.0 if v > 10_000 else float(v)
            return None, mx
        except (TypeError, ValueError):
            return None, None

    s = str(budget).strip()
    m_rng = re.search(
        r"(?:\$?\s*)?(\d{1,3}(?:,\d{3})*|\d+)\s*(?:-|\u2013|\u2014|to|and)\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)",
        s,
        re.IGNORECASE,
    )
    if m_rng:
        try:
            lo = float(m_rng.group(1).replace(",", ""))
            hi = float(m_rng.group(2).replace(",", ""))
            if 0 < lo <= hi:
                return lo, hi
        except ValueError:
            pass

    m_under = re.search(
        r"(?:under|below|less\s+than|up\s+to|at\s+most|max)\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)",
        s,
        re.IGNORECASE,
    )
    if m_under:
        try:
            return None, float(m_under.group(1).replace(",", ""))
        except ValueError:
            pass

    m_over = re.search(
        r"(?:over|above|at\s+least|min)\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)",
        s,
        re.IGNORECASE,
    )
    if m_over:
        try:
            return float(m_over.group(1).replace(",", "")), None
        except ValueError:
            pass

    return None, None


def _filter_listings_by_price_band(
    listings: List[Dict[str, Any]],
    min_usd: Optional[float],
    max_usd: Optional[float],
) -> List[Dict[str, Any]]:
    """Drop live listings outside the shopper's band (when any bound is set)."""
    if min_usd is None and max_usd is None:
        return listings
    lo_c = int(round(min_usd * 100)) if min_usd is not None else None
    hi_c = int(round(max_usd * 100)) if max_usd is not None else None
    out: List[Dict[str, Any]] = []
    for item in listings:
        pc = item.get("price_cents")
        if pc is None:
            continue
        try:
            pci = int(pc)
        except (TypeError, ValueError):
            continue
        if lo_c is not None and pci < lo_c:
            continue
        if hi_c is not None and pci > hi_c:
            continue
        out.append(item)
    return out


def _category_for_domain(domain: str) -> Optional[str]:
    return _DOMAIN_TO_CATEGORY.get((domain or "").lower())


# --- LLM refiner -------------------------------------------------------------

_REFINER_SYSTEM = (
    "You extract eBay listing keywords from a shopper's own words. "
    "Given a shopper message and product domain, return STRICT JSON with "
    "keys brand, model, series, specs. 'specs' is an array of short tokens "
    "like '16GB', 'SSD', '14\"'. Omit fields you cannot extract — do NOT "
    "invent brands or models. Never include use-case words like 'school', "
    "'student', 'work', 'gaming' in any field."
)


def _filters_are_thin(filters: Dict[str, Any]) -> bool:
    """Return True when interview slots don't give us enough to query eBay well."""
    brand = filters.get("brand")
    if brand and str(brand).strip().lower() not in {"", "no preference", "none", "any"}:
        return False
    for key in _SPEC_KEYS:
        if filters.get(key):
            return False
    return True


async def _extract_listing_keywords(
    original_message: str,
    domain: str,
) -> Optional[Dict[str, Any]]:
    """LLM helper: extract {brand, model, series, specs} from free user text.

    Returns ``None`` on any failure so callers can fall through cleanly.
    Gated by ``EBAY_LLM_REFINER`` env var (default on).
    """
    if os.getenv("EBAY_LLM_REFINER", "1").lower() in {"0", "false", "no"}:
        return None
    if not os.getenv("OPENAI_API_KEY"):
        return None

    msg = (original_message or "").strip()
    # Require at least two content tokens to avoid wasting an LLM call on
    # "Web search" or "Yes".
    tokens = [t for t in re.findall(r"[A-Za-z0-9\"'./-]+", msg) if len(t) > 1]
    if len(tokens) < 2:
        return None

    user_prompt = (
        f"Domain: {domain}\n"
        f"Shopper message: {msg}\n"
        "Return JSON like "
        '{"brand":"Dell","model":"XPS 13","series":null,"specs":["16GB","SSD"]}. '
        "Use null for unknown fields. Never include 'student', 'school', "
        "'work', 'gaming', or other use-case words."
    )

    try:
        from openai import OpenAI as _OpenAI  # type: ignore

        client = _OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=os.getenv("EBAY_LLM_REFINER_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": _REFINER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=120,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return None
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("ebay_llm_refiner_failed: %s", exc)
        return None

    if not isinstance(data, dict):
        return None

    # Sanitise: strip use-case words, coerce specs to list[str].
    blocked = {"student", "school", "work", "gaming", "creative", "ml", "ai",
               "business", "office", "home"}

    def _clean_str(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in blocked:
            return None
        return s

    cleaned: Dict[str, Any] = {}
    for key in ("brand", "model", "series"):
        val = _clean_str(data.get(key))
        if val:
            cleaned[key] = val
    raw_specs = data.get("specs") or []
    if isinstance(raw_specs, list):
        specs = [s for s in (_clean_str(x) for x in raw_specs) if s]
        if specs:
            cleaned["specs"] = specs[:4]

    return cleaned or None


# --- Post-search safety net --------------------------------------------------


def _post_filter_listings(
    listings: List[Dict[str, Any]],
    domain: str,
) -> List[Dict[str, Any]]:
    """Drop obviously off-domain titles.

    e.g. for ``domain='laptops'`` we drop titles that contain "backpack" /
    "pencil case" / "planner" unless they also contain a laptop-y token.
    """
    if not listings:
        return listings

    if domain == "laptops":
        off = _LAPTOP_OFFDOMAIN_TOKENS
        good = _LAPTOP_TITLE_TOKENS
    elif domain == "phones":
        off = _PHONE_OFFDOMAIN_TOKENS
        good = _PHONE_TITLE_TOKENS
    elif domain == "books":
        off = _BOOK_OFFDOMAIN_TOKENS
        good = _BOOK_TITLE_TOKENS
    else:
        return listings

    def _contains(title: str, tokens: set) -> bool:
        for tok in tokens:
            # Escape and allow word-boundary or phrase match so "book" doesn't
            # match "bookmark" but "pencil case" still matches literally.
            pat = re.escape(tok)
            if " " in tok:
                if re.search(pat, title):
                    return True
            else:
                if re.search(rf"\b{pat}\b", title):
                    return True
        return False

    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for item in listings:
        title = str(item.get("title") or "").lower()
        if not title:
            kept.append(item)
            continue
        has_off = _contains(title, off)
        has_good = _contains(title, good)
        if has_off and not has_good:
            dropped.append(title[:80])
            continue
        kept.append(item)

    if dropped:
        logger.info(
            "web_search_post_filter: domain=%s dropped=%d sample=%r",
            domain, len(dropped), dropped[:3],
        )
    return kept


# --- Public entrypoint -------------------------------------------------------


async def run_web_search(
    filters: Dict[str, Any],
    domain: str,
    original_message: str,
    limit: int = 5,
    listing_title_hints: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Execute an evaluated eBay search and return serialised listing dicts.

    Returns an empty list on any unrecoverable error so the caller can
    degrade gracefully.
    """
    _ensure_repo_env_loaded()
    try:
        from app.main import _tool_search_and_evaluate_ebay
    except ImportError:
        logger.error("commerce_web_search: cannot import _tool_search_and_evaluate_ebay")
        return []

    # Step 1: optional LLM refinement when interview slots are thin.
    refined: Optional[Dict[str, Any]] = None
    used_llm = False
    if _filters_are_thin(filters):
        try:
            refined = await _extract_listing_keywords(original_message, domain)
            used_llm = refined is not None
        except Exception as exc:
            logger.warning("ebay_llm_refiner_exception: %s", exc)
            refined = None

    query = _build_query(filters, original_message, domain, refined=refined)
    min_price, max_price = _price_bounds_usd_from_filters(filters)
    category_ids = _category_for_domain(domain)

    condition = None
    if filters.get("condition"):
        condition = str(filters["condition"]).lower()

    logger.info(
        "web_search_start: q=%r cat=%s min_price=%s max_price=%s cond=%s limit=%d llm_refined=%s",
        query, category_ids, min_price, max_price, condition, limit, used_llm,
    )

    try:
        resp = await _tool_search_and_evaluate_ebay(
            query=query,
            condition=condition,
            min_price=min_price,
            max_price=max_price,
            limit=limit,
            category_ids=category_ids,
            listing_title_hints=listing_title_hints,
            domain=domain,
        )
        listings = [item.model_dump() for item in resp.results]
        listings = _filter_listings_by_price_band(listings, min_price, max_price)
        filtered = _post_filter_listings(listings, domain)
        logger.info(
            "web_search_done: q=%r raw=%d kept=%d source=%s cat=%s llm_refined=%s",
            query, len(listings), len(filtered), resp.source, category_ids, used_llm,
        )
        return filtered
    except Exception as exc:
        logger.warning("web_search_error: %s", exc, exc_info=True)
        return []
