from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shynbaga import collector
from shynbaga.marketplaces import Blocked, MarketplaceError, Offer, OfferList, Product

T0 = datetime(2026, 11, 1, tzinfo=UTC)
REGION = "750000000"


def offer(seller: str, price: int) -> Offer:
    return Offer(seller, f"Shop {seller}", Decimal(price), "KZT")


def product(external_id: str) -> Product:
    return Product(external_id=external_id, title=f"P{external_id}", offers_ref=external_id)


class FakeMarket:
    key = "fake"

    def __init__(self, offers: dict, pages: list | None = None):
        self.bytes_received = 7
        self.offers = offers
        self.pages = pages if pages is not None else [[product(k) for k in offers]]

    def list_products(self, category, region, page):
        item = self.pages[page] if page < len(self.pages) else []
        if isinstance(item, Exception):
            raise item
        return item

    def fetch_offers(self, item, region):
        answer = self.offers[item.external_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


def run(conn, market, at=T0, max_products=100):
    markets = market if isinstance(market, list) else [market]
    return collector.collect(conn, markets, "Cat", REGION, max_products, clock=lambda: at)


def intervals(conn):
    return conn.execute(
        """SELECT s.external_id, i.price, i.first_seen_at, i.last_seen_at, i.is_current
           FROM price_intervals i JOIN sellers s ON s.id = i.seller_id
           ORDER BY i.id"""
    ).fetchall()


def test_unchanged_price_extends_one_interval(conn):
    market = FakeMarket({"1": OfferList([offer("a", 100)])})
    run(conn, market)
    run(conn, market, T0 + timedelta(days=1))
    assert intervals(conn) == [("a", Decimal(100), T0, T0 + timedelta(days=1), True)]


def test_price_change_closes_old_and_opens_new(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)])}))
    t1 = T0 + timedelta(days=1)
    run(conn, FakeMarket({"1": OfferList([offer("a", 120)])}), t1)
    assert intervals(conn) == [
        ("a", Decimal(100), T0, T0, False),
        ("a", Decimal(120), t1, t1, True),
    ]


def test_each_seller_has_its_own_history(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100), offer("b", 90)])}))
    assert [row[0] for row in intervals(conn)] == ["a", "b"]


def test_departed_seller_is_closed_and_return_starts_a_new_interval(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100), offer("b", 90)])}))
    t1, t2 = T0 + timedelta(days=1), T0 + timedelta(days=2)
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)])}), t1)
    run(conn, FakeMarket({"1": OfferList([offer("a", 100), offer("b", 90)])}), t2)
    assert intervals(conn) == [
        ("a", Decimal(100), T0, t2, True),
        ("b", Decimal(90), T0, T0, False),
        ("b", Decimal(90), t2, t2, True),
    ]


def test_incomplete_list_does_not_close_absent_sellers(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100), offer("b", 90)])}))
    t1 = T0 + timedelta(days=1)
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)], complete=False)}), t1)
    assert [row[4] for row in intervals(conn)] == [True, True]


def test_long_gap_starts_a_new_interval_even_at_the_same_price(conn):
    market = FakeMarket({"1": OfferList([offer("a", 100)])})
    run(conn, market)
    later = T0 + timedelta(hours=49)
    run(conn, market, later)
    assert intervals(conn) == [
        ("a", Decimal(100), T0, T0, False),
        ("a", Decimal(100), later, later, True),
    ]


def test_same_seller_id_on_two_marketplaces_stays_separate(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)])}))
    other = FakeMarket({"1": OfferList([offer("a", 200)])})
    other.key = "other"
    run(conn, other)
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM sellers").fetchone()[0] == 2


def test_failed_product_does_not_touch_its_history(conn):
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)]), "2": OfferList([offer("b", 5)])}))
    result = run(
        conn,
        FakeMarket({"1": MarketplaceError("bad"), "2": OfferList([offer("b", 5)])}),
        T0 + timedelta(days=1),
    )
    assert result.status == "partial"
    assert (result.products_ok, result.products_failed) == (1, 1)
    assert [row[4] for row in intervals(conn)] == [True, True]


def test_repeated_blocks_stop_the_run(conn):
    offers = {str(i): Blocked("HTTP 429") for i in range(10)}
    result = run(conn, FakeMarket(offers))
    assert result.status == "blocked"
    assert result.products_failed == collector.MAX_CONSECUTIVE_BLOCKS
    row = conn.execute("SELECT status, finished_at, bytes_received FROM crawl_runs").fetchone()
    assert row == ("blocked", T0, 7)


def test_a_streak_of_failures_stops_the_run(conn):
    offers = {str(i): MarketplaceError("ProxyError on /x") for i in range(20)}
    result = run(conn, FakeMarket(offers))
    assert result.status == "failed"
    assert result.products_failed == collector.MAX_CONSECUTIVE_FAILURES


def test_failures_with_successes_between_them_do_not_stop_the_run(conn):
    offers = {
        str(i): MarketplaceError("x") if i % 2 else OfferList([offer("a", 100)])
        for i in range(20)
    }
    result = run(conn, FakeMarket(offers))
    assert (result.status, result.products_ok, result.products_failed) == ("partial", 10, 10)


def test_two_workers_share_the_products_and_both_work(conn):
    import threading
    import time

    class Recording(FakeMarket):
        def fetch_offers(self, item, region):
            time.sleep(0.01)  # let the other worker take its turn
            self.seen.append(item.external_id)
            return super().fetch_offers(item, region)

    offers = {str(i): OfferList([offer("a", 100 + i)]) for i in range(20)}
    first, second = Recording(offers), Recording(offers)
    first.seen, second.seen = [], []
    first.bytes_received, second.bytes_received = 3, 4
    result = run(conn, [first, second])
    assert (result.status, result.products_ok) == ("completed", 20)
    assert first.seen and second.seen
    assert sorted(first.seen + second.seen, key=int) == [str(i) for i in range(20)]
    assert conn.execute("SELECT bytes_received FROM crawl_runs").fetchone()[0] == 7
    assert conn.execute("SELECT count(*) FROM price_intervals").fetchone()[0] == 20
    assert threading.active_count() == 1


def test_blocks_from_parallel_workers_stop_every_worker(conn):
    import threading

    offers = {str(i): Blocked("HTTP 429") for i in range(50)}
    result = run(conn, [FakeMarket(offers), FakeMarket(offers)])
    assert result.status == "blocked"
    assert result.products_failed == collector.MAX_CONSECUTIVE_BLOCKS
    assert threading.active_count() == 1


def test_unexpected_worker_error_fails_the_run_and_is_raised(conn):
    import threading

    with pytest.raises(ZeroDivisionError):
        run(conn, FakeMarket({"1": ZeroDivisionError()}))
    assert conn.execute("SELECT status FROM crawl_runs").fetchone()[0] == "failed"
    assert threading.active_count() == 1


def test_single_block_is_tolerated(conn):
    result = run(conn, FakeMarket({"1": Blocked("x"), "2": OfferList([offer("a", 100)])}))
    assert result.status == "partial"
    assert result.products_ok == 1


def test_broken_later_page_keeps_the_products_already_found(conn):
    market = FakeMarket(
        {"1": OfferList([offer("a", 100)])},
        pages=[[product("1")], MarketplaceError("HTTP 400")],
    )
    result = run(conn, market)
    assert (result.status, result.products_ok, result.error) == ("partial", 1, "HTTP 400")
    assert conn.execute("SELECT error FROM crawl_runs").fetchone()[0] == "HTTP 400"


def test_broken_first_page_fails_the_run(conn):
    assert run(conn, FakeMarket({}, pages=[MarketplaceError("HTTP 400")])).status == "failed"


def test_block_during_discovery_is_recorded(conn):
    result = run(conn, FakeMarket({}, pages=[Blocked("HTTP 429")]))
    assert result.status == "blocked"
    assert conn.execute("SELECT status FROM crawl_runs").fetchone()[0] == "blocked"


def test_empty_category_fails_instead_of_looking_successful(conn):
    assert run(conn, FakeMarket({}, pages=[])).status == "failed"


def test_discovery_stops_when_the_server_repeats_a_page():
    calls = []

    class Repeating(FakeMarket):
        def list_products(self, category, region, page):
            calls.append(page)
            return [product("1")]

    assert len(collector.discover(Repeating({}), "Cat", REGION, 100)[0]) == 1
    assert calls == [0, 1]


def test_interrupted_run_is_recorded_as_failed(conn):
    class Interrupted(FakeMarket):
        def list_products(self, category, region, page):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(conn, Interrupted({}))
    assert conn.execute("SELECT status FROM crawl_runs").fetchone()[0] == "failed"


def test_discovery_stops_at_the_limit_and_deduplicates():
    market = FakeMarket({}, pages=[[product("1"), product("2")], [product("2"), product("3")]])
    found, error = collector.discover(market, "Cat", REGION, 3)
    assert [p.external_id for p in found] == ["1", "2", "3"]
    assert error is None
    assert len(collector.discover(market, "Cat", REGION, 1)[0]) == 1
