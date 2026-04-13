"""
Merchant Reliability Engine — comprehensive test suite.

Covers three layers with no live network calls:

  Layer 1 — merchant_reliability.py  (pure scoring, tier logic, risk flags)
  Layer 2 — ebay_seller.py           (OAuth, Browse API, BrowseSellerProfile)
  Layer 3 — /search/ebay + MRE       (end-to-end: Finding → parse → score → sort)
"""

import os
import sys
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# ── path + env setup ──────────────────────────────────────────────────────────
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv(override=True)

os.environ.setdefault("MCP_SKIP_PRELOAD", "1")
os.environ["MERCHANT_RELIABILITY_ENABLED"] = "1"
os.environ["MERCHANT_LLM_SENTIMENT"] = "0"

# ── App imports ───────────────────────────────────────────────────────────────
from app.merchant_reliability import (
    compute_merchant_report,
    _compute_risk_flags,
    _compute_tier,
    tier_sort_key,
    MerchantReport,
    TIER_HIGH, TIER_MEDIUM, TIER_LOW, TIER_DNB,
    is_mre_enabled,
)
from app.ebay_seller import (
    BrowseSellerProfile,
    get_application_access_token,
    is_browse_configured,
    _token_cache,
)

# For endpoint tests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.database import get_db

_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
_Session = sessionmaker(bind=_engine)


def _override_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_db


# ============================================================================
# Layer 1 — Merchant Reliability: Tier Logic
# ============================================================================

class TestTierLogic:
    """Deterministic tier assignment — strict precedence order."""

    def test_high_tier_top_rated_with_great_stats(self):
        tier = _compute_tier(
            top_rated=True, positive_feedback_pct=99.5,
            feedback_score=15000, risk_flags=[],
        )
        assert tier == TIER_HIGH

    def test_high_tier_minimum_thresholds(self):
        tier = _compute_tier(
            top_rated=True, positive_feedback_pct=98.0,
            feedback_score=500, risk_flags=[],
        )
        assert tier == TIER_HIGH

    def test_high_requires_top_rated(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=99.9,
            feedback_score=50000, risk_flags=[],
        )
        assert tier == TIER_MEDIUM

    def test_high_requires_pct_ge_98(self):
        tier = _compute_tier(
            top_rated=True, positive_feedback_pct=97.9,
            feedback_score=1000, risk_flags=[],
        )
        assert tier == TIER_MEDIUM

    def test_high_requires_score_ge_500(self):
        tier = _compute_tier(
            top_rated=True, positive_feedback_pct=99.0,
            feedback_score=499, risk_flags=[],
        )
        assert tier == TIER_MEDIUM

    def test_medium_tier_typical(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=96.5,
            feedback_score=200, risk_flags=[],
        )
        assert tier == TIER_MEDIUM

    def test_medium_tier_boundary(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=95.0,
            feedback_score=50, risk_flags=[],
        )
        assert tier == TIER_MEDIUM

    def test_low_tier_pct_below_95(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=94.9,
            feedback_score=100, risk_flags=[],
        )
        assert tier == TIER_LOW

    def test_low_tier_score_below_50(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=96.0,
            feedback_score=49, risk_flags=[],
        )
        assert tier == TIER_LOW

    def test_low_tier_all_none(self):
        tier = _compute_tier(
            top_rated=None, positive_feedback_pct=None,
            feedback_score=None, risk_flags=[],
        )
        assert tier == TIER_LOW

    def test_dnb_pct_below_90(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=89.9,
            feedback_score=500, risk_flags=[],
        )
        assert tier == TIER_DNB

    def test_dnb_at_exactly_90_is_not_dnb(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=90.0,
            feedback_score=10, risk_flags=[],
        )
        assert tier == TIER_LOW

    def test_dnb_new_account_with_low_pct(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=94.0,
            feedback_score=2, risk_flags=["NEW_ACCOUNT"],
        )
        assert tier == TIER_DNB

    def test_dnb_new_account_with_null_pct(self):
        tier = _compute_tier(
            top_rated=False, positive_feedback_pct=None,
            feedback_score=1, risk_flags=["NEW_ACCOUNT"],
        )
        assert tier == TIER_DNB

    def test_dnb_takes_precedence_over_high(self):
        """Even a top-rated seller with pct < 90 should be DO_NOT_BUY."""
        tier = _compute_tier(
            top_rated=True, positive_feedback_pct=85.0,
            feedback_score=10000, risk_flags=[],
        )
        assert tier == TIER_DNB


# ============================================================================
# Layer 1 — Merchant Reliability: Risk Flags
# ============================================================================

class TestRiskFlags:

    def test_low_volume(self):
        flags = _compute_risk_flags(feedback_score=9, positive_feedback_pct=99.0,
                                    has_return_policy=True, account_type=None)
        assert "LOW_VOLUME" in flags

    def test_new_account(self):
        flags = _compute_risk_flags(feedback_score=3, positive_feedback_pct=100.0,
                                    has_return_policy=True, account_type=None)
        assert "NEW_ACCOUNT" in flags
        assert "LOW_VOLUME" in flags

    def test_no_returns(self):
        flags = _compute_risk_flags(feedback_score=500, positive_feedback_pct=99.0,
                                    has_return_policy=False, account_type=None)
        assert "NO_RETURNS" in flags

    def test_individual_high_value(self):
        flags = _compute_risk_flags(feedback_score=100, positive_feedback_pct=96.0,
                                    has_return_policy=True, account_type="INDIVIDUAL",
                                    price_usd=750.0)
        assert "INDIVIDUAL_HIGH_VALUE" in flags

    def test_individual_low_value_no_flag(self):
        flags = _compute_risk_flags(feedback_score=100, positive_feedback_pct=96.0,
                                    has_return_policy=True, account_type="INDIVIDUAL",
                                    price_usd=200.0)
        assert "INDIVIDUAL_HIGH_VALUE" not in flags

    def test_business_high_value_no_flag(self):
        flags = _compute_risk_flags(feedback_score=100, positive_feedback_pct=96.0,
                                    has_return_policy=True, account_type="BUSINESS",
                                    price_usd=2000.0)
        assert "INDIVIDUAL_HIGH_VALUE" not in flags

    def test_clean_seller_no_flags(self):
        flags = _compute_risk_flags(feedback_score=5000, positive_feedback_pct=99.5,
                                    has_return_policy=True, account_type="BUSINESS")
        assert flags == []

    def test_none_return_policy_no_flag(self):
        """has_return_policy=None (unknown) should NOT trigger NO_RETURNS."""
        flags = _compute_risk_flags(feedback_score=100, positive_feedback_pct=96.0,
                                    has_return_policy=None, account_type=None)
        assert "NO_RETURNS" not in flags


# ============================================================================
# Layer 1 — Merchant Reliability: compute_merchant_report (async)
# ============================================================================

class TestComputeMerchantReport:

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_high_tier_report(self):
        report = self._run(compute_merchant_report(
            seller_username="megastore",
            feedback_score=5000,
            positive_feedback_pct=99.2,
            top_rated=True,
        ))
        assert isinstance(report, MerchantReport)
        assert report.reliability_tier == TIER_HIGH
        assert report.seller_username == "megastore"
        assert report.risk_flags == []
        assert "confidence" in report.negotiation_context.lower()

    def test_medium_tier_report(self):
        report = self._run(compute_merchant_report(
            seller_username="fair_deals",
            feedback_score=100,
            positive_feedback_pct=96.0,
            top_rated=False,
        ))
        assert report.reliability_tier == TIER_MEDIUM
        assert "return policy" in report.negotiation_context.lower()

    def test_low_tier_report(self):
        report = self._run(compute_merchant_report(
            seller_username="random_dude",
            feedback_score=15,
            positive_feedback_pct=92.0,
            top_rated=False,
        ))
        assert report.reliability_tier == TIER_LOW
        assert "caution" in report.negotiation_context.lower()

    def test_dnb_tier_report(self):
        report = self._run(compute_merchant_report(
            seller_username="scam_shop",
            feedback_score=5,
            positive_feedback_pct=50.0,
            top_rated=False,
        ))
        assert report.reliability_tier == TIER_DNB
        assert "alternative" in report.negotiation_context.lower()

    def test_report_includes_raw_signals(self):
        report = self._run(compute_merchant_report(
            seller_username="test",
            feedback_score=100,
            positive_feedback_pct=97.0,
            top_rated=False,
            feedback_rating_star="Blue",
            account_type="BUSINESS",
            has_return_policy=True,
        ))
        assert report.raw_signals["feedback_rating_star"] == "Blue"
        assert report.raw_signals["account_type"] == "BUSINESS"
        assert report.raw_signals["has_return_policy"] is True

    def test_report_with_browse_enrichment(self):
        """Browse data adds account_type and return policy info to risk flags."""
        report = self._run(compute_merchant_report(
            seller_username="individual_seller",
            feedback_score=100,
            positive_feedback_pct=96.0,
            top_rated=False,
            account_type="INDIVIDUAL",
            has_return_policy=False,
            price_usd=800.0,
        ))
        assert "INDIVIDUAL_HIGH_VALUE" in report.risk_flags
        assert "NO_RETURNS" in report.risk_flags

    def test_extra_fields_rejected(self):
        """MerchantReport uses extra='forbid'."""
        with pytest.raises(Exception):
            MerchantReport(
                seller_id="x", seller_username="x", reliability_tier="LOW",
                risk_flags=[], negotiation_context="", raw_signals={},
                bogus_field="should fail",
            )


# ============================================================================
# Layer 1 — tier_sort_key
# ============================================================================

class TestTierSortKey:

    def _make_report(self, tier):
        return MerchantReport(
            seller_id="x", seller_username="x", reliability_tier=tier,
            risk_flags=[], negotiation_context="", raw_signals={},
        )

    def test_sort_order(self):
        assert tier_sort_key(self._make_report(TIER_HIGH)) == 0
        assert tier_sort_key(self._make_report(TIER_MEDIUM)) == 1
        assert tier_sort_key(self._make_report(TIER_LOW)) == 2
        assert tier_sort_key(self._make_report(TIER_DNB)) == 3

    def test_none_sorts_last(self):
        assert tier_sort_key(None) == 99


# ============================================================================
# Layer 2 — ebay_seller.py: BrowseSellerProfile
# ============================================================================

class TestBrowseSellerProfile:

    def test_parses_full_browse_response(self):
        raw = {
            "itemId": "v1|123456|0",
            "seller": {
                "username": "pro_seller",
                "feedbackPercentage": "99.1",
                "feedbackScore": 8500,
                "sellerAccountType": "BUSINESS",
            },
            "returnTerms": {"returnsAccepted": True},
            "condition": "New",
        }
        profile = BrowseSellerProfile(raw)
        assert profile.username == "pro_seller"
        assert profile.feedback_percentage == 99.1
        assert profile.feedback_score == 8500
        assert profile.seller_account_type == "BUSINESS"
        assert profile.has_return_policy is True
        assert profile.condition == "New"
        assert profile.item_id == "v1|123456|0"

    def test_parses_minimal_browse_response(self):
        raw = {"seller": {"username": "seller_a"}}
        profile = BrowseSellerProfile(raw)
        assert profile.username == "seller_a"
        assert profile.feedback_percentage is None
        assert profile.feedback_score is None
        assert profile.seller_account_type is None
        assert profile.has_return_policy is False

    def test_missing_seller_block(self):
        raw = {"itemId": "v1|999|0"}
        profile = BrowseSellerProfile(raw)
        assert profile.username == ""
        assert profile.feedback_percentage is None

    def test_to_dict(self):
        raw = {
            "seller": {"username": "test", "feedbackScore": 100},
            "itemId": "v1|1|0",
        }
        d = BrowseSellerProfile(raw).to_dict()
        assert d["username"] == "test"
        assert d["feedback_score"] == 100
        assert isinstance(d, dict)


# ============================================================================
# Layer 2 — ebay_seller.py: OAuth token caching
# ============================================================================

class TestOAuthTokenCache:

    def setup_method(self):
        _token_cache["token"] = None
        _token_cache["expires_at"] = 0.0

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_returns_none_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EBAY_OAUTH_CLIENT_ID", None)
            os.environ.pop("EBAY_OAUTH_CLIENT_SECRET", None)
            token = self._run(get_application_access_token())
        assert token is None

    def test_returns_cached_token_when_valid(self):
        _token_cache["token"] = "cached-token-abc"
        _token_cache["expires_at"] = time.time() + 3600
        with patch.dict(os.environ, {
            "EBAY_OAUTH_CLIENT_ID": "id",
            "EBAY_OAUTH_CLIENT_SECRET": "secret",
        }):
            token = self._run(get_application_access_token())
        assert token == "cached-token-abc"

    def test_fetches_new_token_when_expired(self):
        _token_cache["token"] = "old"
        _token_cache["expires_at"] = 0.0
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "fresh-token-xyz",
            "expires_in": 7200,
        }
        with patch.dict(os.environ, {
            "EBAY_OAUTH_CLIENT_ID": "id",
            "EBAY_OAUTH_CLIENT_SECRET": "secret",
            "EBAY_ENVIRONMENT": "SANDBOX",
        }):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.post.return_value = mock_resp
                token = self._run(get_application_access_token())
        assert token == "fresh-token-xyz"
        assert _token_cache["token"] == "fresh-token-xyz"

    def test_returns_none_on_auth_failure(self):
        _token_cache["token"] = None
        _token_cache["expires_at"] = 0.0
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with patch.dict(os.environ, {
            "EBAY_OAUTH_CLIENT_ID": "id",
            "EBAY_OAUTH_CLIENT_SECRET": "secret",
        }):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.post.return_value = mock_resp
                token = self._run(get_application_access_token())
        assert token is None


class TestIsBrowseConfigured:

    def test_true_when_both_set(self):
        with patch.dict(os.environ, {
            "EBAY_OAUTH_CLIENT_ID": "id",
            "EBAY_OAUTH_CLIENT_SECRET": "secret",
        }):
            assert is_browse_configured() is True

    def test_false_when_id_missing(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("EBAY_OAUTH_CLIENT_ID", "EBAY_OAUTH_CLIENT_SECRET")}
        with patch.dict(os.environ, env, clear=True):
            assert is_browse_configured() is False


# ============================================================================
# Layer 3 — /search/ebay with MRE enrichment (end-to-end, mocked HTTP)
# ============================================================================

FINDING_RESPONSE_MULTI = {
    "findItemsByKeywordsResponse": [{
        "searchResult": [{
            "item": [
                {
                    "itemId": ["AAA111"],
                    "title": ["Budget Laptop LOW seller"],
                    "viewItemURL": ["https://www.ebay.com/itm/AAA111"],
                    "sellingStatus": [{"currentPrice": [{"@currencyId": "USD", "__value__": "300.00"}]}],
                    "condition": [{"conditionDisplayName": ["Used"]}],
                    "shippingInfo": [{"shippingType": ["Free"]}],
                    "sellerInfo": [{
                        "sellerUserName": ["random_dude"],
                        "feedbackScore": ["8"],
                        "positiveFeedbackPercent": ["91.0"],
                        "feedbackRatingStar": ["None"],
                        "topRatedSeller": ["false"]
                    }],
                },
                {
                    "itemId": ["BBB222"],
                    "title": ["Dell XPS HIGH seller"],
                    "viewItemURL": ["https://www.ebay.com/itm/BBB222"],
                    "sellingStatus": [{"currentPrice": [{"@currencyId": "USD", "__value__": "1200.00"}]}],
                    "condition": [{"conditionDisplayName": ["New"]}],
                    "shippingInfo": [{"shippingType": ["Free"]}],
                    "sellerInfo": [{
                        "sellerUserName": ["top_electronics_pro"],
                        "feedbackScore": ["24500"],
                        "positiveFeedbackPercent": ["99.8"],
                        "feedbackRatingStar": ["YellowShooting"],
                        "topRatedSeller": ["true"]
                    }],
                },
                {
                    "itemId": ["CCC333"],
                    "title": ["Scam Laptop DNB seller"],
                    "viewItemURL": ["https://www.ebay.com/itm/CCC333"],
                    "sellingStatus": [{"currentPrice": [{"@currencyId": "USD", "__value__": "150.00"}]}],
                    "condition": [{"conditionDisplayName": ["Used"]}],
                    "shippingInfo": [{"shippingType": ["Calculated"]}],
                    "sellerInfo": [{
                        "sellerUserName": ["scam_shop_666"],
                        "feedbackScore": ["3"],
                        "positiveFeedbackPercent": ["40.0"],
                        "feedbackRatingStar": ["None"],
                        "topRatedSeller": ["false"]
                    }],
                },
                {
                    "itemId": ["DDD444"],
                    "title": ["ThinkPad MEDIUM seller"],
                    "viewItemURL": ["https://www.ebay.com/itm/DDD444"],
                    "sellingStatus": [{"currentPrice": [{"@currencyId": "USD", "__value__": "800.00"}]}],
                    "condition": [{"conditionDisplayName": ["Refurbished"]}],
                    "shippingInfo": [{"shippingType": ["Free"]}],
                    "sellerInfo": [{
                        "sellerUserName": ["fair_deals_tx"],
                        "feedbackScore": ["320"],
                        "positiveFeedbackPercent": ["97.5"],
                        "feedbackRatingStar": ["Blue"],
                        "topRatedSeller": ["false"]
                    }],
                },
            ]
        }]
    }]
}


class TestSearchEbayWithMRE:
    """End-to-end: Finding API response → seller parsing → MRE scoring → sorted results."""

    def setup_method(self):
        self.client = TestClient(app)

    def test_mre_enriches_finding_results_and_sorts_by_tier(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = FINDING_RESPONSE_MULTI
        mock_resp.status_code = 200

        with patch.dict(os.environ, {
            "EBAY_APP_ID": "test-key",
            "MERCHANT_RELIABILITY_ENABLED": "1",
        }):
            # No Browse API configured — Finding-only MRE
            os.environ.pop("EBAY_OAUTH_CLIENT_ID", None)
            os.environ.pop("EBAY_OAUTH_CLIENT_SECRET", None)

            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "laptop", "limit": 10})

        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "api"
        assert len(data["results"]) == 4

        # All results should have merchant_report
        for r in data["results"]:
            assert r["merchant_report"] is not None
            assert "reliability_tier" in r["merchant_report"]
            assert "risk_flags" in r["merchant_report"]

        # Check sorting: HIGH first, then MEDIUM, then LOW, then DO_NOT_BUY
        tiers = [r["merchant_report"]["reliability_tier"] for r in data["results"]]
        tier_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "DO_NOT_BUY": 3}
        tier_nums = [tier_order[t] for t in tiers]
        assert tier_nums == sorted(tier_nums), f"Results not sorted by tier: {tiers}"

    def test_mre_assigns_correct_tiers(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = FINDING_RESPONSE_MULTI
        mock_resp.status_code = 200

        with patch.dict(os.environ, {
            "EBAY_APP_ID": "test-key",
            "MERCHANT_RELIABILITY_ENABLED": "1",
        }):
            os.environ.pop("EBAY_OAUTH_CLIENT_ID", None)
            os.environ.pop("EBAY_OAUTH_CLIENT_SECRET", None)

            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "laptop", "limit": 10})

        results = resp.json()["results"]
        by_title = {r["title"]: r for r in results}

        assert by_title["Dell XPS HIGH seller"]["merchant_report"]["reliability_tier"] == "HIGH"
        assert by_title["ThinkPad MEDIUM seller"]["merchant_report"]["reliability_tier"] == "MEDIUM"
        assert by_title["Budget Laptop LOW seller"]["merchant_report"]["reliability_tier"] == "LOW"
        assert by_title["Scam Laptop DNB seller"]["merchant_report"]["reliability_tier"] == "DO_NOT_BUY"

    def test_mre_risk_flags_propagated(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = FINDING_RESPONSE_MULTI
        mock_resp.status_code = 200

        with patch.dict(os.environ, {
            "EBAY_APP_ID": "test-key",
            "MERCHANT_RELIABILITY_ENABLED": "1",
        }):
            os.environ.pop("EBAY_OAUTH_CLIENT_ID", None)
            os.environ.pop("EBAY_OAUTH_CLIENT_SECRET", None)

            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "laptop", "limit": 10})

        results = resp.json()["results"]
        by_title = {r["title"]: r for r in results}

        dnb = by_title["Scam Laptop DNB seller"]["merchant_report"]
        assert "LOW_VOLUME" in dnb["risk_flags"]
        assert "NEW_ACCOUNT" in dnb["risk_flags"]

        low = by_title["Budget Laptop LOW seller"]["merchant_report"]
        assert "LOW_VOLUME" in low["risk_flags"]

    def test_mre_disabled_returns_no_reports(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = FINDING_RESPONSE_MULTI
        mock_resp.status_code = 200

        with patch.dict(os.environ, {
            "EBAY_APP_ID": "test-key",
            "MERCHANT_RELIABILITY_ENABLED": "0",
        }):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "laptop", "limit": 10})

        results = resp.json()["results"]
        for r in results:
            assert r["merchant_report"] is None

    def test_mre_seller_info_in_response(self):
        """Verify seller fields are present in the JSON response."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = FINDING_RESPONSE_MULTI
        mock_resp.status_code = 200

        with patch.dict(os.environ, {
            "EBAY_APP_ID": "test-key",
            "MERCHANT_RELIABILITY_ENABLED": "1",
        }):
            os.environ.pop("EBAY_OAUTH_CLIENT_ID", None)
            os.environ.pop("EBAY_OAUTH_CLIENT_SECRET", None)

            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "laptop", "limit": 10})

        results = resp.json()["results"]
        by_title = {r["title"]: r for r in results}
        high = by_title["Dell XPS HIGH seller"]

        assert high["seller"]["seller_username"] == "top_electronics_pro"
        assert high["seller"]["feedback_score"] == 24500
        assert high["seller"]["positive_feedback_pct"] == 99.8
        assert high["seller"]["top_rated_seller"] is True
        assert high["item_id"] == "BBB222"

    def test_rss_results_have_item_id_but_no_seller(self):
        """RSS path extracts item_id but has no seller info."""
        rss_xml = (
            '<?xml version="1.0"?><rss><channel>'
            '<item>'
            '<title>MacBook Air M2</title>'
            '<link>https://www.ebay.com/itm/998877665544</link>'
            '<description>$799.00 Free shipping</description>'
            '</item>'
            '</channel></rss>'
        )
        mock_resp = MagicMock()
        mock_resp.text = rss_xml

        with patch.dict(os.environ, {"MERCHANT_RELIABILITY_ENABLED": "1"}):
            os.environ.pop("EBAY_APP_ID", None)
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "macbook"})

        data = resp.json()
        assert data["source"] == "rss"
        r = data["results"][0]
        assert r["item_id"] == "998877665544"
        assert r["seller"] is None
        assert r["merchant_report"] is None

    def test_negotiation_context_is_human_readable(self):
        """The negotiation_context field should be a readable sentence."""
        mock_resp = MagicMock()
        single_item = {
            "findItemsByKeywordsResponse": [{
                "searchResult": [{
                    "item": [FINDING_RESPONSE_MULTI["findItemsByKeywordsResponse"][0]
                             ["searchResult"][0]["item"][1]]  # HIGH seller
                }]
            }]
        }
        mock_resp.json.return_value = single_item
        mock_resp.status_code = 200

        with patch.dict(os.environ, {
            "EBAY_APP_ID": "test-key",
            "MERCHANT_RELIABILITY_ENABLED": "1",
        }):
            os.environ.pop("EBAY_OAUTH_CLIENT_ID", None)
            os.environ.pop("EBAY_OAUTH_CLIENT_SECRET", None)
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
                resp = self.client.get("/search/ebay", params={"q": "dell xps"})

        ctx = resp.json()["results"][0]["merchant_report"]["negotiation_context"]
        assert len(ctx) > 20
        assert "trusted" in ctx.lower() or "confidence" in ctx.lower()


# ============================================================================
# Layer 3 — Feature flag behavior
# ============================================================================

class TestFeatureFlags:

    def test_is_mre_enabled_reads_env(self):
        with patch.dict(os.environ, {"MERCHANT_RELIABILITY_ENABLED": "1"}):
            assert is_mre_enabled() is True
        with patch.dict(os.environ, {"MERCHANT_RELIABILITY_ENABLED": "0"}):
            assert is_mre_enabled() is False

    def test_mre_defaults_to_disabled(self):
        env = {k: v for k, v in os.environ.items()
               if k != "MERCHANT_RELIABILITY_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            assert is_mre_enabled() is False
