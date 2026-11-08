import logging
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from shynbaga import db
from shynbaga.marketplaces import Blocked, Marketplace, MarketplaceError, OfferList, Product

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
    markets: list[Marketplace], category: str, region: str, max_products: int
) -> tuple[list[Product], str | None]:
    """Products found, plus why the listing stopped early, if it did.

    Worker k reads pages k, k + n, k + 2n, ... so the listing is split between them.
    """
    lock = threading.Lock()
    pages: dict[int, list[Product]] = {}
    seen: set[str] = set()
    failures: list[tuple[int, MarketplaceError]] = []
    crashes: list[BaseException] = []

    def walk(market: Marketplace, page: int) -> None:
        own: set[str] = set()
        while True:
            with lock:
                if (
                    crashes
                    or len(seen) >= max_products
                    or any(isinstance(f, Blocked) for _, f in failures)
                ):
                    return
            try:
                cards = market.list_products(category, region, page)
            except MarketplaceError as failure:
                with lock:
                    failures.append((page, failure))
                return
            except BaseException as crash:  # a bug or an interrupt: re-raised below
                with lock:
                    crashes.append(crash)
                return
            ids = {card.external_id for card in cards}
            # An empty page ends the category; a page of only known products means
            # the server ignores the page number and would otherwise loop forever.
            if not ids or ids <= own:
                return
            own |= ids
            with lock:
                pages[page] = cards
                seen.update(ids)
            page += len(markets)

    workers = [
        threading.Thread(target=walk, args=(market, k), daemon=True)
        for k, market in enumerate(markets)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    if crashes:
        raise crashes[0]
    failures.sort(key=lambda f: f[0])
    for _, failure in failures:
        if isinstance(failure, Blocked):
            raise failure
    products: dict[str, Product] = {}
    for page in sorted(pages):
        for card in pages[page]:
            products.setdefault(card.external_id, card)
    if failures and not products:
        raise failures[0][1]
    error = None
    if failures:
        # A broken page must not cost us the products already found.
        page, failure = failures[0]
        error = str(failure)
        logger.warning("listing stopped at page %s: %s", page, failure)
    return list(products.values())[:max_products], error


def collect(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    category: str,
    region: str,
    max_products: int,
    *,
    clock: Callable[[], datetime] = _utcnow,
) -> RunResult:
    """One crawl. Each market is a worker with its own connection, fetching in parallel."""
    key = markets[0].key
    run_id = db.start_run(conn, key, category, region, clock())
    result = RunResult(status="running")
    try:
        _collect_products(conn, markets, category, region, max_products, clock, result)
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


Outcome = OfferList | BaseException


def _fetch_in_parallel(
    markets: list[Marketplace], products: list[Product], region: str
) -> Iterator[tuple[Product, Outcome]]:
    """Offers for every product, in whatever order the workers finish them.

    Workers only talk to the marketplace; the caller does all database writes, so
    one connection is never shared between threads. Closing the iterator stops
    the workers after the request each one has in flight.
    """
    todo: queue.SimpleQueue[Product] = queue.SimpleQueue()
    for product in products:
        todo.put(product)
    done: queue.SimpleQueue[tuple[Product, Outcome] | None] = queue.SimpleQueue()
    stop = threading.Event()

    def work(market: Marketplace) -> None:
        try:
            while not stop.is_set():
                try:
                    product = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    outcome: Outcome = market.fetch_offers(product, region)
                except BaseException as error:  # handed to the caller, which decides
                    outcome = error
                done.put((product, outcome))
        finally:
            done.put(None)

    workers = [threading.Thread(target=work, args=(m,), daemon=True) for m in markets]
    for worker in workers:
        worker.start()
    try:
        running = len(workers)
        while running:
            item = done.get()
            if item is None:
                running -= 1
            else:
                yield item
    finally:
        stop.set()
        for worker in workers:
            worker.join()


def _collect_products(
    conn: psycopg.Connection,
    markets: list[Marketplace],
    category: str,
    region: str,
    max_products: int,
    clock: Callable[[], datetime],
    result: RunResult,
) -> None:
    key = markets[0].key
    products, result.error = discover(markets, category, region, max_products)
    if not products:
        raise MarketplaceError(f"category {category} returned no products")
    found = {p.external_id: p for p in db.requested_products(conn, key)}
    found.update({p.external_id: p for p in products})
    products = list(found.values())

    consecutive_blocks = 0
    consecutive_failures = 0
    with closing(_fetch_in_parallel(markets, products, region)) as outcomes:
        for product, outcome in outcomes:
            if isinstance(outcome, Blocked):
                result.products_failed += 1
                consecutive_blocks += 1
                if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                    raise outcome
                continue
            if isinstance(outcome, MarketplaceError):
                consecutive_blocks = 0
                consecutive_failures += 1
                result.products_failed += 1
                logger.warning("product %s failed: %s", product.external_id, outcome)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise outcome
                continue
            if isinstance(outcome, BaseException):
                raise outcome
            consecutive_blocks = 0
            consecutive_failures = 0
            now = clock()
            product_id = db.upsert_product(conn, key, product, now)
            db.record_offers(conn, key, product_id, region, outcome, now)
            conn.commit()
            result.products_ok += 1

    clean = result.products_failed == 0 and result.error is None
    result.status = "completed" if clean else "partial"
