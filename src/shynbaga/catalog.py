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
from shynbaga.marketplaces import (
    BadRequest,
    Blocked,
    Facets,
    Marketplace,
    MarketplaceError,
    NotFound,
)

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
# Checks of big listings' filters per round, to split them into parts.
SPLIT_CHECKS_PER_ROUND = 100
# A part is a leaf's listing with up to this many filter terms.
MAX_FILTER_TERMS = 3
# A leaf's parts share this much of its popularity: the leaf's own sweep already
# covers its most popular products, so the parts hold its quieter tail.
SEGMENT_WEIGHT = 0.25
# Each other order of a listing too big to read whole gets this share of its
# popularity: it reads further down the same listing.
SORT_WEIGHT = 0.25
# Filters to prefer when several open up about as many products.
PREFERRED_FACETS = ("manufacturerName", "price")
# A failed filters check is tried again after this long.
SPLIT_RETRY = timedelta(hours=6)
# A split must open up at least this many times what one listing serves.
MIN_SPLIT_GAIN = 1.5
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
    split_checks: int = SPLIT_CHECKS_PER_ROUND
    # For tests: stop after this many sweeps.
    max_sweeps: int | None = None


@dataclass
class CatalogResult(RunResult):
    categories_checked: int = 0
    splits_checked: int = 0
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
        _split_big_listings(conn, markets, settings, clock, result)
        db.ensure_sort_variants(
            conn,
            key,
            min_count=markets[0].max_listing_pages * CARDS_PER_PAGE,
            max_terms=MAX_FILTER_TERMS,
            segment_weight=SEGMENT_WEIGHT,
            sort_weight=SORT_WEIGHT,
            now=clock(),
        )
        conn.commit()
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
            categories_checked=result.categories_checked + result.splits_checked,
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


def choose_split(facets: Facets, used: list[str], listing_cap: int) -> tuple[str, list] | None:
    """The filter whose values open up the most products, counting at most what one
    listing serves per value; None when no filter beats one listing by enough.

    Brand and price are preferred when they do about as well; but a brand like "no
    brand" can hold a million cases, and nearly all cases cost under 10 000 ₸, so
    other filters (compatible model, colour, seller) often split better.
    """
    best = None
    for facet in facets.facets:
        if facet.id in used:
            continue  # its counts ignore its own selection: no use splitting by it again
        reach = sum(min(v.count, listing_cap) for v in facet.values)
        if facets.total:
            reach = min(reach, facets.total)  # overlapping values, like colours
        preferred = facet.id in PREFERRED_FACETS
        # Within 10% of the best, a preferred filter wins.
        rank = reach * (1.1 if preferred else 1.0)
        if best is None or rank > best[0]:
            best = (rank, reach, facet)
    if best is None or best[1] < listing_cap * MIN_SPLIT_GAIN:
        return None
    return best[2].id, best[2].values


def _split_big_listings(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    settings: Settings,
    clock: Callable[[], datetime],
    result: CatalogResult,
) -> None:
    key = markets[0].key
    listing_cap = markets[0].max_listing_pages * CARDS_PER_PAGE
    while result.splits_checked < settings.split_checks:
        batch = db.splits_due(
            conn,
            key,
            min_count=listing_cap,
            max_terms=MAX_FILTER_TERMS,
            limit=min(len(markets), settings.split_checks - result.splits_checked),
            checked_before=clock() - TREE_EVERY,
            segment_weight=SEGMENT_WEIGHT,
        )
        if not batch:
            return

        def facets(market: Marketplace, target: db.SplitTarget) -> Facets:
            return market.fetch_facets(target.code, target.filters, settings.region)

        failures = 0
        with closing(collector._in_parallel(markets, batch, facets)) as outcomes:
            for target, outcome in outcomes:
                if isinstance(outcome, Blocked):
                    raise outcome
                if isinstance(outcome, NotFound):
                    if target.segment is None:
                        db.mark_gone(conn, key, target.code)
                    else:
                        db.mark_segment_gone(conn, target.segment)
                    conn.commit()
                    continue
                if isinstance(outcome, MarketplaceError):
                    failures += 1
                    where = target.code + target.filters
                    result.error = result.error or f"filters {where}: {outcome}"
                    logger.warning("filters %s failed: %s", where, outcome)
                    db.postpone_split(conn, key, target, SPLIT_RETRY, TREE_EVERY, clock())
                    conn.commit()
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
                choice = choose_split(outcome, target.facet_ids, listing_cap)
                facet_id, values = choice if choice else (None, [])
                db.record_split(conn, key, target, facet_id, values, outcome.total, clock())
                conn.commit()
                result.splits_checked += 1
        if failures >= len(batch):
            return  # the whole batch failed: likely the connection, not the targets


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
    swept: set[tuple[str, int | None]] = set()
    while clock() < until:
        if settings.max_sweeps is not None and result.leaves_swept >= settings.max_sweeps:
            return
        item = db.next_sweep(conn, key, clock(), cap)
        # Back to a listing from this round: every one has had its turn.
        if item is None or (item.code, item.segment) in swept:
            return
        swept.add((item.code, item.segment))
        where = item.code + item.filters + ("" if item.sort == "relevance" else f" ({item.sort})")
        pages = min(max(math.ceil(item.products_count / CARDS_PER_PAGE), 1), cap)
        try:
            cards, error = collector.discover(
                markets,
                item.code,
                settings.region,
                pages * CARDS_PER_PAGE,
                pages,
                item.filters,
                item.sort,
            )
        except Blocked:
            raise
        except NotFound:
            if item.segment is None:
                db.mark_gone(conn, key, item.code)
            else:
                db.mark_segment_gone(conn, item.segment)
            conn.commit()
            continue
        except BadRequest as failure:
            if item.segment is None:
                cards, error = [], str(failure)
            else:
                # Kaspi does not take this part's filters: drop the part. It says
                # nothing about the connection, so it is no failure of the round.
                logger.warning("part %s refused: %s", where, failure)
                db.mark_segment_gone(conn, item.segment)
                conn.commit()
                continue
        except MarketplaceError as failure:
            cards, error = [], str(failure)
        if error:
            result.error = result.error or f"{where}: {error}"
        if error and not cards:
            failures += 1
            if failures >= MAX_CONSECUTIVE_LEAF_FAILURES:
                raise MarketplaceError(f"listings keep failing, last {where}: {error}")
            # Swept with nothing, so the queue moves on instead of retrying at once.
            _mark_swept(conn, key, item, 0, clock(), 0)
            conn.commit()
            continue
        failures = 0
        now = clock()
        for position, card in enumerate(cards, start=1):
            product_id = db.upsert_product(conn, key, card, now)
            # Only the whole leaf's listing gives a place in the leaf.
            place = position if item.segment is None else None
            db.record_card(conn, product_id, settings.region, item.code, place, card.metrics, now)
        # Every page full: the listing may have grown since it was counted.
        grown = len(cards) + CARDS_PER_PAGE if len(cards) >= pages * CARDS_PER_PAGE else 0
        _mark_swept(conn, key, item, len(cards), now, grown)
        conn.commit()
        result.leaves_swept += 1
        result.cards_seen += len(cards)
        logger.info("swept %s: %d cards", where, len(cards))


def _mark_swept(
    conn: psycopg.Connection, key: str, item: db.Sweep, cards: int, now: datetime, grown: int
) -> None:
    if item.segment is None:
        db.mark_swept(conn, key, item.code, cards, now, count_at_least=grown)
    else:
        db.mark_segment_swept(conn, item.segment, cards, now, count_at_least=grown)
