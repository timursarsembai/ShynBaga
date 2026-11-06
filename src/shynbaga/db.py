from datetime import datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import NamedTuple

import psycopg

from shynbaga.marketplaces import Offer, OfferList, Product

# Past this without an observation, we no longer know the price held in between.
MAX_OBSERVATION_GAP = timedelta(hours=48)


class CurrentPrice(NamedTuple):
    id: int
    price: Decimal
    currency: str
    last_seen_at: datetime


class ProductInfo(NamedTuple):
    title: str
    url: str | None


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


def last_run_finished_at(
    conn: psycopg.Connection, marketplace: str, category: str, region: str
) -> datetime | None:
    # An interrupted run says nothing about the marketplace, so it does not count.
    row = conn.execute(
        """SELECT max(finished_at) FROM crawl_runs
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
) -> None:
    conn.execute(
        """UPDATE crawl_runs
           SET finished_at = %s, status = %s, products_ok = %s, products_failed = %s,
               bytes_received = %s, error = %s
           WHERE id = %s""",
        (now, status, products_ok, products_failed, bytes_received, error, run_id),
    )
    conn.commit()


def upsert_product(
    conn: psycopg.Connection, marketplace: str, product: Product, now: datetime
) -> int:
    row = conn.execute(
        """INSERT INTO products (marketplace, external_id, title, brand, category_path,
                                 url, image_url, offers_ref, first_seen_at, last_seen_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (marketplace, external_id) DO UPDATE SET
               title = EXCLUDED.title,
               brand = EXCLUDED.brand,
               category_path = EXCLUDED.category_path,
               url = EXCLUDED.url,
               image_url = EXCLUDED.image_url,
               offers_ref = EXCLUDED.offers_ref,
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
    return row[0]


def record_offers(
    conn: psycopg.Connection,
    marketplace: str,
    product_id: int,
    region: str,
    offers: OfferList,
    now: datetime,
) -> None:
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


def is_known_product(conn: psycopg.Connection, marketplace: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM products WHERE marketplace = %s AND external_id = %s",
        (marketplace, external_id),
    ).fetchone()
    return row is not None


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
        """SELECT external_id, title, COALESCE(offers_ref, external_id), url, image_url,
                  brand, category_path
           FROM products WHERE marketplace = %s AND requested_at IS NOT NULL""",
        (marketplace,),
    ).fetchall()
    return [Product(*row[:6], category_path=tuple(row[6])) for row in rows]


def product_history(
    conn: psycopg.Connection, marketplace: str, external_id: str, region: str
) -> tuple[ProductInfo, list[PricePeriod]] | None:
    row = conn.execute(
        "SELECT id, title, url FROM products WHERE marketplace = %s AND external_id = %s",
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
    return ProductInfo(row[1], row[2]), [PricePeriod(*r) for r in rows]


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
