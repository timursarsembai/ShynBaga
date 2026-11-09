import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from shynbaga import collector
from shynbaga.marketplaces import Blocked, MarketplaceError, NotFound, OfferList, SellerProfile
from shynbaga.marketplaces.kaspi import Kaspi, parse_seller_page
from tests.test_collector import T0, FakeMarket, offer, run

MERCHANT = {
    "uid": "30271313",
    "name": "OPPO Official store",
    "logo": "https://img.test/logo",
    "phone": "87076881010",
    "created": "2024-04-12T19:16:34.023",
    "merchantMetrics": {
        "cancelled": {"percentage": 0.8, "notEnoughData": False},
        "returned": {"percentage": 0.19, "notEnoughData": False},
        "lateKaspiDelivery": {"percentage": 0, "notEnoughData": True},
        "state": {"value": "TOP", "novice": False, "score": 4.8},
        "orderStatistics": {"ordersTotalSegment": "20000"},
    },
}


def page(data) -> str:
    return f"<html><script>BACKEND.components.merchant = {json.dumps(data)};BACKEND.x = 1;</script>"


def test_seller_page_is_parsed_without_the_phone():
    profile = parse_seller_page(page(MERCHANT))
    assert profile == SellerProfile(
        joined_on=date(2024, 4, 12),
        logo_url="https://img.test/logo",
        state="TOP",
        score=Decimal("4.80"),
        cancelled_pct=Decimal("0.800"),
        returned_pct=Decimal("0.190"),
        late_delivery_pct=None,  # Kaspi says it has too little data
        orders_segment=20000,
    )
    assert "87076881010" not in repr(profile)


def test_seller_page_spacing_does_not_matter():
    html = page(MERCHANT).replace("merchant = {", "merchant =\n  {", 1)
    assert parse_seller_page(html).state == "TOP"
    assert parse_seller_page(page(MERCHANT).replace("merchant = {", "merchant ={", 1)).state == "TOP"


def test_seller_page_without_data_is_an_error():
    with pytest.raises(MarketplaceError):
        parse_seller_page("<html>nothing</html>")
    with pytest.raises(MarketplaceError):
        parse_seller_page("BACKEND.components.merchant = {broken")


def test_malformed_seller_metrics_become_none():
    data = dict(MERCHANT, created=5, merchantMetrics={"cancelled": {"percentage": 400},
                                                       "orderStatistics": {"ordersTotalSegment": "many"}})
    profile = parse_seller_page(page(data))
    assert (profile.joined_on, profile.cancelled_pct, profile.orders_segment) == (None, None, None)


def test_fetch_seller_reads_the_address_tab_as_html():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, text=page(MERCHANT))

    market = Kaspi(delay=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert market.fetch_seller("Sulpak").state == "TOP"
    assert seen["request"].url.path == "/shop/info/merchant/sulpak/address-tab/"
    assert seen["request"].headers["accept"] == "text/html"


def test_missing_seller_page_is_not_found():
    market = Kaspi(delay=0, transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                   sleep=lambda s: None)
    with pytest.raises(NotFound):
        market.fetch_seller("gone")


class Profiled(FakeMarket):
    def __init__(self, offers, profiles):
        super().__init__(offers)
        self.profiles = profiles
        self.asked = []

    def fetch_seller(self, seller_id):
        self.asked.append(seller_id)
        return super().fetch_seller(seller_id)


PROFILE = SellerProfile(joined_on=date(2024, 4, 12), state="TOP", orders_segment=20000)


def test_crawl_profiles_sellers_and_skips_them_for_a_week(conn):
    offers = {"1": OfferList([offer("a", 100), offer("b", 90)])}
    first = Profiled(offers, {"a": PROFILE, "b": PROFILE})
    run(conn, first)
    assert sorted(first.asked) == ["a", "b"]
    rows = conn.execute(
        """SELECT s.external_id, s.joined_on, p.state, p.orders_segment
           FROM seller_profiles p JOIN sellers s ON s.id = p.seller_id ORDER BY 1"""
    ).fetchall()
    assert rows == [("a", date(2024, 4, 12), "TOP", 20000), ("b", date(2024, 4, 12), "TOP", 20000)]

    soon = Profiled(offers, {"a": PROFILE, "b": PROFILE})
    run(conn, soon, T0 + timedelta(days=6))
    assert soon.asked == []
    later = Profiled(offers, {"a": PROFILE, "b": PROFILE})
    run(conn, later, T0 + timedelta(days=8))
    assert sorted(later.asked) == ["a", "b"]


def test_gone_seller_is_marked_checked_and_failed_one_is_retried(conn):
    offers = {"1": OfferList([offer("a", 100), offer("b", 90)])}
    run(conn, Profiled(offers, {"b": MarketplaceError("HTTP 400")}))
    checked = dict(conn.execute("SELECT external_id, profile_checked_at IS NOT NULL FROM sellers"))
    assert checked == {"a": True, "b": False}
    assert conn.execute("SELECT count(*) FROM seller_profiles").fetchone()[0] == 0


def test_block_on_a_seller_page_marks_the_run_blocked(conn):
    offers = {"1": OfferList([offer("a", 100)])}
    result = run(conn, Profiled(offers, {"a": Blocked("HTTP 429")}))
    assert result.status == "blocked"
    assert result.products_ok == 1


def test_profiles_per_run_are_capped(conn, monkeypatch):
    monkeypatch.setattr(collector, "SELLER_PROFILES_PER_RUN", 1)
    offers = {"1": OfferList([offer("a", 100), offer("b", 90)])}
    market = Profiled(offers, {"a": PROFILE, "b": PROFILE})
    run(conn, market)
    assert len(market.asked) == 1
