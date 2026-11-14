"""Read-only queries behind the admin pages. Rows come back as dicts."""

from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import dict_row

PAGE_SIZE = 50

# Summaries come from products' own columns, so a page is the LIMIT over products
# alone; the latest snapshot is looked up only for the rows shown. Card fields fall
# back to that snapshot for products not listed since the catalog crawl began.
_PRODUCT_FROM = """
FROM products p
LEFT JOIN categories c ON c.marketplace = p.marketplace AND c.code = p.category_code
"""

_PRODUCT_ROWS = """
SELECT p.id, p.marketplace, p.external_id, p.title, p.brand, p.category_path, p.url,
       p.first_seen_at, p.last_seen_at, p.requested_at, p.listed_at, p.card_seen_at,
       p.offers_checked_at, p.category_code, c.title AS category_title,
       p.offers_min_price AS min_price, p.offers_max_price AS max_price, p.sellers_now,
       p.offer_intervals AS intervals, p.card_changes,
       coalesce(p.position, snap.position) AS position,
       coalesce((p.card->>'rating')::numeric, snap.rating) AS rating,
       coalesce((p.card->>'reviews_count')::integer, snap.reviews_count) AS reviews_count,
       coalesce(p.card_price, snap.price) AS card_price,
       coalesce((p.card->>'list_price')::numeric, snap.list_price) AS list_price,
       coalesce((p.card->>'price_with_bonus')::numeric, snap.price_with_bonus)
           AS price_with_bonus,
       coalesce(p.card->>'delivery', snap.delivery) AS delivery,
       coalesce(p.card->'promos', snap.promos) AS promos,
       snap.offers_total
FROM (SELECT p.* {from_} {where} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s) p
LEFT JOIN categories c ON c.marketplace = p.marketplace AND c.code = p.category_code
LEFT JOIN LATERAL (
    SELECT position, rating, reviews_count, price, list_price, price_with_bonus, delivery,
           promos, offers_total
    FROM product_snapshots s WHERE s.product_id = p.id
    ORDER BY observed_at DESC LIMIT 1
) snap ON true
ORDER BY {outer_order}
"""

# Counting millions of rows for the pager is slow; past this the pager says "more".
COUNT_CAP = 10_000

# Sellers' lowest price where we have it, else the listing card's.
_PRICE = "coalesce(p.offers_min_price, p.card_price)"

# Each sort uses only products' columns: (inner order, outer order over the page).
PRODUCT_SORTS = {
    # The same estimate that picks products for sellers' prices; indexed.
    "popular": "p.score DESC NULLS LAST, p.id",
    "position": "p.position ASC NULLS LAST, p.id",
    "title": "p.title, p.id",
    "price": f"{_PRICE} ASC NULLS LAST, p.id",
    "price_desc": f"{_PRICE} DESC NULLS LAST, p.id",
    "sellers": "p.sellers_now DESC, p.id",
    "reviews": "(p.card->>'reviews_count')::integer DESC NULLS LAST, p.id",
    "changes": "p.offer_intervals + p.card_changes DESC, p.id",
    "seen": "p.last_seen_at DESC, p.id",
    "new": "p.first_seen_at DESC, p.id",
}

SELLER_SORTS = {
    "products": "products_now DESC, s.id",
    "name": "s.name, s.id",
    "reviews": "snap.reviews_count DESC NULLS LAST, s.id",
    "rating": "snap.rating DESC NULLS LAST, s.id",
    "joined": "s.joined_on ASC NULLS LAST, s.id",
    "cancelled": "prof.cancelled_pct DESC NULLS LAST, s.id",
    "seen": "s.last_seen_at DESC, s.id",
}


def _money(raw: str | None) -> Decimal | None:
    try:
        value = Decimal((raw or "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _cursor(conn: psycopg.Connection):
    return conn.cursor(row_factory=dict_row)


def products(
    conn: psycopg.Connection, *, q: str = "", brand: str = "", price_from: str = "",
    price_to: str = "", only: str = "", category: str = "", sort: str = "popular",
    page: int = 1,
) -> tuple[list[dict], int, bool]:
    """A page of products, the total, and whether the total stopped at COUNT_CAP."""
    where, params = [], {}
    q = q.strip()
    if q:
        where.append("(p.title ILIKE %(pat)s OR p.external_id = %(q)s OR p.brand ILIKE %(pat)s)")
        params |= {"q": q, "pat": _like(q)}
    if brand := brand.strip():
        where.append("p.brand ILIKE %(brand)s")
        params["brand"] = _like(brand)
    if category:
        where.append("p.category_code = %(category)s")
        params["category"] = category
    if (low := _money(price_from)) is not None:
        where.append(f"{_PRICE} >= %(low)s")
        params["low"] = low
    if (high := _money(price_to)) is not None:
        where.append(f"{_PRICE} <= %(high)s")
        params["high"] = high
    if only == "requested":
        where.append("p.requested_at IS NOT NULL")
    elif only == "changed":
        where.append("p.offer_intervals > p.sellers_now")
    elif only == "card_changed":
        where.append("p.card_changes > 0")
    elif only == "with_offers":
        where.append("p.sellers_now > 0")
    elif only == "no_offers":
        where.append("p.sellers_now = 0")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order = PRODUCT_SORTS.get(sort, PRODUCT_SORTS["popular"])
    sql = _PRODUCT_ROWS.format(
        from_=_PRODUCT_FROM, where=where_sql, order=order,
        # The page keeps its order: the same expression, read off the page's rows.
        outer_order=order,
    )
    params |= {"limit": PAGE_SIZE, "offset": (max(page, 1) - 1) * PAGE_SIZE}
    rows = _cursor(conn).execute(sql, params).fetchall()
    counted = conn.execute(
        f"SELECT count(*) FROM (SELECT 1 {_PRODUCT_FROM} {where_sql} LIMIT %(cap)s) x",
        params | {"cap": COUNT_CAP + 1},
    ).fetchone()[0]
    return rows, min(counted, COUNT_CAP), counted > COUNT_CAP


def _count_beyond_last_page(conn, sql: str, params: dict) -> int:
    # An empty page past the end still needs the total for the pager.
    if params["offset"] == 0:
        return 0
    row = _cursor(conn).execute(sql, params | {"offset": 0, "limit": 1}).fetchone()
    return row["total"] if row else 0


def product(conn: psycopg.Connection, product_id: int) -> dict | None:
    sql = _PRODUCT_ROWS.format(
        from_=_PRODUCT_FROM, where="WHERE p.id = %(id)s", order="p.id", outer_order="p.id"
    )
    return _cursor(conn).execute(sql, {"id": product_id, "limit": 1, "offset": 0}).fetchone()


def product_offers(conn: psycopg.Connection, product_id: int) -> list[dict]:
    """Every price stretch of every seller, current ones first."""
    return _cursor(conn).execute(
        """SELECT i.seller_id, s.name AS seller, s.url AS seller_url, i.region, i.price,
                  i.currency, i.first_seen_at, i.last_seen_at, i.is_current
           FROM price_intervals i JOIN sellers s ON s.id = i.seller_id
           WHERE i.product_id = %s
           ORDER BY i.is_current DESC, CASE WHEN i.is_current THEN i.price END,
                    s.name, i.first_seen_at DESC""",
        (product_id,),
    ).fetchall()


def card_prices(conn: psycopg.Connection, product_id: int) -> list[dict]:
    return _cursor(conn).execute(
        """SELECT region, price, list_price, first_seen_at, last_seen_at, is_current
           FROM card_prices WHERE product_id = %s ORDER BY first_seen_at DESC""",
        (product_id,),
    ).fetchall()


CATEGORY_SORTS = {
    "popular": "c.popularity DESC, c.code",
    "count": "c.products_count DESC, c.code",
    "swept": "c.swept_at DESC NULLS LAST, c.code",
    "title": "c.title, c.code",
}


def categories(
    conn: psycopg.Connection, *, q: str = "", only: str = "", sort: str = "popular",
    page: int = 1,
) -> tuple[list[dict], int]:
    where, params = ["NOT c.gone"], {}
    q = q.strip()
    if q:
        where.append("(c.title ILIKE %(pat)s OR c.code ILIKE %(pat)s)")
        params["pat"] = _like(q)
    if only == "leaves":
        where.append("c.is_leaf")
    elif only == "swept":
        where.append("c.swept_at IS NOT NULL")
    elif only == "unswept":
        where.append("c.is_leaf AND c.swept_at IS NULL")
    sql = f"""SELECT c.code, c.title, c.depth, c.products_count, c.popularity, c.is_leaf,
                     c.checked_at, c.swept_at, c.swept_products, parent.title AS parent_title,
                     count(*) OVER () AS total
              FROM categories c
              LEFT JOIN categories parent
                     ON parent.marketplace = c.marketplace AND parent.code = c.parent_code
              WHERE {" AND ".join(where)}
              ORDER BY {CATEGORY_SORTS.get(sort, CATEGORY_SORTS["popular"])}
              LIMIT %(limit)s OFFSET %(offset)s"""
    params |= {"limit": PAGE_SIZE, "offset": (max(page, 1) - 1) * PAGE_SIZE}
    rows = _cursor(conn).execute(sql, params).fetchall()
    total = rows[0]["total"] if rows else _count_beyond_last_page(conn, sql, params)
    return rows, total


def catalog_totals(conn: psycopg.Connection) -> dict:
    return _cursor(conn).execute(
        """SELECT count(*) FILTER (WHERE NOT gone) AS nodes,
                  count(*) FILTER (WHERE is_leaf AND NOT gone) AS leaves,
                  count(*) FILTER (WHERE swept_at IS NOT NULL AND NOT gone) AS swept,
                  count(*) FILTER (WHERE is_leaf IS NOT TRUE AND checked_at IS NULL
                                   AND NOT gone) AS unchecked,
                  coalesce(sum(products_count) FILTER (WHERE is_leaf AND NOT gone), 0)
                      AS leaf_products,
                  coalesce(sum(swept_products) FILTER (WHERE NOT gone), 0) AS swept_products
           FROM categories"""
    ).fetchone()


def product_snapshots(conn: psycopg.Connection, product_id: int) -> list[dict]:
    return _cursor(conn).execute(
        """SELECT run_id, region, observed_at, position, rating, reviews_count, price,
                  list_price, price_with_bonus, delivery, promos, offers_total, offers_seen
           FROM product_snapshots WHERE product_id = %s ORDER BY observed_at DESC""",
        (product_id,),
    ).fetchall()


_SELLER_ROWS = """
WITH cur AS (
    SELECT seller_id, count(DISTINCT product_id) AS products_now
    FROM price_intervals WHERE is_current GROUP BY seller_id
), ever AS (
    SELECT seller_id, count(DISTINCT product_id) AS products_ever
    FROM price_intervals GROUP BY seller_id
), snap AS (
    SELECT DISTINCT ON (seller_id) seller_id, rating, reviews_count
    FROM seller_snapshots ORDER BY seller_id, observed_on DESC
), prof AS (
    SELECT DISTINCT ON (seller_id) seller_id, observed_on, state, score, cancelled_pct,
           returned_pct, late_delivery_pct, orders_segment
    FROM seller_profiles ORDER BY seller_id, observed_on DESC
)
SELECT s.id, s.marketplace, s.external_id, s.name, s.url, s.joined_on, s.logo_url,
       s.profile_checked_at, s.first_seen_at, s.last_seen_at,
       coalesce(cur.products_now, 0) AS products_now,
       coalesce(ever.products_ever, 0) AS products_ever,
       snap.rating, snap.reviews_count,
       prof.observed_on AS profile_on, prof.state, prof.score, prof.cancelled_pct,
       prof.returned_pct, prof.late_delivery_pct, prof.orders_segment,
       count(*) OVER () AS total
FROM sellers s
LEFT JOIN cur ON cur.seller_id = s.id
LEFT JOIN ever ON ever.seller_id = s.id
LEFT JOIN snap ON snap.seller_id = s.id
LEFT JOIN prof ON prof.seller_id = s.id
"""


def sellers(
    conn: psycopg.Connection, *, q: str = "", only: str = "", sort: str = "products",
    page: int = 1,
) -> tuple[list[dict], int]:
    where, params = [], {}
    q = q.strip()
    if q:
        where.append("(s.name ILIKE %(pat)s OR s.external_id ILIKE %(q)s)")
        params |= {"q": q, "pat": _like(q)}
    if only == "profiled":
        where.append("prof.seller_id IS NOT NULL")
    elif only == "gone":
        where.append("cur.seller_id IS NULL")
    sql = _SELLER_ROWS
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {SELLER_SORTS.get(sort, SELLER_SORTS['products'])}"
    sql += " LIMIT %(limit)s OFFSET %(offset)s"
    params |= {"limit": PAGE_SIZE, "offset": (max(page, 1) - 1) * PAGE_SIZE}
    rows = _cursor(conn).execute(sql, params).fetchall()
    total = rows[0]["total"] if rows else _count_beyond_last_page(conn, sql, params)
    return rows, total


def seller(conn: psycopg.Connection, seller_id: int) -> dict | None:
    return _cursor(conn).execute(_SELLER_ROWS + " WHERE s.id = %s", (seller_id,)).fetchone()


def seller_products(conn: psycopg.Connection, seller_id: int) -> list[dict]:
    """Each product the seller ever offered: the seller's latest price against the
    lowest current price of anyone, and how many times the seller's price changed."""
    return _cursor(conn).execute(
        """WITH mine AS (
               SELECT DISTINCT ON (product_id) product_id, price, currency, is_current,
                      last_seen_at
               FROM price_intervals WHERE seller_id = %(s)s
               ORDER BY product_id, last_seen_at DESC
           ), stats AS (
               SELECT product_id, count(*) AS intervals, min(price) AS my_min,
                      max(price) AS my_max, min(first_seen_at) AS first_seen_at
               FROM price_intervals WHERE seller_id = %(s)s GROUP BY product_id
           ), best AS (
               SELECT product_id, min(price) AS best_price, count(*) AS sellers_now
               FROM price_intervals
               WHERE is_current AND product_id IN (SELECT product_id FROM mine)
               GROUP BY product_id
           )
           SELECT p.id, p.title, p.external_id, p.url, mine.price, mine.currency,
                  mine.is_current, mine.last_seen_at, stats.intervals, stats.my_min,
                  stats.my_max, stats.first_seen_at, best.best_price, best.sellers_now
           FROM mine JOIN products p ON p.id = mine.product_id
           JOIN stats ON stats.product_id = mine.product_id
           LEFT JOIN best ON best.product_id = mine.product_id
           ORDER BY mine.is_current DESC, p.title""",
        {"s": seller_id},
    ).fetchall()


def seller_history(conn: psycopg.Connection, seller_id: int) -> tuple[list[dict], list[dict]]:
    profiles = _cursor(conn).execute(
        """SELECT observed_on, state, score, cancelled_pct, returned_pct,
                  late_delivery_pct, orders_segment
           FROM seller_profiles WHERE seller_id = %s ORDER BY observed_on DESC""",
        (seller_id,),
    ).fetchall()
    ratings = _cursor(conn).execute(
        """SELECT observed_on, rating, reviews_count FROM seller_snapshots
           WHERE seller_id = %s ORDER BY observed_on DESC""",
        (seller_id,),
    ).fetchall()
    return profiles, ratings


def runs(conn: psycopg.Connection) -> list[dict]:
    return _cursor(conn).execute(
        """SELECT id, marketplace, category, region, started_at, finished_at, status,
                  products_ok, products_failed, bytes_received, error, cards_seen,
                  categories_checked, leaves_swept,
                  finished_at - started_at AS took
           FROM crawl_runs ORDER BY id DESC"""
    ).fetchall()


def totals(conn: psycopg.Connection) -> dict:
    """Row counts. The big tables use the planner's estimate: exact counts of
    millions of rows on every page load would be slow."""
    return _cursor(conn).execute(
        """SELECT (SELECT greatest(reltuples, 0)::bigint FROM pg_class
                   WHERE oid = 'products'::regclass) AS products,
                  (SELECT count(*) FROM sellers) AS sellers,
                  (SELECT greatest(reltuples, 0)::bigint FROM pg_class
                   WHERE oid = 'price_intervals'::regclass) AS intervals,
                  (SELECT greatest(reltuples, 0)::bigint FROM pg_class
                   WHERE oid = 'product_snapshots'::regclass) AS snapshots,
                  (SELECT greatest(reltuples, 0)::bigint FROM pg_class
                   WHERE oid = 'card_prices'::regclass) AS card_prices,
                  (SELECT count(*) FROM seller_profiles) AS profiles,
                  (SELECT count(*) FROM crawl_runs) AS runs"""
    ).fetchone()
