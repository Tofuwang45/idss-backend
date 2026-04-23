"""Post-recommendation refinement tests.

Covers the two bugs fixed for eBay / web-search follow-ups:

1. Bare budget fast-path (`$800-$1500`, `under $900`, `between 800 and 1500 …
   dollars, redo`) must be parsed into a `budget` refinement and trigger a
   re-search, without relying on the LLM intent classifier.

2. `pending_refine_slot` is set when the user taps "Change budget" /
   "Different brand" so the NEXT free-text message is interpreted as the
   value for that slot (and cleared after being applied).

3. When the user previously chose "Web search" (`commerce_search_mode ==
   "web_search"`), refinement results must go through the web-search handoff,
   NOT the catalog search.
"""

from __future__ import annotations

import asyncio
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.chat_endpoint import (
    ChatRequest,
    _handle_post_recommendation,
    _parse_bare_budget,
    _extract_budget_from_any_text,
    _BARE_BUDGET_RE,
)
from agent.interview.session_manager import (
    InterviewSessionState,
    InterviewSessionManager,
    STAGE_RECOMMENDATIONS,
)
from agent.universal_agent import UniversalAgent


# ---------------------------------------------------------------------------
# _parse_bare_budget — pure regex, no async
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg, expected",
    [
        ("$800-$1500", "$800-$1500"),
        ("800-1500", "$800-$1500"),
        ("$800 - $1500", "$800-$1500"),
        ("$800 to $1500", "$800-$1500"),
        ("between 800 and 1500 dollars", "$800-$1500"),
        ("keep it between 800 and 1500 dollars, redo", "$800-$1500"),
        ("$1,200-$1,800", "$1200-$1800"),
        ("under $800", "under $800"),
        ("less than 1000", "under $1000"),
        ("up to 1,500", "under $1500"),
        ("over $500", "over $500"),
        ("more than 2000", "over $2000"),
        ("$1200", "$1200"),
        ("1500 dollars", "$1500"),
    ],
)
def test_parse_bare_budget_matches(msg: str, expected: str) -> None:
    assert _parse_bare_budget(msg) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "",
        "Hello there",
        "I want a Dell laptop",
        "compare these",
        "research the first one",
        "tell me about the apple one",
        # Too ambiguous — one small digit count, no $ sign and no currency word
        "12",
        # Full sentence with a price embedded — must NOT be treated as bare budget;
        # the LLM refinement path handles these.
        "show me dell laptops under $800 with 16GB RAM",
    ],
)
def test_parse_bare_budget_non_matches(msg: str) -> None:
    assert _parse_bare_budget(msg) is None


# ---------------------------------------------------------------------------
# Session state: pending_refine_slot serializes + survives reset
# ---------------------------------------------------------------------------

def test_pending_refine_slot_default_none() -> None:
    s = InterviewSessionState()
    assert s.pending_refine_slot is None


def test_pending_refine_slot_round_trip() -> None:
    mgr = InterviewSessionManager()
    s = mgr.get_session("refine-slot-1")
    s.pending_refine_slot = "budget"
    d = mgr._state_to_dict(s)
    assert d["pending_refine_slot"] == "budget"
    restored = mgr._dict_to_state(d)
    assert restored.pending_refine_slot == "budget"


def test_reset_clears_pending_refine_slot() -> None:
    mgr = InterviewSessionManager()
    s = mgr.get_session("refine-slot-reset")
    s.pending_refine_slot = "brand"
    mgr.reset_session("refine-slot-reset")
    s2 = mgr.get_session("refine-slot-reset")
    assert s2.pending_refine_slot is None


# ---------------------------------------------------------------------------
# "Change budget" button sets pending_refine_slot + next message refines
# ---------------------------------------------------------------------------

def _make_rec_session(commerce_mode: str | None = None) -> InterviewSessionState:
    s = InterviewSessionState(
        active_domain="laptops",
        stage=STAGE_RECOMMENDATIONS,
    )
    s.agent_filters = {"brand": "Apple", "use_case": "Machine learning / AI"}
    s.explicit_filters = {"brand": "Apple"}
    s.commerce_search_mode = commerce_mode
    return s


def _make_sm(session: InterviewSessionState) -> MagicMock:
    sm = MagicMock()
    sm.add_message = MagicMock()
    sm.update_filters = MagicMock()
    sm._persist = MagicMock()
    sm.get_session = MagicMock(return_value=session)
    sm.set_stage = MagicMock()
    sm.set_last_recommendations = MagicMock()
    sm.set_last_recommendation_data = MagicMock()
    return sm


def test_change_budget_sets_pending_slot() -> None:
    session = _make_rec_session()
    sm = _make_sm(session)
    req = ChatRequest(message="Change budget", session_id="s-cb-1")

    resp = asyncio.run(_handle_post_recommendation(req, session, "s-cb-1", sm))

    assert resp is not None
    assert resp.response_type == "question"
    assert "budget" in resp.message.lower()
    assert session.pending_refine_slot == "budget"


# ---------------------------------------------------------------------------
# pending_refine_slot=budget + bare range → web-search handoff re-run
# ---------------------------------------------------------------------------

def test_pending_budget_routes_to_web_search_when_mode_is_web() -> None:
    session = _make_rec_session(commerce_mode="web_search")
    session.pending_refine_slot = "budget"
    sm = _make_sm(session)
    req = ChatRequest(message="$800-$1500", session_id="s-cb-web")

    fake_resp = MagicMock(name="FakeChatResponse")

    async def _fake_web_handoff(handoff, *_a, **_kw):
        # Verify the budget actually flowed into the search filters.
        assert handoff["domain"] == "laptops"
        assert handoff["search_filters"].get("price_max_cents") == 150000
        return fake_resp

    with patch("agent.chat_endpoint._handle_web_search_handoff",
               new=AsyncMock(side_effect=_fake_web_handoff)) as web_mock, \
         patch("agent.chat_endpoint._search_and_respond_ecommerce",
               new=AsyncMock()) as cat_mock:
        resp = asyncio.run(
            _handle_post_recommendation(req, session, "s-cb-web", sm)
        )

    assert resp is fake_resp
    web_mock.assert_called_once()
    cat_mock.assert_not_called()
    # Slot must be cleared after the value has been applied.
    assert session.pending_refine_slot is None


def test_pending_budget_routes_to_catalog_when_mode_is_catalog() -> None:
    session = _make_rec_session(commerce_mode="catalog")
    session.pending_refine_slot = "budget"
    sm = _make_sm(session)
    req = ChatRequest(message="under $900", session_id="s-cb-cat")

    fake_resp = MagicMock(name="FakeChatResponse")

    async def _fake_catalog(search_filters, category, domain, *_a, **_kw):
        assert domain == "laptops"
        assert search_filters.get("price_max_cents") == 90000
        return fake_resp

    with patch("agent.chat_endpoint._handle_web_search_handoff",
               new=AsyncMock()) as web_mock, \
         patch("agent.chat_endpoint._search_and_respond_ecommerce",
               new=AsyncMock(side_effect=_fake_catalog)) as cat_mock:
        resp = asyncio.run(
            _handle_post_recommendation(req, session, "s-cb-cat", sm)
        )

    assert resp is fake_resp
    web_mock.assert_not_called()
    cat_mock.assert_called_once()
    assert session.pending_refine_slot is None


# ---------------------------------------------------------------------------
# Deterministic fast-path without any pending slot:
# A message that's "just a price range" is still interpreted as a budget
# refinement and routed via the current commerce mode.
# ---------------------------------------------------------------------------

def test_bare_budget_without_pending_slot_routes_web_search() -> None:
    session = _make_rec_session(commerce_mode="web_search")
    session.pending_refine_slot = None
    sm = _make_sm(session)
    req = ChatRequest(message="keep it between 800 and 1500 dollars, redo",
                      session_id="s-bb-web")

    async def _fake_web_handoff(handoff, *_a, **_kw):
        assert handoff["search_filters"].get("price_max_cents") == 150000
        return "WEB_OK"

    with patch("agent.chat_endpoint._handle_web_search_handoff",
               new=AsyncMock(side_effect=_fake_web_handoff)) as web_mock:
        resp = asyncio.run(
            _handle_post_recommendation(req, session, "s-bb-web", sm)
        )

    assert resp == "WEB_OK"
    web_mock.assert_called_once()


# ---------------------------------------------------------------------------
# _extract_budget_from_any_text — looser extractor used when we KNOW the user
# is refining the budget (pending_refine_slot == "budget") or as a fallback
# after the strict anchored parser rejects the message.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg, expected",
    [
        ("keep it above $500", "over $500"),
        ("keep it above 500", "over $500"),
        ("bump to $1000", "$1000"),
        ("make it between 800 and 1500 dollars", "$800-$1500"),
        ("somewhere around 900, redo", "$900"),
        ("max 1200", "under $1200"),
        ("no more than 750 please", "under $750"),
        ("cap it at $2000", "under $2000"),
        ("at least 800", "over $800"),
        ("north of $500", "over $500"),
        ("Broaden to under $2,000", "under $2000"),
        # Falls through to plain extractor
        ("$1000", "$1000"),
    ],
)
def test_extract_budget_from_any_text_matches(msg: str, expected: str) -> None:
    assert _extract_budget_from_any_text(msg) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "better specs",
        "what are pros and cons",
        "show me something similar",
        "compare these",
        # Small digits that are NOT prices (screen size, option count).
        "12 inch screen",
        "option 1",
        "give me 3 options",
        "",
    ],
)
def test_extract_budget_from_any_text_non_matches(msg: str) -> None:
    assert _extract_budget_from_any_text(msg) is None


# ---------------------------------------------------------------------------
# UniversalAgent.get_search_filters — budget synonyms must produce the right
# ceiling/floor in cents, no matter how the phrasing was stashed on the slot.
# ---------------------------------------------------------------------------

def _agent_filters_for_budget(budget_value: str, domain: str = "laptops") -> dict:
    a = UniversalAgent(session_id="t-agent")
    a.active_domain = domain
    a.filters["budget"] = budget_value
    return a.get_search_filters()


def test_get_search_filters_above_sets_min_only() -> None:
    f = _agent_filters_for_budget("above $500")
    assert f.get("price_min_cents") == 50000
    assert "price_max_cents" not in f


def test_get_search_filters_over_sets_min_only() -> None:
    f = _agent_filters_for_budget("over $500")
    assert f.get("price_min_cents") == 50000
    assert "price_max_cents" not in f


def test_get_search_filters_at_least_sets_min() -> None:
    f = _agent_filters_for_budget("at least 1000")
    assert f.get("price_min_cents") == 100000
    assert "price_max_cents" not in f


def test_get_search_filters_between_sets_min_and_max() -> None:
    f = _agent_filters_for_budget("between 800 and 1500")
    assert f.get("price_min_cents") == 80000
    assert f.get("price_max_cents") == 150000


def test_get_search_filters_no_more_than_sets_max() -> None:
    f = _agent_filters_for_budget("no more than $750")
    assert f.get("price_max_cents") == 75000


def test_get_search_filters_em_dash_range() -> None:
    f = _agent_filters_for_budget("$800\u2014$1500")
    assert f.get("price_min_cents") == 80000
    assert f.get("price_max_cents") == 150000


# ---------------------------------------------------------------------------
# "Try again" re-runs the most recent web search with stored filters
# ---------------------------------------------------------------------------

def test_try_again_reruns_web_search_with_stored_filters() -> None:
    session = _make_rec_session(commerce_mode="web_search")
    session.last_web_search_filters = {
        "domain": "laptops",
        "search_filters": {"brand": "Apple", "price_max_cents": 100000},
        "original_message": "Apple laptop under $1000",
        "question_count": 3,
        "n_rows": 2,
        "n_per_row": 3,
        "compare_first": False,
    }
    sm = _make_sm(session)
    req = ChatRequest(message="Try again", session_id="s-retry-1")

    fake_resp = MagicMock(name="RetryResp")

    async def _fake_web_handoff(handoff, *_a, **_kw):
        assert handoff["domain"] == "laptops"
        assert handoff["search_filters"]["price_max_cents"] == 100000
        assert handoff["search_filters"]["brand"] == "Apple"
        return fake_resp

    with patch("agent.chat_endpoint._handle_web_search_handoff",
               new=AsyncMock(side_effect=_fake_web_handoff)) as web_mock:
        resp = asyncio.run(
            _handle_post_recommendation(req, session, "s-retry-1", sm)
        )

    assert resp is fake_resp
    web_mock.assert_called_once()


def test_try_again_without_stored_filters_falls_through() -> None:
    session = _make_rec_session(commerce_mode="web_search")
    session.last_web_search_filters = None
    sm = _make_sm(session)
    req = ChatRequest(message="Try again", session_id="s-retry-none")

    with patch("agent.chat_endpoint._handle_web_search_handoff",
               new=AsyncMock()) as web_mock:
        resp = asyncio.run(
            _handle_post_recommendation(req, session, "s-retry-none", sm)
        )

    # Falls through to the normal refinement pipeline — web handoff NOT fired
    web_mock.assert_not_called()
    # resp is either None (no handler claimed it) or the generic refine-fallback
    # ChatResponse — but crucially not our `_handle_web_search_handoff` mock.
    assert resp is None or getattr(resp, "response_type", None) in (
        "question", "recommendations"
    )


# ---------------------------------------------------------------------------
# "Broaden to under $N" phrasing from the zero-results quick-reply flows
# through the same bare-budget fast-path.
# ---------------------------------------------------------------------------

def test_broaden_quick_reply_applies_as_budget_refinement() -> None:
    session = _make_rec_session(commerce_mode="web_search")
    sm = _make_sm(session)
    req = ChatRequest(message="Broaden to under $2,000", session_id="s-broaden")

    async def _fake_web_handoff(handoff, *_a, **_kw):
        assert handoff["search_filters"].get("price_max_cents") == 200000
        return "BROADEN_OK"

    with patch("agent.chat_endpoint._handle_web_search_handoff",
               new=AsyncMock(side_effect=_fake_web_handoff)) as web_mock:
        resp = asyncio.run(
            _handle_post_recommendation(req, session, "s-broaden", sm)
        )

    assert resp == "BROADEN_OK"
    web_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Session serialization round-trip for last_web_search_filters
# ---------------------------------------------------------------------------

def test_last_web_search_filters_round_trip() -> None:
    mgr = InterviewSessionManager()
    s = mgr.get_session("lwsf-1")
    s.last_web_search_filters = {
        "domain": "laptops",
        "search_filters": {"brand": "Apple", "price_max_cents": 100000},
        "original_message": "",
        "question_count": 3,
        "n_rows": 2,
        "n_per_row": 3,
        "compare_first": False,
    }
    d = mgr._state_to_dict(s)
    assert d["last_web_search_filters"]["search_filters"]["price_max_cents"] == 100000
    restored = mgr._dict_to_state(d)
    assert restored.last_web_search_filters is not None
    assert restored.last_web_search_filters["domain"] == "laptops"
