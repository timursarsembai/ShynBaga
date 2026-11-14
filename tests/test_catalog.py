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
    Facet,
    Facets,
    FacetValue,
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
        self.facets = {}
        self.facet_calls = []

    def fetch_facets(self, category, filters, region):
        self.facet_calls.append(category + filters)
        found = self.facets.get(category + filters, Facets(0, []))
        if isinstance(found, Exception):
            raise found
        return found

    def fetch_category(self, code, region):
        self.category_calls.append(code)
        found = self.tree[code]
        if isinstance(found, Exception):
            raise found
        return found

    def list_products(self, category, region, page, filters="", sort="relevance"):
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
    market.listing_calls.clear()

    def clock():
        # Time runs out as soon as the first listing has been read.
        return T0 + timedelta(hours=7) if market.listing_calls else T0

    result = catalog.crawl(conn, [market], settings(tree_checks=0), clock=clock)
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
    assert db.next_sweep(conn, "fake", later, 3).code == "Lamps"


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


# Splitting big listings into parts.


def facet(facet_id, **counts):
    return Facet(
        facet_id,
        facet_id,
        [FacetValue(f":{facet_id}:{name}", name, count) for name, count in counts.items()],
    )


def test_split_takes_the_filter_that_opens_up_the_most_products():
    facets = Facets(
        1000,
        [
            facet("price", cheap=990, dear=10),  # reach 100 + 10
            facet("colour", red=400, blue=300, green=300),  # reach 300
            facet("manufacturerName", nobrand=900, a=50, b=50),  # reach 200
        ],
    )
    facet_id, values = catalog.choose_split(facets, [], listing_cap=100)
    assert facet_id == "colour"
    assert [v.term for v in values] == [":colour:red", ":colour:blue", ":colour:green"]


def test_split_prefers_brand_when_it_does_about_as_well():
    facets = Facets(
        1000, [facet("colour", red=400, blue=300, green=300), facet("manufacturerName", a=500, b=500, c=280)]
    )
    assert catalog.choose_split(facets, [], listing_cap=100)[0] == "manufacturerName"


def test_split_skips_used_filters_and_useless_ones():
    facets = Facets(1000, [facet("manufacturerName", a=500, b=500), facet("price", cheap=1000)])
    assert catalog.choose_split(facets, ["manufacturerName"], listing_cap=100) is None


def test_kaspi_filters_answer_is_read_into_facets():
    from shynbaga.marketplaces.kaspi import parse_facets

    payload = {
        "data": {
            "total": 1040192,
            "filters": [
                {
                    "id": "manufacturerName",
                    "title": "Бренд",
                    "rows": [
                        {"id": ":manufacturerName:LorrieX", "title": "LorrieX", "count": 614981},
                        {"id": ":manufacturerName:Empty", "title": "Empty", "count": 0},
                    ],
                },
                {"id": "broken", "rows": "nope"},
            ],
        }
    }
    found = parse_facets(payload, "Phone cases")
    assert found == Facets(
        1040192,
        [Facet("manufacturerName", "Бренд", [FacetValue(":manufacturerName:LorrieX", "LorrieX", 614981)])],
    )


BIG_TREE = {
    "Root": CategoryCheck(Category("Root", "all", 100, 0, False), [Category("Phones", "Phones", 100, 0, None)]),
    "Phones": CategoryCheck(section("Phones", 100, 900), [leaf("Cases", 100, 800)]),
}


def segments(conn):
    return conn.execute(
        """SELECT filters, facet_ids, products_count, popularity, is_split, gone
           FROM category_segments WHERE sort = 'relevance' ORDER BY filters"""
    ).fetchall()


def test_a_big_leaf_is_split_and_a_big_part_split_again(conn):
    market = FakeCatalog(BIG_TREE, {})
    market.facets = {
        "Cases": Facets(100, [facet("manufacturerName", A=60, B=30, C=10)]),
        "Cases:manufacturerName:A": Facets(
            60, [facet("manufacturerName", A=60, B=30), facet("price", cheap=34, dear=26)]
        ),
    }
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: T0)
    # Cases is 100 cards, one listing serves 36. Part A (60) splits again by price:
    # brand is used already.
    assert segments(conn) == [
        (":manufacturerName:A", ["manufacturerName"], 60, 120, True, False),
        (":manufacturerName:A:price:cheap", ["manufacturerName", "price"], 34, 68, None, False),
        (":manufacturerName:A:price:dear", ["manufacturerName", "price"], 26, 52, None, False),
        (":manufacturerName:B", ["manufacturerName"], 30, 60, None, False),
        (":manufacturerName:C", ["manufacturerName"], 10, 20, None, False),
    ]
    # Parts no bigger than one listing are not checked: nothing more to gain.
    assert sorted(market.facet_calls) == ["Cases", "Cases:manufacturerName:A"]


def test_parts_are_swept_with_their_filters_and_keep_the_leaf_place(conn):
    listings = {
        "Cases": [[card("1", 100), card("2", 200)]],
        "Cases:manufacturerName:B": [[card("2", 190), card("3", 300)]],
    }

    class Filtered(FakeCatalog):
        def list_products(self, category, region, page, filters="", sort="relevance"):
            return super().list_products(category + filters, region, page)

    market = Filtered(BIG_TREE, listings)
    market.facets = {"Cases": Facets(100, [facet("manufacturerName", A=30, B=30, C=40)])}
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: T0)
    # Parts wait from when they were made, so give them a day.
    later = T0 + timedelta(days=1)
    catalog.crawl(conn, [market], settings(max_sweeps=4), clock=lambda: later)
    swept = {code for code, page in market.listing_calls}
    assert {"Cases", "Cases:manufacturerName:B"} <= swept
    rows = conn.execute(
        "SELECT external_id, position, card_price FROM products ORDER BY external_id"
    ).fetchall()
    # Product 2 keeps its place in the whole leaf; product 3 was only in a part.
    assert rows == [("1", 1, Decimal(100)), ("2", 2, Decimal(190)), ("3", None, Decimal(300))]


def test_parts_that_vanish_go_with_their_own_parts(conn):
    market = FakeCatalog(BIG_TREE, {})
    market.facets = {
        "Cases": Facets(100, [facet("manufacturerName", A=60, B=40)]),
        "Cases:manufacturerName:A": Facets(60, [facet("price", cheap=40, dear=20)]),
        "Cases:manufacturerName:B": Facets(40, []),
    }
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: T0)
    later = T0 + catalog.TREE_EVERY + timedelta(hours=1)
    market.facets = {"Cases": Facets(100, [facet("manufacturerName", B=40, D=60)])}
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: later)
    gone = [row[0] for row in segments(conn) if row[5]]
    assert gone == [
        ":manufacturerName:A",
        ":manufacturerName:A:price:cheap",
        ":manufacturerName:A:price:dear",
    ]



def split_tree(conn, market, at=T0):
    catalog.crawl(conn, [market], settings(max_sweeps=0), clock=lambda: at)


TWO_BIG = {
    "Root": CategoryCheck(Category("Root", "all", 200, 0, False), [Category("Phones", "Phones", 200, 0, None)]),
    "Phones": CategoryCheck(section("Phones", 200, 900), [leaf("Cases", 100, 800), leaf("Bags", 100, 700)]),
}


def test_a_failed_filters_check_waits_and_lets_the_others_go(conn):
    market = FakeCatalog(TWO_BIG, {})
    market.facets = {
        "Cases": MarketplaceError("HTTP 500"),
        "Bags": Facets(100, [facet("manufacturerName", A=50, B=50)]),
    }
    split_tree(conn, market)
    split_tree(conn, market, T0 + timedelta(hours=1))
    # Bags got split although Cases, more popular, failed first.
    assert [row[0] for row in segments(conn)] == [":manufacturerName:A", ":manufacturerName:B"]
    assert market.facet_calls.count("Cases") == 1  # not again within SPLIT_RETRY
    split_tree(conn, market, T0 + catalog.SPLIT_RETRY + timedelta(minutes=1))
    assert market.facet_calls.count("Cases") == 2


def test_parts_of_a_leaf_now_small_enough_are_not_swept(conn):
    market = FakeCatalog(BIG_TREE, {})
    market.facets = {"Cases": Facets(100, [facet("manufacturerName", A=50, B=50)])}
    split_tree(conn, market)
    db.mark_swept(conn, "fake", "Cases", 36, T0 + timedelta(hours=23))
    assert db.next_sweep(conn, "fake", T0 + timedelta(days=1), 3).segment is not None
    conn.execute("UPDATE categories SET products_count = 30 WHERE code = 'Cases'")
    item = db.next_sweep(conn, "fake", T0 + timedelta(days=1), 3)
    assert item.code == "Cases" and item.segment is None


def test_new_parts_do_not_jump_ahead_of_a_popular_leaf(conn):
    market = FakeCatalog(TWO_BIG, {})
    market.facets = {"Cases": Facets(100, [facet("manufacturerName", A=50, B=50)])}
    split_tree(conn, market)
    # Bags was swept a while ago; the parts were made just now.
    db.mark_swept(conn, "fake", "Cases", 36, T0)
    db.mark_swept(conn, "fake", "Bags", 36, T0 - timedelta(hours=12))
    conn.commit()
    item = db.next_sweep(conn, "fake", T0 + timedelta(minutes=5), 3)
    assert (item.code, item.segment) == ("Bags", None)


def test_a_part_back_from_gone_is_checked_afresh(conn):
    market = FakeCatalog(BIG_TREE, {})
    market.facets = {
        "Cases": Facets(100, [facet("manufacturerName", A=60, B=40)]),
        "Cases:manufacturerName:A": Facets(60, [facet("price", cheap=30, dear=30)]),
    }
    split_tree(conn, market)
    t1 = T0 + catalog.TREE_EVERY + timedelta(hours=1)
    market.facets["Cases"] = Facets(100, [facet("manufacturerName", B=100)])
    split_tree(conn, market, t1)
    t2 = t1 + catalog.TREE_EVERY + timedelta(hours=1)
    market.facets["Cases"] = Facets(100, [facet("manufacturerName", A=60, B=40)])
    calls = len(market.facet_calls)
    split_tree(conn, market, t2)
    assert "Cases:manufacturerName:A" in market.facet_calls[calls:]
    part = [r for r in segments(conn) if r[0] == ":manufacturerName:A"][0]
    assert part[4:] == (True, False)  # split again, not gone


def test_a_part_kaspi_refuses_is_dropped_without_failing_the_round(conn):
    from shynbaga.marketplaces import BadRequest

    class Filtered(FakeCatalog):
        def list_products(self, category, region, page, filters="", sort="relevance"):
            return super().list_products(category + filters, region, page)

    bad = BadRequest("HTTP 400")
    listings = {
        "Cases": [[card("1", 100)]],
        "Cases:manufacturerName:A": [bad],
        "Cases:manufacturerName:B": [bad],
        "Cases:manufacturerName:C": [bad],
    }
    market = Filtered(BIG_TREE, listings)
    market.facets = {"Cases": Facets(100, [facet("manufacturerName", A=40, B=30, C=30)])}
    split_tree(conn, market)
    later = T0 + timedelta(days=1)
    result = catalog.crawl(conn, [market], settings(max_sweeps=10), clock=lambda: later)
    assert result.status == "completed"
    assert all(row[5] for row in segments(conn))


def test_overlapping_values_share_no_more_than_the_whole():
    market_values = [FacetValue(":colour:red", "red", 80), FacetValue(":colour:blue", "blue", 70)]
    reach = catalog.choose_split(Facets(100, [Facet("colour", "colour", market_values)]), [], 40)
    assert reach is not None  # 80 of reach, capped by the total 100: worth a split


def test_overlapping_parts_split_the_popularity(conn):
    market = FakeCatalog(BIG_TREE, {})
    market.facets = {"Cases": Facets(100, [facet("colour", red=80, blue=70, green=50)])}
    split_tree(conn, market)
    total = conn.execute(
        "SELECT sum(popularity) FROM category_segments WHERE parent_id IS NULL AND sort = 'relevance'"
    ).fetchone()[0]
    assert total == 200  # a quarter of 800, not more


# Other orders for listings filters will not split further.


class Sorted(FakeCatalog):
    """Listings keyed by category, filters and, for other orders, the sort."""

    def list_products(self, category, region, page, filters="", sort="relevance"):
        key = category + filters + ("" if sort == "relevance" else f"@{sort}")
        return super().list_products(key, region, page)


def variants(conn):
    return conn.execute(
        """SELECT filters, sort, products_count, popularity, gone FROM category_segments
           WHERE sort <> 'relevance' ORDER BY filters, sort"""
    ).fetchall()


def test_a_big_leaf_no_filter_splits_is_read_in_other_orders(conn):
    market = Sorted(BIG_TREE, {"Cases@price-asc": [[card("7", 5)]], "Cases": [[card("1", 100)]]})
    market.facets = {"Cases": Facets(100, [facet("price", cheap=100)])}  # no use
    split_tree(conn, market)
    assert variants(conn) == [
        ("", "created-desc", 100, 50, False),
        ("", "price-asc", 100, 50, False),
        ("", "price-desc", 100, 50, False),
    ]
    later = T0 + timedelta(days=1)
    catalog.crawl(conn, [market], settings(max_sweeps=10), clock=lambda: later)
    swept = {code for code, _ in market.listing_calls}
    assert {"Cases", "Cases@price-asc", "Cases@price-desc", "Cases@created-desc"} <= swept
    # The cheap end of the listing, beyond what relevance served: no place in the leaf.
    assert conn.execute(
        "SELECT position, card_price FROM products WHERE external_id = '7'"
    ).fetchone() == (None, Decimal(5))


def test_only_parts_filters_will_not_split_get_other_orders(conn):
    market = Sorted(BIG_TREE, {})
    market.facets = {
        "Cases": Facets(100, [facet("manufacturerName", A=70, B=30)]),
        "Cases:manufacturerName:A": Facets(70, [facet("price", cheap=70)]),  # cannot split
    }
    split_tree(conn, market)
    found = {(f, s) for f, s, *_ in variants(conn)}
    assert found == {(":manufacturerName:A", s) for s, _ in db.OTHER_SORTS}
    # A leaf that splits gets no orders of its own, nor a part one order covers.
    assert not any(f in ("", ":manufacturerName:B") for f, _ in found)


def test_orders_go_when_their_listing_splits_after_all(conn):
    market = Sorted(BIG_TREE, {})
    market.facets = {"Cases": Facets(100, [facet("price", cheap=100)])}
    split_tree(conn, market)
    assert len(variants(conn)) == 3
    later = T0 + catalog.TREE_EVERY + timedelta(hours=1)
    market.facets = {"Cases": Facets(100, [facet("manufacturerName", A=50, B=50)])}
    split_tree(conn, market, later)
    # The whole leaf's orders are gone; the new parts, which no filter splits, have theirs.
    assert all(row[4] for row in variants(conn) if row[0] == "")
    assert {row[0] for row in variants(conn) if not row[4]} == {
        ":manufacturerName:A",
        ":manufacturerName:B",
    }


def test_orders_are_not_read_once_the_listing_fits_one_order(conn):
    market = Sorted(BIG_TREE, {})
    market.facets = {"Cases": Facets(100, [facet("price", cheap=100)])}
    split_tree(conn, market)
    conn.execute("UPDATE category_segments SET products_count = 30 WHERE sort <> 'relevance'")
    db.mark_swept(conn, "fake", "Cases", 36, T0)
    item = db.next_sweep(conn, "fake", T0 + timedelta(days=3), 3)
    assert item.sort == "relevance"


def test_orders_of_a_part_that_shrank_are_not_read(conn):
    market = Sorted(BIG_TREE, {})
    market.facets = {
        "Cases": Facets(100, [facet("manufacturerName", A=70, B=30)]),
        "Cases:manufacturerName:A": Facets(70, [facet("price", cheap=70)]),
    }
    split_tree(conn, market)
    conn.execute(
        """UPDATE category_segments SET products_count = 20
           WHERE filters = ':manufacturerName:A' AND sort = 'relevance'"""
    )
    for code in ("Cases",):
        db.mark_swept(conn, "fake", code, 36, T0 + timedelta(days=3))
    picked = set()
    for hours in range(0, 72, 6):
        item = db.next_sweep(conn, "fake", T0 + timedelta(days=3, hours=hours), 3)
        picked.add(item.sort)
        if item.segment:
            db.mark_segment_swept(conn, item.segment, 0, T0 + timedelta(days=3, hours=hours))
    assert picked == {"relevance"}
