from datetime import datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from shynbaga.marketplaces import (
    CardMetrics,
    Category,
    CategoryCheck,
    FacetValue,
    Offer,
    OfferList,
    Product,
    SellerProfile,
)

# Past this without an observation, we no longer know the price held in between.
MAX_OBSERVATION_GAP = timedelta(hours=48)
# Listing cards are seen less often: most leaves come round every few days, and a
# new row per sweep for an unchanged price would only bloat the table. Within this
# gap an unchanged lowest price is taken to have held throughout.
CARD_OBSERVATION_GAP = timedelta(days=7)


class CurrentPrice(NamedTuple):
    id: int
    price: Decimal
    currency: str
    last_seen_at: datetime


class ProductInfo(NamedTuple):
    title: str
    url: str | None
    last_seen_at: datetime


class PricePeriod(NamedTuple):
    seller: str
    seller_url: str | None
    price: Decimal
    currency: str
    first_seen_at: datetime
    last_seen_at: datetime
    is_current: bool


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(files("shynbaga").joinpath("schema.sql").read_text(encoding="utf-8"))
    conn.commit()


def start_run(
    conn: psycopg.Connection, marketplace: str, category: str, region: str, now: datetime
) -> int:
    row = conn.execute(
        """INSERT INTO crawl_runs (marketplace, category, region, started_at)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (marketplace, category, region, now),
    ).fetchone()
    conn.commit()
    return row[0]


def close_abandoned_runs(conn: psycopg.Connection, marketplace: str, now: datetime) -> int:
    """Mark runs a killed collector left as running; only one collector runs at a time."""
    count = conn.execute(
        """UPDATE crawl_runs SET status = 'interrupted', finished_at = %s
           WHERE marketplace = %s AND status = 'running'""",
        (now, marketplace),
    ).rowcount
    conn.commit()
    return count


def last_run_started_at(
    conn: psycopg.Connection, marketplace: str, category: str, region: str
) -> datetime | None:
    # An interrupted run says nothing about the marketplace, so it does not count.
    row = conn.execute(
        """SELECT max(started_at) FROM crawl_runs
           WHERE marketplace = %s AND category = %s AND region = %s
             AND status <> 'interrupted'""",
        (marketplace, category, region),
    ).fetchone()
    return row[0]


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    now: datetime,
    *,
    status: str,
    products_ok: int,
    products_failed: int,
    bytes_received: int,
    error: str | None,
    cards_seen: int = 0,
    categories_checked: int = 0,
    leaves_swept: int = 0,
) -> None:
    conn.execute(
        """UPDATE crawl_runs
           SET finished_at = %s, status = %s, products_ok = %s, products_failed = %s,
               bytes_received = %s, error = %s, cards_seen = %s,
               categories_checked = %s, leaves_swept = %s
           WHERE id = %s""",
        (
            now,
            status,
            products_ok,
            products_failed,
            bytes_received,
            error,
            cards_seen,
            categories_checked,
            leaves_swept,
            run_id,
        ),
    )
    conn.commit()


def upsert_product(
    conn: psycopg.Connection, marketplace: str, product: Product, now: datetime
) -> int:
    row = conn.execute(
        """INSERT INTO products (marketplace, external_id, title, brand, category_path,
                                 url, image_url, offers_ref, listed_at,
                                 first_seen_at, last_seen_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (marketplace, external_id) DO UPDATE SET
               title = EXCLUDED.title,
               brand = EXCLUDED.brand,
               category_path = EXCLUDED.category_path,
               url = EXCLUDED.url,
               image_url = EXCLUDED.image_url,
               offers_ref = EXCLUDED.offers_ref,
               listed_at = COALESCE(EXCLUDED.listed_at, products.listed_at),
               last_seen_at = EXCLUDED.last_seen_at
           RETURNING id""",
        (
            marketplace,
            product.external_id,
            product.title,
            product.brand,
            list(product.category_path),
            product.url,
            product.image_url,
            product.offers_ref,
            product.listed_at,
            now,
            now,
        ),
    ).fetchone()
    return row[0]


def _upsert_seller(
    conn: psycopg.Connection, marketplace: str, offer: Offer, now: datetime
) -> int:
    row = conn.execute(
        """INSERT INTO sellers (marketplace, external_id, name, url,
                                first_seen_at, last_seen_at)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (marketplace, external_id) DO UPDATE SET
               name = EXCLUDED.name, url = EXCLUDED.url, last_seen_at = EXCLUDED.last_seen_at
           RETURNING id""",
        (marketplace, offer.seller_id, offer.seller_name, offer.seller_url, now, now),
    ).fetchone()
    if offer.seller_rating is not None or offer.seller_reviews_count is not None:
        conn.execute(
            """INSERT INTO seller_snapshots (seller_id, observed_on, rating, reviews_count)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (seller_id, observed_on) DO UPDATE SET
                   rating = EXCLUDED.rating, reviews_count = EXCLUDED.reviews_count""",
            (row[0], now.date(), offer.seller_rating, offer.seller_reviews_count),
        )
    return row[0]


def sellers_due_profile(
    conn: psycopg.Connection, marketplace: str, limit: int, checked_before: datetime
) -> list[str]:
    """Sellers never checked first, then those checked longest ago."""
    rows = conn.execute(
        """SELECT external_id FROM sellers
           WHERE marketplace = %s
             AND (profile_checked_at IS NULL OR profile_checked_at < %s)
           ORDER BY profile_checked_at NULLS FIRST, id
           LIMIT %s""",
        (marketplace, checked_before, limit),
    ).fetchall()
    return [row[0] for row in rows]


def record_seller_profile(
    conn: psycopg.Connection,
    marketplace: str,
    external_id: str,
    profile: SellerProfile | None,
    now: datetime,
) -> None:
    """Store a seller's page; None marks a seller whose page no longer exists."""
    row = conn.execute(
        """UPDATE sellers SET profile_checked_at = %s,
               joined_on = COALESCE(%s, joined_on), logo_url = COALESCE(%s, logo_url)
           WHERE marketplace = %s AND external_id = %s
           RETURNING id""",
        (
            now,
            profile.joined_on if profile else None,
            profile.logo_url if profile else None,
            marketplace,
            external_id,
        ),
    ).fetchone()
    if row is None or profile is None:
        return
    conn.execute(
        """INSERT INTO seller_profiles (seller_id, observed_on, state, score, cancelled_pct,
                                      returned_pct, late_delivery_pct, orders_segment)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (seller_id, observed_on) DO UPDATE SET
               state = EXCLUDED.state, score = EXCLUDED.score,
               cancelled_pct = EXCLUDED.cancelled_pct, returned_pct = EXCLUDED.returned_pct,
               late_delivery_pct = EXCLUDED.late_delivery_pct,
               orders_segment = EXCLUDED.orders_segment""",
        (
            row[0],
            now.date(),
            profile.state,
            profile.score,
            profile.cancelled_pct,
            profile.returned_pct,
            profile.late_delivery_pct,
            profile.orders_segment,
        ),
    )


def record_snapshot(
    conn: psycopg.Connection,
    product_id: int,
    region: str,
    product: Product,
    offers: OfferList,
    now: datetime,
    *,
    run_id: int | None = None,
    position: int | None = None,
) -> None:
    m = product.metrics
    conn.execute(
        """INSERT INTO product_snapshots
               (product_id, run_id, region, observed_at, position, rating, reviews_count,
                price, list_price, price_with_bonus, delivery, promos,
                offers_total, offers_seen)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            product_id,
            run_id,
            region,
            now,
            position,
            m.rating if m else None,
            m.reviews_count if m else None,
            m.price if m else None,
            m.list_price if m else None,
            m.price_with_bonus if m else None,
            m.delivery if m else None,
            Jsonb(list(m.promos) if m else []),
            offers.total,
            len(offers.offers),
        ),
    )


def record_offers(
    conn: psycopg.Connection,
    marketplace: str,
    product_id: int,
    region: str,
    offers: OfferList,
    now: datetime,
) -> None:
    # Callers upsert the product first in the same transaction. That row lock keeps a
    # lookup and the crawler from writing the same product at once.
    rows = conn.execute(
        """SELECT seller_id, id, price, currency, last_seen_at FROM price_intervals
           WHERE product_id = %s AND region = %s AND is_current""",
        (product_id, region),
    ).fetchall()
    current = {row[0]: CurrentPrice(*row[1:]) for row in rows}

    for offer in offers.offers:
        seller_id = _upsert_seller(conn, marketplace, offer, now)
        interval = current.pop(seller_id, None)
        if interval is not None:
            unchanged = (interval.price, interval.currency) == (offer.price, offer.currency)
            if unchanged and now - interval.last_seen_at <= MAX_OBSERVATION_GAP:
                conn.execute(
                    "UPDATE price_intervals SET last_seen_at = %s WHERE id = %s",
                    (now, interval.id),
                )
                continue
            _close(conn, interval.id)
        conn.execute(
            """INSERT INTO price_intervals
                   (product_id, seller_id, region, price, currency, first_seen_at, last_seen_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (product_id, seller_id, region, offer.price, offer.currency, now, now),
        )

    if offers.complete:
        for interval in current.values():
            _close(conn, interval.id)
    _summarize_offers(conn, product_id)


def _summarize_offers(conn: psycopg.Connection, product_id: int) -> None:
    conn.execute(
        """UPDATE products p SET offers_min_price = s.min_price,
               offers_max_price = s.max_price, sellers_now = s.sellers_now,
               offer_intervals = s.intervals
           FROM (SELECT min(price) FILTER (WHERE is_current) AS min_price,
                        max(price) FILTER (WHERE is_current) AS max_price,
                        count(*) FILTER (WHERE is_current) AS sellers_now,
                        count(*) AS intervals
                 FROM price_intervals WHERE product_id = %(id)s) s
           WHERE p.id = %(id)s""",
        {"id": product_id},
    )


def known_product(
    conn: psycopg.Connection, marketplace: str, external_id: str
) -> tuple[Product, datetime | None] | None:
    """The stored product and when its sellers' prices were last fetched (None if
    never: a product seen only in listings), or None if the product is new."""
    row = conn.execute(
        f"""SELECT {_PRODUCT_COLUMNS}, offers_checked_at
            FROM products WHERE marketplace = %s AND external_id = %s""",
        (marketplace, external_id),
    ).fetchone()
    if row is None:
        return None
    return _product(row), row[-1]


_PRODUCT_COLUMNS = """external_id, title, COALESCE(offers_ref, external_id), url, image_url,
                  brand, category_path, listed_at, card"""


def _product(row: tuple) -> Product:
    return Product(
        *row[:6], category_path=tuple(row[6]), listed_at=row[7], metrics=_metrics(row[8])
    )


def _card(metrics: CardMetrics) -> dict:
    return {
        "rating": _str(metrics.rating),
        "reviews_count": metrics.reviews_count,
        "price": _str(metrics.price),
        "list_price": _str(metrics.list_price),
        "price_with_bonus": _str(metrics.price_with_bonus),
        "delivery": metrics.delivery,
        "promos": list(metrics.promos),
    }


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _metrics(card: dict | None) -> CardMetrics | None:
    if not card:
        return None

    def money(name: str) -> Decimal | None:
        raw = card.get(name)
        return Decimal(raw) if raw is not None else None

    return CardMetrics(
        rating=money("rating"),
        reviews_count=card.get("reviews_count"),
        price=money("price"),
        list_price=money("list_price"),
        price_with_bonus=money("price_with_bonus"),
        delivery=card.get("delivery"),
        promos=tuple(card.get("promos") or ()),
    )


def mark_offers_checked(conn: psycopg.Connection, product_id: int, now: datetime) -> None:
    conn.execute("UPDATE products SET offers_checked_at = %s WHERE id = %s", (now, product_id))


def record_card(
    conn: psycopg.Connection,
    product_id: int,
    region: str,
    category_code: str,
    position: int | None,
    metrics: CardMetrics | None,
    now: datetime,
) -> None:
    """What a listing card showed: the product's latest card and its lowest-price history.

    `position` is the place in the whole leaf's listing; None for a card from a part
    of the leaf, which keeps the place and popularity estimate from the leaf's sweep.
    """
    price = metrics.price if metrics else None
    conn.execute(
        """UPDATE products p SET category_code = %(code)s,
               card = %(card)s, card_price = %(price)s, card_seen_at = %(now)s,
               position = coalesce(%(position)s, position),
               ranked_at = CASE WHEN %(position)s::integer IS NULL THEN ranked_at
                                ELSE %(now)s END,
               score = CASE WHEN %(position)s::integer IS NULL THEN score
                            ELSE (SELECT c.popularity FROM categories c
                                  WHERE c.marketplace = p.marketplace
                                    AND c.code = %(code)s)::float8 / %(position)s END
           WHERE id = %(id)s""",
        {
            "code": category_code,
            "position": position,
            "card": Jsonb(_card(metrics)) if metrics else None,
            "price": price,
            "now": now,
            "id": product_id,
        },
    )
    if price is None:
        return
    list_price = metrics.list_price
    current = conn.execute(
        """SELECT id, price, list_price, last_seen_at FROM card_prices
           WHERE product_id = %s AND region = %s AND is_current""",
        (product_id, region),
    ).fetchone()
    if current is not None:
        same = (current[1], current[2]) == (price, list_price)
        if same and now - current[3] <= CARD_OBSERVATION_GAP:
            conn.execute("UPDATE card_prices SET last_seen_at = %s WHERE id = %s", (now, current[0]))
            return
        conn.execute("UPDATE card_prices SET is_current = false WHERE id = %s", (current[0],))
        if current[1] != price:
            conn.execute(
                "UPDATE products SET card_changes = card_changes + 1 WHERE id = %s",
                (product_id,),
            )
    conn.execute(
        """INSERT INTO card_prices (product_id, region, price, list_price,
                                    first_seen_at, last_seen_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (product_id, region, price, list_price, now, now),
    )


def mark_requested(
    conn: psycopg.Connection, marketplace: str, external_id: str, now: datetime
) -> None:
    conn.execute(
        """UPDATE products SET requested_at = COALESCE(requested_at, %s)
           WHERE marketplace = %s AND external_id = %s""",
        (now, marketplace, external_id),
    )


def requested_products(conn: psycopg.Connection, marketplace: str) -> list[Product]:
    rows = conn.execute(
        f"""SELECT {_PRODUCT_COLUMNS}
            FROM products WHERE marketplace = %s AND requested_at IS NOT NULL""",
        (marketplace,),
    ).fetchall()
    return [_product(row) for row in rows]


def products_due_offers(
    conn: psycopg.Connection,
    marketplace: str,
    tracked: int,
    checked_before: datetime,
    card_fresh_after: datetime,
) -> list[tuple[Product, int | None]]:
    """Products whose sellers' prices are due, with their listing position.

    Tracked are products someone looked up, then the `tracked` most popular by an
    estimate: the category's popularity over the product's place in its listing.
    Only places from the leaf's latest sweep count. A card older than
    `card_fresh_after` is left out, so a snapshot never passes it off as current.
    """
    rows = conn.execute(
        """WITH top AS (
               SELECT p.id FROM products p
               JOIN categories c ON c.marketplace = p.marketplace AND c.code = p.category_code
               WHERE p.marketplace = %(m)s AND p.score IS NOT NULL
                 AND p.ranked_at >= c.swept_at AND NOT c.gone
               ORDER BY p.score DESC, p.id
               LIMIT %(n)s
           )
           SELECT external_id, title, COALESCE(offers_ref, external_id), url, image_url,
                  brand, category_path, listed_at,
                  CASE WHEN card_seen_at >= %(fresh)s THEN card END, position
           FROM products
           WHERE marketplace = %(m)s
             AND (id IN (SELECT id FROM top) OR requested_at IS NOT NULL)
             AND (offers_checked_at IS NULL OR offers_checked_at < %(before)s)
           ORDER BY requested_at IS NULL, score DESC NULLS LAST, id""",
        {"m": marketplace, "n": tracked, "before": checked_before, "fresh": card_fresh_after},
    ).fetchall()
    return [(_product(row), row[-1]) for row in rows]


ROOT_DEPTH = 0


def ensure_root(conn: psycopg.Connection, marketplace: str, code: str) -> None:
    conn.execute(
        """INSERT INTO categories (marketplace, code, title, depth)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (marketplace, code, "Все категории", ROOT_DEPTH),
    )
    conn.commit()


def categories_to_check(
    conn: psycopg.Connection, marketplace: str, limit: int, checked_before: datetime
) -> list[str]:
    """Nodes that may have children and were never checked or not lately; popular first."""
    rows = conn.execute(
        """SELECT code FROM categories
           WHERE marketplace = %s AND NOT gone AND is_leaf IS NOT TRUE
             AND (checked_at IS NULL OR checked_at < %s)
           ORDER BY checked_at IS NOT NULL, depth = 0 DESC, popularity DESC, code
           LIMIT %s""",
        (marketplace, checked_before, limit),
    ).fetchall()
    return [row[0] for row in rows]


def record_category(
    conn: psycopg.Connection, marketplace: str, check: CategoryCheck, now: datetime
) -> None:
    """Store a node's own answer: its figures, its children, and children it lost."""
    node = check.node
    row = conn.execute(
        """UPDATE categories SET products_count = %s, is_leaf = %s, checked_at = %s,
               popularity = CASE WHEN depth = 0 THEN popularity ELSE %s END
           WHERE marketplace = %s AND code = %s
           RETURNING depth""",
        (node.products_count, node.is_leaf, now, node.popularity, marketplace, node.code),
    ).fetchone()
    if row is None:
        raise ValueError(f"category {node.code} is not in the tree")
    depth = row[0]
    for child in check.children:
        _upsert_child(conn, marketplace, node.code, depth + 1, child, now)
    lost = conn.execute(
        """UPDATE categories SET gone = true
           WHERE marketplace = %s AND parent_code = %s AND NOT gone
             AND NOT (code = ANY(%s::text[]))
           RETURNING code""",
        (marketplace, node.code, [c.code for c in check.children]),
    ).fetchall()
    for (code,) in lost:
        _bury_below(conn, marketplace, code)


def mark_gone(conn: psycopg.Connection, marketplace: str, code: str) -> None:
    """The marketplace no longer has this node: drop it and everything under it."""
    conn.execute(
        "UPDATE categories SET gone = true WHERE marketplace = %s AND code = %s",
        (marketplace, code),
    )
    _bury_below(conn, marketplace, code)


def _bury_below(conn: psycopg.Connection, marketplace: str, code: str) -> None:
    conn.execute(
        """WITH RECURSIVE below AS (
               SELECT code FROM categories WHERE marketplace = %(m)s AND parent_code = %(c)s
               UNION
               SELECT c.code FROM categories c JOIN below b ON c.parent_code = b.code
               WHERE c.marketplace = %(m)s
           )
           UPDATE categories SET gone = true
           WHERE marketplace = %(m)s AND code IN (SELECT code FROM below) AND NOT gone""",
        {"m": marketplace, "c": code},
    )


def _upsert_child(
    conn: psycopg.Connection,
    marketplace: str,
    parent: str,
    depth: int,
    child: Category,
    now: datetime,
) -> None:
    # A leaf needs no request of its own: the parent's answer already showed it bare.
    conn.execute(
        """INSERT INTO categories (marketplace, code, parent_code, title, depth,
                                   products_count, popularity, is_leaf, checked_at)
           VALUES (%(m)s, %(code)s, %(parent)s, %(title)s, %(depth)s, %(count)s,
                   %(pop)s, %(leaf)s, CASE WHEN %(leaf)s THEN %(now)s::timestamptz END)
           ON CONFLICT (marketplace, code) DO UPDATE SET
               parent_code = EXCLUDED.parent_code, title = EXCLUDED.title,
               depth = EXCLUDED.depth, products_count = EXCLUDED.products_count,
               gone = false,
               -- The root's answer understates popularity; a node's own check sets it.
               popularity = CASE WHEN %(depth)s = 1 AND categories.checked_at IS NOT NULL
                                 THEN categories.popularity ELSE EXCLUDED.popularity END,
               is_leaf = COALESCE(EXCLUDED.is_leaf, categories.is_leaf),
               checked_at = CASE WHEN EXCLUDED.is_leaf THEN EXCLUDED.checked_at
                                 -- A former leaf with children, or a node back from
                                 -- gone, needs its own check to list its children.
                                 WHEN categories.gone
                                      OR (categories.is_leaf AND EXCLUDED.is_leaf = false)
                                 THEN NULL
                                 ELSE categories.checked_at END""",
        {
            "m": marketplace,
            "code": child.code,
            "parent": parent,
            "title": child.title,
            "depth": depth,
            "count": child.products_count,
            "pop": child.popularity,
            "leaf": child.is_leaf,
            "now": now,
        },
    )
    if child.is_leaf:
        # A node that had children and now has none: they are gone with it.
        _bury_below(conn, marketplace, child.code)


class Sweep(NamedTuple):
    """One listing to read: a whole leaf, or a part of it (`segment` set)."""

    code: str
    products_count: int
    segment: int | None = None
    filters: str = ""


# A listing never swept counts as last swept this long ago.
NEVER_SWEPT_AGE = timedelta(days=30)
# So a listing with no popularity figure still comes up now and then.
POPULARITY_FLOOR = 100


def next_sweep(
    conn: psycopg.Connection, marketplace: str, now: datetime, max_pages: int
) -> Sweep | None:
    """The listing that most needs a sweep: popularity times time since the last sweep,
    per page a sweep costs. Popular ones come back often, and every one's turn comes
    eventually. Leaves and the unsplit parts of big leaves share the queue."""
    row = conn.execute(
        """WITH queue AS (
               SELECT code, products_count, NULL::bigint AS segment, '' AS filters,
                      popularity + %(floor)s AS weight,
                      coalesce(swept_at, %(now)s - %(never)s) AS due_since
               FROM categories
               WHERE marketplace = %(m)s AND is_leaf AND NOT gone AND products_count > 0
               UNION ALL
               -- Parts get no floor (there can be tens of thousands) and wait from
               -- when they were made. Only parts of leaves still too big to list whole.
               SELECT s.category_code, s.products_count, s.id, s.filters, s.popularity,
                      coalesce(s.swept_at, s.created_at, %(now)s)
               FROM category_segments s
               JOIN categories c ON c.marketplace = s.marketplace AND c.code = s.category_code
               WHERE s.marketplace = %(m)s AND NOT s.gone AND s.is_split IS NOT TRUE
                 AND NOT c.gone AND c.is_leaf AND c.products_count > %(cards)s
                 AND s.products_count > 0
           )
           SELECT code, products_count, segment, filters FROM queue
           ORDER BY weight::float8 * extract(epoch FROM %(now)s - due_since)
                    / least(greatest(ceil(products_count / 12.0), 1), %(pages)s) DESC,
                    code, segment NULLS FIRST
           LIMIT 1""",
        {
            "m": marketplace,
            "floor": POPULARITY_FLOOR,
            "now": now,
            "never": NEVER_SWEPT_AGE,
            "pages": max_pages,
            "cards": max_pages * 12,
        },
    ).fetchone()
    return Sweep(*row) if row else None


def mark_swept(
    conn: psycopg.Connection,
    marketplace: str,
    code: str,
    products: int,
    now: datetime,
    *,
    count_at_least: int = 0,
) -> None:
    """`count_at_least` raises the leaf's size when the sweep found it bigger than its
    parent said, so the next sweep reads further before the tree is refreshed."""
    conn.execute(
        """UPDATE categories SET swept_at = %s, swept_products = %s,
               products_count = greatest(products_count, %s)
           WHERE marketplace = %s AND code = %s""",
        (now, products, count_at_least, marketplace, code),
    )


def mark_segment_swept(
    conn: psycopg.Connection, segment: int, products: int, now: datetime, *, count_at_least: int = 0
) -> None:
    conn.execute(
        """UPDATE category_segments SET swept_at = %s, swept_products = %s,
               products_count = greatest(products_count, %s)
           WHERE id = %s""",
        (now, products, count_at_least, segment),
    )


def mark_segment_gone(conn: psycopg.Connection, segment: int) -> None:
    conn.execute("UPDATE category_segments SET gone = true WHERE id = %s", (segment,))
    _bury_segments_below(conn, segment)


class SplitTarget(NamedTuple):
    """A listing too big to read whole: a leaf (segment None) or one of its parts."""

    code: str
    segment: int | None
    filters: str
    facet_ids: list[str]
    products_count: int
    popularity: int


def splits_due(
    conn: psycopg.Connection,
    marketplace: str,
    *,
    min_count: int,
    max_terms: int,
    limit: int,
    checked_before: datetime,
    segment_weight: float,
) -> list[SplitTarget]:
    """Leaves and parts bigger than one listing serves, never split or not lately,
    most popular first. A leaf's popularity is weighted down for its parts: its own
    sweep already covers its most popular products."""
    rows = conn.execute(
        """SELECT code, NULL::bigint, '', ARRAY[]::text[], products_count,
                  (popularity * %(w)s)::bigint AS popularity
           FROM categories
           WHERE marketplace = %(m)s AND is_leaf AND NOT gone AND products_count > %(min)s
             AND (split_checked_at IS NULL OR split_checked_at < %(before)s)
           UNION ALL
           SELECT s.category_code, s.id, s.filters, s.facet_ids, s.products_count, s.popularity
           FROM category_segments s
           JOIN categories c ON c.marketplace = s.marketplace AND c.code = s.category_code
           WHERE s.marketplace = %(m)s AND NOT s.gone AND NOT c.gone AND c.is_leaf
             AND c.products_count > %(min)s AND s.products_count > %(min)s AND cardinality(s.facet_ids) < %(terms)s
             AND (s.checked_at IS NULL OR s.checked_at < %(before)s)
           ORDER BY popularity DESC, products_count DESC
           LIMIT %(limit)s""",
        {
            "m": marketplace,
            "w": segment_weight,
            "min": min_count,
            "terms": max_terms,
            "before": checked_before,
            "limit": limit,
        },
    ).fetchall()
    return [SplitTarget(r[0], r[1], r[2], list(r[3]), r[4], r[5]) for r in rows]


def record_split(
    conn: psycopg.Connection,
    marketplace: str,
    target: SplitTarget,
    facet_id: str | None,
    values: list[FacetValue],
    total: int,
    now: datetime,
) -> None:
    """Store how a listing splits: one part per value of `facet_id`. With no facet
    the listing cannot be split usefully, and any parts it had are dropped."""
    if target.segment is None:
        conn.execute(
            """UPDATE categories SET split_checked_at = %s
               WHERE marketplace = %s AND code = %s""",
            (now, marketplace, target.code),
        )
    else:
        conn.execute(
            """UPDATE category_segments SET is_split = %s, checked_at = %s,
                   products_count = CASE WHEN %s > 0 THEN %s ELSE products_count END
               WHERE id = %s""",
            (facet_id is not None, now, total, total, target.segment),
        )
    kept = []
    # Values of a filter like colour overlap and add up past the total: share by
    # their sum, so the parts together hold the target's popularity, no more.
    shares = max(sum(v.count for v in values), total, 1)
    for value in values if facet_id else []:
        row = conn.execute(
            """INSERT INTO category_segments (marketplace, category_code, filters, facet_ids,
                                              parent_id, title, products_count, popularity,
                                              created_at)
               VALUES (%(m)s, %(code)s, %(filters)s, %(facets)s, %(parent)s, %(title)s,
                       %(count)s, %(pop)s, %(now)s)
               ON CONFLICT (marketplace, category_code, filters) DO UPDATE SET
                   parent_id = EXCLUDED.parent_id, title = EXCLUDED.title,
                   products_count = EXCLUDED.products_count,
                   popularity = EXCLUDED.popularity,
                   -- Back from gone, its own parts are gone: check it afresh.
                   is_split = CASE WHEN category_segments.gone THEN NULL
                                   ELSE category_segments.is_split END,
                   checked_at = CASE WHEN category_segments.gone THEN NULL
                                     ELSE category_segments.checked_at END,
                   gone = false
               RETURNING id""",
            {
                "m": marketplace,
                "code": target.code,
                "filters": target.filters + value.term,
                "facets": [*target.facet_ids, facet_id],
                "parent": target.segment,
                "title": value.title,
                "count": value.count,
                "pop": round(target.popularity * value.count / shares),
                "now": now,
            },
        ).fetchone()
        kept.append(row[0])
    lost = conn.execute(
        """UPDATE category_segments SET gone = true
           WHERE marketplace = %s AND category_code = %s AND NOT gone
             AND parent_id IS NOT DISTINCT FROM %s AND NOT (id = ANY(%s::bigint[]))
           RETURNING id""",
        (marketplace, target.code, target.segment, kept),
    ).fetchall()
    for (segment,) in lost:
        _bury_segments_below(conn, segment)


def postpone_split(
    conn: psycopg.Connection,
    marketplace: str,
    target: SplitTarget,
    retry_in: timedelta,
    every: timedelta,
    now: datetime,
) -> None:
    """After a failed check, try this target again in `retry_in`, not first thing
    next round, where it would hold every other split up behind it."""
    checked = now - every + retry_in
    if target.segment is None:
        conn.execute(
            """UPDATE categories SET split_checked_at = %s
               WHERE marketplace = %s AND code = %s""",
            (checked, marketplace, target.code),
        )
    else:
        conn.execute(
            "UPDATE category_segments SET checked_at = %s WHERE id = %s",
            (checked, target.segment),
        )


def _bury_segments_below(conn: psycopg.Connection, segment: int) -> None:
    conn.execute(
        """WITH RECURSIVE below AS (
               SELECT id FROM category_segments WHERE parent_id = %(s)s
               UNION
               SELECT c.id FROM category_segments c JOIN below b ON c.parent_id = b.id
           )
           UPDATE category_segments SET gone = true
           WHERE id IN (SELECT id FROM below) AND NOT gone""",
        {"s": segment},
    )


def last_failed_round(
    conn: psycopg.Connection, marketplace: str, category: str, region: str
) -> datetime | None:
    """When the latest finished round ended, if it was blocked or failed."""
    row = conn.execute(
        """SELECT status, finished_at FROM crawl_runs
           WHERE marketplace = %s AND category = %s AND region = %s
             AND status NOT IN ('running', 'interrupted')
           ORDER BY id DESC LIMIT 1""",
        (marketplace, category, region),
    ).fetchone()
    if row and row[0] in ("blocked", "failed"):
        return row[1]
    return None


def product_history(
    conn: psycopg.Connection, marketplace: str, external_id: str, region: str
) -> tuple[ProductInfo, list[PricePeriod]] | None:
    row = conn.execute(
        """SELECT id, title, url, last_seen_at FROM products
           WHERE marketplace = %s AND external_id = %s""",
        (marketplace, external_id),
    ).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        """SELECT s.name, s.url, i.price, i.currency, i.first_seen_at, i.last_seen_at,
                  i.is_current
           FROM price_intervals i JOIN sellers s ON s.id = i.seller_id
           WHERE i.product_id = %s AND i.region = %s
           ORDER BY i.first_seen_at, i.price""",
        (row[0], region),
    ).fetchall()
    return ProductInfo(row[1], row[2], row[3]), [PricePeriod(*r) for r in rows]


def lowest_prices(history: list[PricePeriod]) -> list[tuple[datetime, Decimal]]:
    """The lowest price among sellers at each moment we looked, dropping repeats."""
    moments = sorted({p.first_seen_at for p in history} | {p.last_seen_at for p in history})
    series: list[tuple[datetime, Decimal]] = []
    for moment in moments:
        lowest = min(
            p.price for p in history if p.first_seen_at <= moment <= p.last_seen_at
        )
        if not series or series[-1][1] != lowest:
            series.append((moment, lowest))
    return series


def _close(conn: psycopg.Connection, interval_id: int) -> None:
    conn.execute("UPDATE price_intervals SET is_current = false WHERE id = %s", (interval_id,))
