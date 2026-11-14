"""The whole-catalog crawl: two layers sharing one request budget.

One round, a few hours long, does in order:

1. Tree: checks catalog nodes that may have children, popular ones first, so the
   list of leaf categories grows and stays current.
2. Sellers: every seller's price for the tracked products, the most popular ones and
   those someone looked up, when their last check is older than OFFERS_EVERY.
3. Listings: sweeps leaf categories until the round's time is up. A sweep reads a
   leaf's listing pages and keeps each card's lowest price: 12 products a request,
   against about one request a product for the sellers' prices. The next leaf is the
   one with the most popularity times time since its last sweep, per page.
4. Seller pages that are due, as in the single-category crawl.

All state lives in the database, so a restart only loses the leaf in progress.
"""

import logging
import math
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from shynbaga import collector, db
from shynbaga.collector import RunResult
from shynbaga.marketplaces import Blocked, Marketplace, MarketplaceError, NotFound

logger = logging.getLogger(__name__)

CATALOG = "catalog"
# Sellers' prices for tracked products are fetched again after this long.
OFFERS_EVERY = timedelta(hours=12)
# A node with children is checked again after this long; leaves come with their parent.
TREE_EVERY = timedelta(days=30)
# After a blocked or failed round, the next one waits this long, restarts included.
PAUSE_AFTER_FAILURE = timedelta(hours=6)
# Tree checks per round, so a fresh tree does not hold the listings back for long.
TREE_CHECKS_PER_ROUND = 300
# A leaf's listing failing this many times in a row means the connection is gone.
MAX_CONSECUTIVE_LEAF_FAILURES = 3
CARDS_PER_PAGE = 12


@dataclass
class Settings:
    region: str
    root: str
    # The round stops starting new sweeps after this long.
    round_length: timedelta
    # How many of the most popular products get sellers' prices.
    tracked: int
    tree_checks: int = TREE_CHECKS_PER_ROUND
    # For tests: stop after this many sweeps.
    max_sweeps: int | None = None


@dataclass
class CatalogResult(RunResult):
    categories_checked: int = 0
    leaves_swept: int = 0
    cards_seen: int = 0


def crawl(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    settings: Settings,
    *,
    clock: Callable[[], datetime] = collector._utcnow,
) -> CatalogResult:
    key = markets[0].key
    started = clock()
    run_id = db.start_run(conn, key, CATALOG, settings.region, started)
    result = CatalogResult(status="running")
    try:
        db.ensure_root(conn, key, settings.root)
        _check_tree(conn, markets, settings, clock, result)
        _fetch_tracked_offers(conn, markets, settings, clock, result, run_id)
        _sweep(conn, markets, settings, started + settings.round_length, clock, result)
        collector._refresh_seller_profiles(conn, markets, clock)
        clean = result.products_failed == 0 and result.error is None
        result.status = "completed" if clean else "partial"
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
            bytes_received=sum(m.bytes_received for m in markets),
            error=result.error,
            cards_seen=result.cards_seen,
            categories_checked=result.categories_checked,
            leaves_swept=result.leaves_swept,
        )
    return result


def _check_tree(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    settings: Settings,
    clock: Callable[[], datetime],
    result: CatalogResult,
) -> None:
    key = markets[0].key
    failures = 0
    # One batch at a time: a node's children are only known after its check, and
    # the popular ones among them should go before the rest of the old queue.
    while result.categories_checked < settings.tree_checks:
        batch = db.categories_to_check(
            conn,
            key,
            min(len(markets), settings.tree_checks - result.categories_checked),
            clock() - TREE_EVERY,
        )
        if not batch:
            return

        def check(market: Marketplace, code: str):
            return market.fetch_category(code, settings.region)

        with closing(collector._in_parallel(markets, batch, check)) as outcomes:
            for code, outcome in outcomes:
                if isinstance(outcome, Blocked):
                    raise outcome
                if isinstance(outcome, NotFound):
                    db.mark_gone(conn, key, code)
                    conn.commit()
                    continue
                if isinstance(outcome, MarketplaceError):
                    failures += 1
                    # Without this a tree that never grows would look like a quiet success.
                    result.error = result.error or f"category {code}: {outcome}"
                    logger.warning("category %s failed: %s", code, outcome)
                    if failures >= collector.MAX_CONSECUTIVE_FAILURES:
                        raise outcome
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
                failures = 0
                db.record_category(conn, key, outcome, clock())
                conn.commit()
                result.categories_checked += 1
        if failures:
            # Failed nodes stay due, and asking again at once would only fail again.
            return


def _fetch_tracked_offers(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    settings: Settings,
    clock: Callable[[], datetime],
    result: CatalogResult,
    run_id: int,
) -> None:
    now = clock()
    # Rounds start about a round's length apart, so a product checked just under
    # OFFERS_EVERY ago would otherwise wait a whole extra round.
    wait = max(OFFERS_EVERY - settings.round_length, timedelta(0))
    due = db.products_due_offers(
        conn, markets[0].key, settings.tracked, now - wait, now - OFFERS_EVERY
    )
    if not due:
        return
    products = [product for product, _ in due]
    positions = {p.external_id: pos for p, pos in due if pos is not None}
    collector.fetch_offers(
        conn, markets, products, positions, settings.region, clock, result, run_id
    )


def _sweep(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    settings: Settings,
    until: datetime,
    clock: Callable[[], datetime],
    result: CatalogResult,
) -> None:
    key = markets[0].key
    cap = markets[0].max_listing_pages
    failures = 0
    swept: set[str] = set()
    while clock() < until:
        if settings.max_sweeps is not None and result.leaves_swept >= settings.max_sweeps:
            return
        leaf = db.next_leaf(conn, key, clock(), cap)
        # Back to a leaf from this round: every leaf has had its turn.
        if leaf is None or leaf.code in swept:
            return
        swept.add(leaf.code)
        pages = min(max(math.ceil(leaf.products_count / CARDS_PER_PAGE), 1), cap)
        try:
            cards, error = collector.discover(
                markets, leaf.code, settings.region, pages * CARDS_PER_PAGE, pages
            )
        except Blocked:
            raise
        except NotFound:
            db.mark_gone(conn, key, leaf.code)
            conn.commit()
            continue
        except MarketplaceError as failure:
            cards, error = [], str(failure)
        if error:
            result.error = result.error or f"{leaf.code}: {error}"
        if error and not cards:
            failures += 1
            if failures >= MAX_CONSECUTIVE_LEAF_FAILURES:
                raise MarketplaceError(f"listings keep failing, last {leaf.code}: {error}")
            # Swept with nothing, so the queue moves on instead of retrying at once.
            db.mark_swept(conn, key, leaf.code, 0, clock())
            conn.commit()
            continue
        failures = 0
        now = clock()
        for position, card in enumerate(cards, start=1):
            product_id = db.upsert_product(conn, key, card, now)
            db.record_card(
                conn, product_id, settings.region, leaf.code, position, card.metrics, now
            )
        # Every page full: the leaf may have grown since its parent's count.
        grown = len(cards) + CARDS_PER_PAGE if len(cards) >= pages * CARDS_PER_PAGE else 0
        db.mark_swept(conn, key, leaf.code, len(cards), now, count_at_least=grown)
        conn.commit()
        result.leaves_swept += 1
        result.cards_seen += len(cards)
        logger.info("swept %s: %d cards", leaf.code, len(cards))
