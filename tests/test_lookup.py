from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shynbaga import collector, db
from shynbaga.marketplaces import Offer, OfferList, identify
from shynbaga.marketplaces.kaspi import parse_offers, parse_product, product_url, seller_url
from tests.test_collector import REGION, T0, FakeMarket, offer, product, run


class TrackingMarket(FakeMarket):
    def fetch_product(self, external_id, region):
        self.fetched = external_id
        return product(external_id)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://kaspi.kz/shop/p/apple-iphone-18-177319119/?c=750000000", "177319119"),
        ("https://kaspi.kz/shop/p/apple-iphone-18-177319119/", "177319119"),
        ("https://kaspi.kz/shop/p/apple-iphone-18-177319119", "177319119"),
        ("kaspi.kz/shop/p/apple-iphone-18-177319119/", "177319119"),
        ("  https://www.kaspi.kz/shop/p/x-5/?ref=shared_link  ", "5"),
        ("https://kaspi.kz/shop/p/-180533183/", "180533183"),
        ("https://kaspi.kz/shop/almaty/p/x-5/", "5"),
        ("https://kaspi.kz/shop/p/krossovki-16540138b/", "16540138"),
    ],
)
def test_product_links_are_recognized(url, expected):
    assert identify(url) == ("kaspi", expected)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a link",
        "https://kaspi.kz/shop/",
        "https://kaspi.kz/shop/info/merchant/30174702/",
        "https://kaspi.kz.evil.example/shop/p/x-5/",
        "https://evilkaspi.kz/shop/p/x-5/",
        "https://example.com/?u=https://kaspi.kz/shop/p/x-5/",
        "http://[bad",
    ],
)
def test_other_links_are_rejected(url):
    assert identify(url) is None


def test_product_url_drops_the_city():
    assert product_url("/p/iphone-1/?c=750000000") == "https://kaspi.kz/shop/p/iphone-1/"


def test_seller_url_uses_the_working_lowercase_form():
    base = "https://kaspi.kz/shop/info/merchant"
    assert seller_url("Sulpak") == f"{base}/sulpak/address-tab/"
    assert seller_url("30174702") == f"{base}/30174702/address-tab/"
    assert seller_url("a/b") == f"{base}/a%2Fb/address-tab/"


def test_parsed_offer_carries_seller_name_and_url():
    [parsed] = parse_offers(
        {"offers": [{"merchantId": "Sulpak", "merchantName": "Sulpak LLC", "price": 10}]}
    )
    assert parsed.seller_name == "Sulpak LLC"
    assert parsed.seller_url == "https://kaspi.kz/shop/info/merchant/sulpak/address-tab/"


def test_parse_product_reads_the_card():
    parsed = parse_product(
        {
            "card": {"id": "5", "name": "Phone"},
            "galleryImages": [{"medium": "https://img.test/m.jpg"}],
            "masterCategoryName": "Смартфоны",
        },
        "5",
    )
    assert parsed.title == "Phone"
    assert parsed.url == "https://kaspi.kz/shop/p/-5/"
    assert parsed.image_url == "https://img.test/m.jpg"
    assert parsed.category_path == ("Смартфоны",)


def test_unknown_product_is_fetched_and_kept_in_later_runs(conn):
    market = TrackingMarket({"9": OfferList([offer("a", 100)])}, pages=[[]])
    collector.track(conn, market, "9", REGION, clock=lambda: T0)
    assert market.fetched == "9"

    crawl = TrackingMarket(
        {"1": OfferList([offer("b", 5)]), "9": OfferList([offer("a", 90)])},
        pages=[[product("1")]],
    )
    t1 = T0 + timedelta(days=1)
    assert run(conn, crawl, t1).products_ok == 2
    _, history = db.product_history(conn, "fake", "9", REGION)
    assert [(p.price, p.is_current) for p in history] == [
        (Decimal(100), False),
        (Decimal(90), True),
    ]


def test_fresh_known_product_is_not_fetched_again(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)])}))
    market = TrackingMarket({})  # any fetch would fail: it has no offers
    soon = T0 + collector.LOOKUP_FRESH_FOR - timedelta(minutes=1)
    collector.track(conn, market, "1", REGION, clock=lambda: soon)
    assert not hasattr(market, "fetched")
    assert db.requested_products(conn, "fake")[0].external_id == "1"


def test_stale_known_product_gets_fresh_prices_before_it_is_shown(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)])}))
    market = TrackingMarket({"1": OfferList([offer("a", 90)])})
    later = T0 + collector.LOOKUP_FRESH_FOR
    collector.track(conn, market, "1", REGION, clock=lambda: later)
    assert not hasattr(market, "fetched")  # the stored product is reused, not fetched
    info, history = db.product_history(conn, "fake", "1", REGION)
    assert info.last_seen_at == later
    assert [(p.price, p.is_current) for p in history] == [
        (Decimal(100), False),
        (Decimal(90), True),
    ]


def test_history_has_seller_name_and_url(conn):
    item = Offer("a", "Shop A", Decimal(100), "KZT", "https://shop.test/a")
    run(conn, FakeMarket({"1": OfferList([item])}))
    info, [period] = db.product_history(conn, "fake", "1", REGION)
    assert info.title == "P1"
    assert (period.seller, period.seller_url) == ("Shop A", "https://shop.test/a")


def test_unknown_history_is_none(conn):
    assert db.product_history(conn, "fake", "404", REGION) is None


def test_lowest_price_series_follows_the_cheapest_seller(conn):
    t1, t2, t3 = (T0 + timedelta(days=n) for n in (1, 2, 3))
    run(conn, FakeMarket({"1": OfferList([offer("a", 100), offer("b", 120)])}))
    run(conn, FakeMarket({"1": OfferList([offer("a", 100), offer("b", 120)])}), t1)
    run(conn, FakeMarket({"1": OfferList([offer("b", 120)])}), t2)
    run(conn, FakeMarket({"1": OfferList([offer("b", 90)])}), t3)
    _, history = db.product_history(conn, "fake", "1", REGION)
    assert db.lowest_prices(history) == [
        (T0, Decimal(100)),
        (t2, Decimal(120)),
        (t3, Decimal(90)),
    ]
