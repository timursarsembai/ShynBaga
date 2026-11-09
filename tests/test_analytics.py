from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shynbaga import collector
from shynbaga.marketplaces import CardMetrics, Offer, OfferList, Product
from shynbaga.marketplaces.kaspi import parse_offers, parse_products
from tests.test_collector import REGION, T0, FakeMarket, run

CARD = {
    "id": "143681401",
    "title": "OPPO Reno",
    "unitPrice": 369990,
    "unitSalePrice": 349990,
    "priceMinusBonus": 339491,
    "createdTime": "2025-08-01T11:21:31.815Z",
    "rating": 5.0,
    "reviewsQuantity": 14,
    "deliveryDuration": "TODAY",
    "promo": [
        {"code": "SMARTPHONES_0024", "type": "LOAN", "priority": 24, "img": {"medium": {}}},
        {"code": "BONUS", "type": "BONUS", "bonusInCurrency": 10499, "img": {}},
        "garbage",
    ],
}


def test_card_metrics_are_parsed():
    [product] = parse_products({"data": [CARD]})
    m = product.metrics
    assert (m.rating, m.reviews_count) == (Decimal("5.00"), 14)
    assert (m.price, m.list_price, m.price_with_bonus) == (
        Decimal(349990),
        Decimal(369990),
        Decimal(339491),
    )
    assert m.delivery == "TODAY"
    assert product.listed_at == datetime(2025, 8, 1, 11, 21, 31, 815000, tzinfo=UTC)


def test_promos_keep_codes_and_bonus_but_not_images():
    [product] = parse_products({"data": [CARD]})
    assert product.metrics.promos == (
        {"code": "SMARTPHONES_0024", "type": "LOAN", "priority": 24},
        {"code": "BONUS", "type": "BONUS", "bonusInCurrency": 10499},
    )


def test_malformed_metrics_become_none():
    card = dict(CARD, rating="5", reviewsQuantity=True, createdTime="soon", promo="x")
    [product] = parse_products({"data": [card]})
    assert (product.metrics.rating, product.metrics.reviews_count) == (None, None)
    assert product.listed_at is None
    assert product.metrics.promos == ()


def test_out_of_range_rating_is_dropped():
    for bad in (10, -1, float("nan"), float("inf")):
        [product] = parse_products({"data": [dict(CARD, rating=bad)]})
        assert product.metrics.rating is None


def test_offer_carries_seller_rating():
    [parsed] = parse_offers(
        {
            "offers": [
                {
                    "merchantId": "1",
                    "price": 5,
                    "merchantRating": 4.9,
                    "merchantReviewsQuantity": 1568,
                }
            ]
        }
    )
    assert (parsed.seller_rating, parsed.seller_reviews_count) == (Decimal("4.90"), 1568)


def card_product(external_id: str, reviews: int) -> Product:
    return Product(
        external_id=external_id,
        title=f"P{external_id}",
        offers_ref=external_id,
        listed_at=T0 - timedelta(days=30),
        metrics=CardMetrics(
            rating=Decimal("4.5"),
            reviews_count=reviews,
            price=Decimal(100),
            list_price=Decimal(120),
            promos=({"code": "LOAN"},),
        ),
    )


def rated(seller: str, reviews: int) -> Offer:
    return Offer(seller, seller, Decimal(100), "KZT", seller_rating=Decimal("4.8"),
                 seller_reviews_count=reviews)


def test_crawl_records_a_snapshot_with_position_and_run(conn):
    market = FakeMarket(
        {"7": OfferList([rated("a", 10)], total=3), "8": OfferList([rated("a", 10)])},
        pages=[[card_product("7", 14), card_product("8", 2)]],
    )
    run(conn, market)
    rows = conn.execute(
        """SELECT p.external_id, s.position, s.reviews_count, s.list_price, s.promos,
                  s.offers_total, s.offers_seen, s.run_id IS NOT NULL
           FROM product_snapshots s JOIN products p ON p.id = s.product_id
           ORDER BY s.position"""
    ).fetchall()
    assert rows == [
        ("7", 1, 14, Decimal(120), [{"code": "LOAN"}], 3, 1, True),
        ("8", 2, 2, Decimal(120), [{"code": "LOAN"}], None, 1, True),
    ]
    listed = conn.execute("SELECT listed_at FROM products WHERE external_id = '7'").fetchone()[0]
    assert listed == T0 - timedelta(days=30)


def test_every_crawl_adds_a_snapshot(conn):
    market = FakeMarket({"7": OfferList([rated("a", 10)])}, pages=[[card_product("7", 14)]])
    run(conn, market)
    run(conn, market, T0 + timedelta(hours=6))
    assert conn.execute("SELECT count(*) FROM product_snapshots").fetchone()[0] == 2


def test_seller_rating_keeps_the_latest_value_of_each_day(conn):
    def crawl(at, reviews):
        run(conn, FakeMarket({"7": OfferList([rated("a", reviews)])},
                             pages=[[card_product("7", 1)]]), at)

    crawl(T0, 10)
    crawl(T0 + timedelta(hours=6), 11)
    crawl(T0 + timedelta(days=1), 15)
    rows = conn.execute(
        "SELECT observed_on, reviews_count FROM seller_snapshots ORDER BY observed_on"
    ).fetchall()
    assert rows == [(T0.date(), 11), ((T0 + timedelta(days=1)).date(), 15)]


def test_lookup_records_a_snapshot_without_card_fields(conn):
    class Tracking(FakeMarket):
        def fetch_product(self, external_id, region):
            return Product(external_id=external_id, title="P", offers_ref=external_id)

    collector.track(conn, Tracking({"9": OfferList([rated("a", 1)])}), "9", REGION,
                    clock=lambda: T0)
    row = conn.execute(
        "SELECT run_id, position, reviews_count, promos, offers_seen FROM product_snapshots"
    ).fetchone()
    assert row == (None, None, None, [], 1)
