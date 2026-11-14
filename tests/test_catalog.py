from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from shynbaga import catalog, db
from shynbaga.marketplaces import (
    Blocked,
    CardMetrics,
    Category,
    CategoryCheck,
    MarketplaceError,
    Offer,
    OfferList,
    Product,
)
from shynbaga.marketplaces.kaspi import Kaspi, parse_category

T0 = datetime(2026, 11, 14, tzinfo=UTC)
REGION = "750000000"


def node(code, count=10, popularity=5, items=None, active=False):
    item = {
        "id": f":category:{code}",
        "title": code,
        "titleRu": f"{code} ru",
        "count": count,
        "popularity": popularity,
        "active": active,
    }
    if items is not None:
        item["items"] = items
    return item


def answer(items, total=100):
    return {"data": {"treeCategory": {"items": items}, "total": total}}


def test_root_answer_lists_top_level_nodes_without_trusting_their_children():
    check = parse_category(
        answer([node("Fashion", 900, 0, [node("Women", 500)]), node("Home", 800)]),
        "Categories",
    )
    assert [c.code for c in check.children] == ["Fashion", "Home"]
    assert all(c.is_leaf is None for c in check.children)
    assert check.node.is_leaf is False


def test_node_answer_finds_the_node_under_its_ancestors_and_marks_leaves():
    tree = [
        node(
            "Phones and gadgets",
            items=[
                node(
                    "Phone accessories",
                    300,
                    40,
                    active=True,
                    items=[
                        node("Phone cases", 200, 30, items=[]),
                        node("Chargers", 50, 9, items=[node("Wireless", 20)]),
                    ],
                )
            ],
        )
    ]
    check = parse_category(answer(tree), "Phone accessories")
    assert check.node == Category("Phone accessories", "Phone accessories ru", 300, 40, False)
    assert check.children == [
        Category("Phone cases", "Phone cases ru", 200, 30, True),
        Category("Chargers", "Chargers ru", 50, 9, False),
    ]


def test_node_without_children_is_a_leaf():
    check = parse_category(answer([node("TVs", 20, 3, items=[], active=True)]), "TVs")
    assert check.node.is_leaf is True
    assert check.children == []


def test_answer_without_the_node_is_an_error():
    with pytest.raises(MarketplaceError):
        parse_category(answer([node("Other")]), "TVs")
    with pytest.raises(MarketplaceError):
        parse_category({"data": {}}, "TVs")


def kaspi_answering(status: int) -> Kaspi:
    def handler(request):
        return httpx.Response(status, json={"data": []})

    return Kaspi(transport=httpx.MockTransport(handler), delay=0, retry_delays=())


def test_listing_past_the_deepest_page_reads_as_the_end():
    assert kaspi_answering(400).list_products("Smartphones", REGION, 305) == []


def test_bad_request_on_the_first_page_is_still_an_error():
    with pytest.raises(MarketplaceError):
        kaspi_answering(400).list_products("Smartphones", REGION, 0)


# A whole round against a fake marketplace.


def card(external_id, price, reviews=1):
    return Product(
        external_id=external_id,
        title=f"P{external_id}",
        offers_ref=external_id,
        metrics=CardMetrics(price=Decimal(price), reviews_count=reviews),
    )


class FakeCatalog:
    key = "fake"
    max_listing_pages = 3
    root_category = "Root"

    def __init__(self, tree, listings, offers=None):
        self.bytes_received = 0
        self.tree = tree
        self.listings = listings
        self.offers = offers or {}
        self.category_calls = []
        self.listing_calls = []

    def fetch_category(self, code, region):
        self.category_calls.append(code)
        found = self.tree[code]
        if isinstance(found, Exception):
            raise found
        return found

    def list_products(self, category, region, page):
        self.listing_calls.append((category, page))
        pages = self.listings.get(category, [])
        found = pages[page] if page < len(pages) else []
        if isinstance(found, Exception):
            raise found
        return found

    def fetch_offers(self, product, region):
        return self.offers.get(product.external_id, OfferList([]))

    def fetch_seller(self, seller_id):
        raise MarketplaceError("not needed")


def leaf(code, count, popularity):
    return Category(code, code, count, popularity, True)


def section(code, count, popularity):
    return Category(code, code, count, popularity, False)


TREE = {
    "Root": CategoryCheck(
        Category("Root", "all", 100, 0, False),
        [Category("Phones", "Phones", 60, 0, None), Category("Home", "Home", 40, 0, None)],
    ),
    "Phones": CategoryCheck(
        section("Phones", 60, 900), [leaf("Smartphones", 30, 800), leaf("Cases", 30, 50)]
    ),
    "Home": CategoryCheck(section("Home", 40, 300), [leaf("Lamps", 40, 200)]),
}


def settings(**changes):
    values = {
        "region": REGION,
        "root": "Root",
        "round_length": timedelta(hours=6),
        "tracked": 1,
    } | changes
    return catalog.Settings(**values)


def categories(conn):
    return conn.execute(
        """SELECT code, parent_code, depth, popularity, is_leaf, checked_at IS NOT NULL
           FROM categories ORDER BY depth, code"""
    ).fetchall()


def test_round_builds_the_tree_and_learns_leaves_from_their_parents(conn):
    market = FakeCatalog(TREE, {})
    result = catalog.crawl(conn, [market], settings(), clock=lambda: T0)
    assert result.status == "completed"
    assert result.categories_checked == 3
    # Leaves came with their parent's answer: no request of their own.
    assert sorted(market.category_calls) == ["Home", "Phones", "Root"]
    assert categories(conn) == [
        ("Root", None, 0, 0, False, True),
        ("Home", "Root", 1, 300, False, True),
        ("Phones", "Root", 1, 900, False, True),
        ("Cases", "Phones", 2, 50, True, True),
        ("Lamps", "Home", 2, 200, True, True),
        ("Smartphones", "Phones", 2, 800, True, True),
    ]


def test_popular_unchecked_nodes_go_first(conn):
    market = FakeCatalog(TREE, {})
    catalog.crawl(conn, [market], settings(tree_checks=1), clock=lambda: T0)
    catalog.crawl(conn, [market], settings(tree_checks=10), clock=lambda: T0)
    assert market.category_calls[0] == "Root"
    # Top-level nodes carry no real popularity before their own check, so they go
    # in turn; then the rest.
    assert sorted(market.category_calls[1:]) == ["Home", "Phones"]


def test_a_node_dropped_by_its_parent_is_gone(conn):
    market = FakeCatalog(TREE, {})
    catalog.crawl(conn, [market], settings(), clock=lambda: T0)
    later = T0 + catalog.TREE_EVERY + timedelta(hours=1)
    market.tree = dict(TREE)
    market.tree["Phones"] = CategoryCheck(section("Phones", 30, 900), [leaf("Smartphones", 30, 800)])
    catalog.crawl(conn, [market], settings(), clock=lambda: later)
    gone = conn.execute("SELECT code FROM categories WHERE gone").fetchall()
    assert gone == [("Cases",)]


def tree_known(conn, market):
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: T0)


def test_sweeps_go_by_popularity_and_keep_card_prices(conn):
    listings = {
        "Smartphones": [[card("1", 100), card("2", 200)], [card("3", 300)]],
        "Lamps": [[card("9", 50)]],
        "Cases": [[card("5", 10)]],
    }
    market = FakeCatalog(TREE, listings)
    tree_known(conn, market)
    result = catalog.crawl(conn, [market], settings(max_sweeps=2), clock=lambda: T0)
    assert result.leaves_swept == 2
    assert result.cards_seen == 4
    swept = conn.execute(
        "SELECT code, swept_products FROM categories WHERE swept_at IS NOT NULL ORDER BY code"
    ).fetchall()
    assert swept == [("Lamps", 1), ("Smartphones", 3)]
    rows = conn.execute(
        """SELECT p.external_id, p.category_code, p.position, p.card_price, c.price
           FROM products p JOIN card_prices c ON c.product_id = p.id ORDER BY p.external_id"""
    ).fetchall()
    assert rows == [
        ("1", "Smartphones", 1, Decimal(100), Decimal(100)),
        ("2", "Smartphones", 2, Decimal(200), Decimal(200)),
        ("3", "Smartphones", 3, Decimal(300), Decimal(300)),
        ("9", "Lamps", 1, Decimal(50), Decimal(50)),
    ]


def test_sweep_reads_no_more_pages_than_the_count_needs(conn):
    full = lambda n: [card(f"{n}-{i}", 100) for i in range(12)]  # noqa: E731
    market = FakeCatalog(TREE, {"Lamps": [full(0), full(1), full(2), full(3)]})
    market.max_listing_pages = 10
    tree_known(conn, market)
    for code in ("Smartphones", "Cases"):
        db.mark_swept(conn, "fake", code, 0, T0 + timedelta(days=1))
    conn.commit()
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    # 40 products make 4 pages of 12, though the listing would go on.
    assert [page for code, page in market.listing_calls if code == "Lamps"] == [0, 1, 2, 3]
    # Every page was full, so the leaf may have grown: the next sweep reads further.
    count = conn.execute("SELECT products_count FROM categories WHERE code = 'Lamps'").fetchone()
    assert count == (60,)


def test_unchanged_card_price_extends_one_stretch(conn):
    market = FakeCatalog(TREE, {"Lamps": [[card("9", 50)]]})
    tree_known(conn, market)
    for hours in (0, 6):
        db.mark_swept(conn, "fake", "Smartphones", 0, T0 + timedelta(days=1))
        db.mark_swept(conn, "fake", "Cases", 0, T0 + timedelta(days=1))
        conn.commit()
        catalog.crawl(
            conn, [market], settings(max_sweeps=1), clock=lambda h=hours: T0 + timedelta(hours=h)
        )
    market.listings["Lamps"] = [[card("9", 45)]]
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0 + timedelta(hours=12))
    rows = conn.execute(
        "SELECT price, first_seen_at, last_seen_at, is_current FROM card_prices ORDER BY id"
    ).fetchall()
    assert rows == [
        (Decimal(50), T0, T0 + timedelta(hours=6), False),
        (Decimal(45), T0 + timedelta(hours=12), T0 + timedelta(hours=12), True),
    ]


def test_tracked_products_get_sellers_prices_with_a_card_snapshot(conn):
    listings = {"Smartphones": [[card("1", 100, reviews=7), card("2", 200)]]}
    offers = {"1": OfferList([Offer("s1", "Shop", Decimal(99), "KZT")])}
    market = FakeCatalog(TREE, listings, offers)
    tree_known(conn, market)
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    # The next round fetches sellers' prices for the single most popular product.
    t1 = T0 + timedelta(hours=1)
    result = catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: t1)
    assert result.products_ok == 1
    snapshot = conn.execute(
        """SELECT p.external_id, s.position, s.price, s.reviews_count, s.offers_seen
           FROM product_snapshots s JOIN products p ON p.id = s.product_id"""
    ).fetchall()
    assert snapshot == [("1", 1, Decimal(100), 7, 1)]
    # Not again until OFFERS_EVERY has passed.
    again = catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: t1)
    assert again.products_ok == 0


def test_requested_products_are_tracked_too(conn):
    market = FakeCatalog(TREE, {"Smartphones": [[card("1", 100), card("2", 200)]]})
    tree_known(conn, market)
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    db.mark_requested(conn, "fake", "2", T0)
    conn.commit()
    result = catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: T0)
    assert result.products_ok == 2


def test_block_ends_the_round(conn):
    market = FakeCatalog(TREE, {"Smartphones": [Blocked("HTTP 429")]})
    tree_known(conn, market)
    result = catalog.crawl(conn, [market], settings(max_sweeps=5), clock=lambda: T0)
    assert result.status == "blocked"
    status = conn.execute("SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert status == ("blocked",)


def test_a_failing_leaf_is_skipped_and_the_round_goes_on(conn):
    listings = {"Smartphones": [MarketplaceError("HTTP 500")], "Lamps": [[card("9", 50)]]}
    market = FakeCatalog(TREE, listings)
    tree_known(conn, market)
    result = catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    assert result.status == "partial"
    assert result.leaves_swept == 1
    assert "Smartphones" in result.error


def test_listings_failing_again_and_again_stop_the_round(conn):
    error = MarketplaceError("HTTP 500")
    listings = {"Smartphones": [error], "Lamps": [error], "Cases": [error]}
    market = FakeCatalog(TREE, listings)
    tree_known(conn, market)
    result = catalog.crawl(conn, [market], settings(max_sweeps=5), clock=lambda: T0)
    assert result.status == "failed"


def test_round_stops_sweeping_when_its_time_is_up(conn):
    market = FakeCatalog(TREE, {"Smartphones": [[card("1", 100)]], "Lamps": [[card("9", 50)]]})
    tree_known(conn, market)
    moments = iter([T0, T0, T0, T0 + timedelta(hours=7)] + [T0 + timedelta(hours=8)] * 50)
    result = catalog.crawl(conn, [market], settings(tree_checks=0), clock=lambda: next(moments))
    assert result.leaves_swept == 1


def test_a_gone_node_takes_its_subtree_with_it(conn):
    market = FakeCatalog(TREE, {})
    tree_known(conn, market)
    later = T0 + catalog.TREE_EVERY + timedelta(hours=1)
    market.tree = dict(TREE)
    market.tree["Root"] = CategoryCheck(
        TREE["Root"].node, [Category("Home", "Home", 40, 0, None)]
    )
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: later)
    gone = conn.execute("SELECT code FROM categories WHERE gone ORDER BY code").fetchall()
    assert gone == [("Cases",), ("Phones",), ("Smartphones",)]
    assert db.next_leaf(conn, "fake", later, 3).code == "Lamps"


def test_a_section_that_became_a_leaf_drops_its_children(conn):
    market = FakeCatalog(TREE, {})
    tree_known(conn, market)
    later = T0 + catalog.TREE_EVERY + timedelta(hours=1)
    market.tree = dict(TREE)
    market.tree["Root"] = CategoryCheck(
        TREE["Root"].node,
        [Category("Phones", "Phones", 60, 0, None), Category("Home", "Home", 40, 0, None)],
    )
    market.tree["Phones"] = CategoryCheck(leaf("Phones", 60, 900), [])
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: later)
    rows = conn.execute(
        """SELECT code, is_leaf, gone FROM categories
           WHERE code IN ('Phones', 'Cases', 'Smartphones') ORDER BY code"""
    ).fetchall()
    assert rows == [("Cases", True, True), ("Phones", True, False), ("Smartphones", True, True)]


def test_a_vanished_listing_marks_the_leaf_gone(conn):
    from shynbaga.marketplaces import NotFound

    market = FakeCatalog(TREE, {"Smartphones": [NotFound("HTTP 404")], "Lamps": [[card("9", 50)]]})
    tree_known(conn, market)
    result = catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    assert result.status == "completed"
    assert conn.execute("SELECT gone FROM categories WHERE code = 'Smartphones'").fetchone() == (True,)


def test_failed_tree_checks_are_reported(conn):
    market = FakeCatalog({"Root": MarketplaceError("HTTP 500")}, {})
    result = catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: T0)
    assert result.status == "partial"
    assert "Root" in result.error


def test_tracked_products_come_back_every_12_hours_with_6_hour_rounds(conn):
    market = FakeCatalog(TREE, {"Smartphones": [[card("1", 100)]]})
    tree_known(conn, market)
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    t1 = T0 + timedelta(minutes=10)
    assert catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: t1).products_ok == 1
    # Checked at t1; the round starting a little under 12 hours later takes it.
    t2 = t1 + timedelta(hours=11, minutes=50)
    assert catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: t2).products_ok == 1


def test_an_old_card_is_not_snapshotted_as_current(conn):
    market = FakeCatalog(TREE, {"Smartphones": [[card("1", 100, reviews=7)]]})
    tree_known(conn, market)
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    later = T0 + catalog.OFFERS_EVERY + timedelta(hours=1)
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: later)
    snapshot = conn.execute("SELECT price, reviews_count, offers_seen FROM product_snapshots").fetchone()
    assert snapshot == (None, None, 0)


def test_products_fallen_out_of_their_listing_are_not_tracked(conn):
    market = FakeCatalog(TREE, {"Smartphones": [[card("1", 100), card("2", 90)]]})
    tree_known(conn, market)
    catalog.crawl(conn, [market], settings(max_sweeps=1), clock=lambda: T0)
    market.listings["Smartphones"] = [[card("2", 90)]]
    t1 = T0 + timedelta(days=2)
    for code in ("Lamps", "Cases"):
        db.mark_swept(conn, "fake", code, 0, t1 + timedelta(days=1))
    conn.commit()
    catalog.crawl(conn, [market], settings(max_sweeps=1, tracked=5), clock=lambda: t1)
    t2 = t1 + timedelta(minutes=1)
    due = db.products_due_offers(conn, "fake", 5, t2, t2 - catalog.OFFERS_EVERY)
    assert [p.external_id for p, _ in due] == ["2"]


def test_a_blocked_round_is_remembered_for_the_pause(conn):
    market = FakeCatalog(TREE, {"Smartphones": [Blocked("HTTP 429")]})
    tree_known(conn, market)
    assert db.last_failed_round(conn, "fake", catalog.CATALOG, REGION) is None
    catalog.crawl(conn, [market], settings(max_sweeps=5), clock=lambda: T0)
    assert db.last_failed_round(conn, "fake", catalog.CATALOG, REGION) == T0
    # A restart in between closes the killed run as interrupted; the pause still holds.
    db.start_run(conn, "fake", catalog.CATALOG, REGION, T0)
    db.close_abandoned_runs(conn, "fake", T0)
    assert db.last_failed_round(conn, "fake", catalog.CATALOG, REGION) == T0
