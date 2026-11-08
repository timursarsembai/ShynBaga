import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from shynbaga import db
from shynbaga.marketplaces import Blocked, Marketplace, MarketplaceError, Product

logger = logging.getLogger(__name__)

# One refusal can be a hiccup; several in a row mean the IP is blocked.
MAX_CONSECUTIVE_BLOCKS = 3
# Each failure already spent its retries, so a streak means the connection is gone.
MAX_CONSECUTIVE_FAILURES = 5
# Single failed products are normal; a larger share means something is wrong.
ALERT_FAILED_SHARE = 0.1


@dataclass
class RunResult:
    status: str
    products_ok: int = 0
    products_failed: int = 0
    error: str | None = None


def alert_text(marketplace: str, category: str, result: RunResult) -> str | None:
    """What to tell the owner about a finished run; None when it went fine."""
    where = f"{marketplace}, {category}"
    saved = f"Успели сохранить: {result.products_ok}."
    if result.status == "blocked":
        return f"ShynBaga: сбор заблокирован ({where}). {result.error}. {saved}"
    if result.status == "failed":
        return f"ShynBaga: сбор упал ({where}). {result.error}. {saved}"
    attempted = result.products_ok + result.products_failed
    if result.error or result.products_failed > attempted * ALERT_FAILED_SHARE:
        return (
            f"ShynBaga: обход прошёл не полностью ({where}). "
            f"Ошибок: {result.products_failed} из {attempted}. {result.error or ''}".rstrip()
        )
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def discover(
    market: Marketplace, category: str, region: str, max_products: int
) -> tuple[list[Product], str | None]:
    """Products found, plus why the listing stopped early, if it did."""
    products: dict[str, Product] = {}
    page = 0
    error = None
    while len(products) < max_products:
        try:
            cards = market.list_products(category, region, page)
        except Blocked:
            raise
        except MarketplaceError as failure:
            if not products:
                raise
            # A broken page must not cost us the products already found.
            error = str(failure)
            logger.warning("listing stopped at page %s: %s", page, failure)
            break
        if not cards:
            break
        known = len(products)
        for card in cards:
            products.setdefault(card.external_id, card)
        if len(products) == known:
            # A server that ignores the page number would otherwise loop forever.
            break
        page += 1
    return list(products.values())[:max_products], error


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


def track(
    conn: psycopg.Connection,
    market: Marketplace,
    external_id: str,
    region: str,
    *,
    clock: Callable[[], datetime] = _utcnow,
) -> None:
    """Make sure a product someone asked about is tracked, fetching it if it is new."""
    if not db.is_known_product(conn, market.key, external_id):
        product = market.fetch_product(external_id, region)
        offers = market.fetch_offers(product, region)
        now = clock()
        product_id = db.upsert_product(conn, market.key, product, now)
        db.record_offers(conn, market.key, product_id, region, offers, now)
    db.mark_requested(conn, market.key, external_id, clock())
    conn.commit()


def _collect_products(
    conn: psycopg.Connection,
    market: Marketplace,
    category: str,
    region: str,
    max_products: int,
    clock: Callable[[], datetime],
    result: RunResult,
) -> None:
    products, result.error = discover(market, category, region, max_products)
    if not products:
        raise MarketplaceError(f"category {category} returned no products")
    found = {p.external_id: p for p in db.requested_products(conn, market.key)}
    found.update({p.external_id: p for p in products})
    products = list(found.values())

    consecutive_blocks = 0
    consecutive_failures = 0
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
            consecutive_failures += 1
            result.products_failed += 1
            logger.warning("product %s failed: %s", product.external_id, error)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise
            continue
        consecutive_blocks = 0
        consecutive_failures = 0
        now = clock()
        product_id = db.upsert_product(conn, market.key, product, now)
        db.record_offers(conn, market.key, product_id, region, offers, now)
        conn.commit()
        result.products_ok += 1

    clean = result.products_failed == 0 and result.error is None
    result.status = "completed" if clean else "partial"
