"""Read-only queries behind the admin pages. Rows come back as dicts."""

from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import dict_row

PAGE_SIZE = 50

# Latest card snapshot and current offers per product.
_PRODUCT_ROWS = """
WITH cur AS (
    SELECT product_id, min(price) AS min_price, max(price) AS max_price,
           count(*) AS sellers_now, max(last_seen_at) AS price_seen_at
    FROM price_intervals WHERE is_current GROUP BY product_id
), changes AS (
    SELECT product_id, count(*) AS intervals FROM price_intervals GROUP BY product_id
), snap AS (
    SELECT DISTINCT ON (product_id) product_id, position, rating, reviews_count,
           price AS card_price, list_price, price_with_bonus, delivery, promos,
           offers_total, observed_at
    FROM product_snapshots ORDER BY product_id, observed_at DESC
)
SELECT p.id, p.marketplace, p.external_id, p.title, p.brand, p.category_path, p.url,
       p.first_seen_at, p.last_seen_at, p.requested_at, p.listed_at,
       cur.min_price, cur.max_price, cur.sellers_now, cur.price_seen_at,
       coalesce(changes.intervals, 0) AS intervals,
       snap.position, snap.rating, snap.reviews_count, snap.card_price, snap.list_price,
       snap.price_with_bonus, snap.delivery, snap.promos, snap.offers_total,
       count(*) OVER () AS total
FROM products p
LEFT JOIN cur ON cur.product_id = p.id
LEFT JOIN changes ON changes.product_id = p.id
LEFT JOIN snap ON snap.product_id = p.id
"""

PRODUCT_SORTS = {
    "position": "snap.position ASC NULLS LAST, p.id",
    "title": "p.title, p.id",
    "price": "cur.min_price ASC NULLS LAST, p.id",
    "price_desc": "cur.min_price DESC NULLS LAST, p.id",
    "sellers": "cur.sellers_now DESC NULLS LAST, p.id",
    "reviews": "snap.reviews_count DESC NULLS LAST, p.id",
    "changes": "intervals DESC, p.id",
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


def brands(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL ORDER BY brand"
    ).fetchall()
    return [r[0] for r in rows]


def products(
    conn: psycopg.Connection, *, q: str = "", brand: str = "", price_from: str = "",
    price_to: str = "", only: str = "", sort: str = "position", page: int = 1,
) -> tuple[list[dict], int]:
    where, params = [], {}
    q = q.strip()
    if q:
        where.append("(p.title ILIKE %(pat)s OR p.external_id = %(q)s OR p.brand ILIKE %(pat)s)")
        params |= {"q": q, "pat": _like(q)}
    if brand:
        where.append("p.brand = %(brand)s")
        params["brand"] = brand
    if (low := _money(price_from)) is not None:
        where.append("cur.min_price >= %(low)s")
        params["low"] = low
    if (high := _money(price_to)) is not None:
        where.append("cur.min_price <= %(high)s")
        params["high"] = high
    if only == "requested":
        where.append("p.requested_at IS NOT NULL")
    elif only == "changed":
        where.append("coalesce(changes.intervals, 0) > coalesce(cur.sellers_now, 0)")
    elif only == "no_offers":
        where.append("cur.product_id IS NULL")
    sql = _PRODUCT_ROWS
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {PRODUCT_SORTS.get(sort, PRODUCT_SORTS['position'])}"
    sql += " LIMIT %(limit)s OFFSET %(offset)s"
    params |= {"limit": PAGE_SIZE, "offset": (max(page, 1) - 1) * PAGE_SIZE}
    rows = _cursor(conn).execute(sql, params).fetchall()
    total = rows[0]["total"] if rows else _count_beyond_last_page(conn, sql, params)
    return rows, total


def _count_beyond_last_page(conn, sql: str, params: dict) -> int:
    # An empty page past the end still needs the total for the pager.
    if params["offset"] == 0:
        return 0
    row = _cursor(conn).execute(sql, params | {"offset": 0, "limit": 1}).fetchone()
    return row["total"] if row else 0


def product(conn: psycopg.Connection, product_id: int) -> dict | None:
    return _cursor(conn).execute(_PRODUCT_ROWS + " WHERE p.id = %s", (product_id,)).fetchone()


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
                  products_ok, products_failed, bytes_received, error,
                  finished_at - started_at AS took
           FROM crawl_runs ORDER BY id DESC"""
    ).fetchall()


def totals(conn: psycopg.Connection) -> dict:
    return _cursor(conn).execute(
        """SELECT (SELECT count(*) FROM products) AS products,
                  (SELECT count(*) FROM sellers) AS sellers,
                  (SELECT count(*) FROM price_intervals) AS intervals,
                  (SELECT count(*) FROM product_snapshots) AS snapshots,
                  (SELECT count(*) FROM seller_profiles) AS profiles,
                  (SELECT count(*) FROM crawl_runs) AS runs"""
    ).fetchone()
