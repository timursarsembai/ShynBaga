import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from shinbaga import db
from shinbaga.marketplaces import Blocked, Marketplace, MarketplaceError, Product

logger = logging.getLogger(__name__)

# One refusal can be a hiccup; several in a row mean the IP is blocked.
MAX_CONSECUTIVE_BLOCKS = 3


@dataclass
class RunResult:
    status: str
    products_ok: int = 0
    products_failed: int = 0
    error: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def discover(
    market: Marketplace, category: str, region: str, max_products: int
) -> list[Product]:
    products: dict[str, Product] = {}
    page = 0
    while len(products) < max_products:
        cards = market.list_products(category, region, page)
        if not cards:
            break
        known = len(products)
        for card in cards:
            products.setdefault(card.external_id, card)
        if len(products) == known:
            # A server that ignores the page number would otherwise loop forever.
            break
        page += 1
    return list(products.values())[:max_products]


def collect(
    conn: psycopg.Connection,
    market: Marketplace,
    category: str,
    region: str,
    max_products: int,
    *,
    clock: Callable[[], datetime] = _utcnow,
) -> RunResult:
    run_id = db.start_run(conn, market.key, category, region, clock())
    result = RunResult(status="running")
    try:
        _collect_products(conn, market, category, region, max_products, clock, result)
    except MarketplaceError as error:
        conn.rollback()
        result.status = "blocked" if isinstance(error, Blocked) else "failed"
        result.error = str(error)
    except BaseException as error:
        conn.rollback()
        result.status = "failed"
        result.error = type(error).__name__
        raise
    finally:
        db.finish_run(
            conn,
            run_id,
            clock(),
            status=result.status,
            products_ok=result.products_ok,
            products_failed=result.products_failed,
            bytes_received=market.bytes_received,
            error=result.error,
        )
    return result


def _collect_products(
    conn: psycopg.Connection,
    market: Marketplace,
    category: str,
    region: str,
    max_products: int,
    clock: Callable[[], datetime],
    result: RunResult,
) -> None:
    products = discover(market, category, region, max_products)
    if not products:
        raise MarketplaceError(f"category {category} returned no products")

    consecutive_blocks = 0
    for product in products:
        try:
            offers = market.fetch_offers(product, region)
        except Blocked:
            result.products_failed += 1
            consecutive_blocks += 1
            if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                raise
            continue
        except MarketplaceError as error:
            consecutive_blocks = 0
            result.products_failed += 1
            logger.warning("product %s failed: %s", product.external_id, error)
            continue
        consecutive_blocks = 0
        now = clock()
        product_id = db.upsert_product(conn, market.key, product, now)
        db.record_offers(conn, market.key, product_id, region, offers, now)
        conn.commit()
        result.products_ok += 1

    result.status = "completed" if result.products_failed == 0 else "partial"
