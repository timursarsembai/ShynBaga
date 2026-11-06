from datetime import datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import NamedTuple

import psycopg

from shinbaga.marketplaces import Offer, OfferList, Product

# Past this without an observation, we no longer know the price held in between.
MAX_OBSERVATION_GAP = timedelta(hours=48)


class CurrentPrice(NamedTuple):
    id: int
    price: Decimal
    currency: str
    last_seen_at: datetime


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(files("shinbaga").joinpath("schema.sql").read_text(encoding="utf-8"))
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
                                 url, image_url, first_seen_at, last_seen_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (marketplace, external_id) DO UPDATE SET
               title = EXCLUDED.title,
               brand = EXCLUDED.brand,
               category_path = EXCLUDED.category_path,
               url = EXCLUDED.url,
               image_url = EXCLUDED.image_url,
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
            now,
            now,
        ),
    ).fetchone()
    return row[0]


def _upsert_seller(
    conn: psycopg.Connection, marketplace: str, offer: Offer, now: datetime
) -> int:
    row = conn.execute(
        """INSERT INTO sellers (marketplace, external_id, name, first_seen_at, last_seen_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (marketplace, external_id) DO UPDATE SET
               name = EXCLUDED.name, last_seen_at = EXCLUDED.last_seen_at
           RETURNING id""",
        (marketplace, offer.seller_id, offer.seller_name, now, now),
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


def _close(conn: psycopg.Connection, interval_id: int) -> None:
    conn.execute("UPDATE price_intervals SET is_current = false WHERE id = %s", (interval_id,))
