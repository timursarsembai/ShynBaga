import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

from shynbaga import collector, db, notify
from shynbaga.marketplaces import MarketplaceError, create, identify

DEFAULT_REGION = "750000000"  # Almaty
DEFAULT_CATEGORY = "Smartphones"


def main() -> int:
    parser = argparse.ArgumentParser(prog="shynbaga")
    parser.add_argument("command", choices=["check", "migrate", "collect", "lookup", "test-alert"])
    parser.add_argument("url", nargs="?", help="product link, for lookup")
    parser.add_argument("--marketplace", default="kaspi")
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--max-products", type=int, default=1000)
    parser.add_argument(
        "--min-interval-hours",
        type=float,
        default=0,
        help="skip collect when a run started this recently, so restarts do not re-crawl",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "migrate":
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            db.migrate(conn)
        return 0

    if args.command == "test-alert":
        return 0 if notify.send("ShynBaga: проверка уведомлений о сборе.") else 1

    if args.command == "lookup":
        found = identify(args.url or "")
        if found is None:
            print("Это не похоже на ссылку на товар.", file=sys.stderr)
            return 1
        args.marketplace, external_id = found

    market = create(
        args.marketplace,
        proxy=os.environ.get("SHYNBAGA_PROXY_URL") or None,
        delay=float(os.environ.get("SHYNBAGA_REQUEST_DELAY") or 10),
    )
    try:
        if args.command == "check":
            return _check(market, args.category, args.region)
        if args.command == "lookup":
            with db.connect(os.environ["DATABASE_URL"]) as conn:
                return _lookup(conn, market, external_id, args.region)
        return _collect(market, args)
    finally:
        market.close()


def _collect(market, args: argparse.Namespace) -> int:
    try:
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            started = db.last_run_started_at(conn, market.key, args.category, args.region)
            age = datetime.now(UTC) - started if started else None
            if age is not None and age < timedelta(hours=args.min_interval_hours):
                return 0
            result = collector.collect(
                conn, market, args.category, args.region, args.max_products
            )
    except Exception as error:
        # Only the type: database errors can quote the connection string.
        notify.send(f"ShynBaga: сборщик упал с ошибкой {type(error).__name__}.")
        raise
    print(result)
    if text := collector.alert_text(market.key, args.category, result):
        notify.send(text)
    return 0 if result.status in ("completed", "partial") else 1


def _lookup(conn, market, external_id: str, region: str) -> int:
    try:
        collector.track(conn, market, external_id, region)
    except MarketplaceError as error:
        print(f"Товар не удалось получить: {error}", file=sys.stderr)
        return 1
    info, history = db.product_history(conn, market.key, external_id, region)
    print(info.title)
    print(info.url)
    print("\nТекущие цены:")
    current = sorted((p for p in history if p.is_current), key=lambda p: p.price)
    for period in current:
        print(f"  {period.price:>12} {period.currency}  {period.seller}  {period.seller_url}")
    print("\nМинимальная цена по времени:")
    for moment, price in db.lowest_prices(history):
        print(f"  {moment:%Y-%m-%d %H:%M} UTC  {price}")
    return 0


def _check(market, category: str, region: str) -> int:
    """Two real requests: the cheapest proof that collection can work from this IP."""
    try:
        products = market.list_products(category, region, 0)
        print(f"products on page 0: {len(products)}")
        if not products:
            return 1
        offers = market.fetch_offers(products[0], region)
    except MarketplaceError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"first product: {products[0].title}")
    print(f"offers: {len(offers.offers)}, complete: {offers.complete}")
    for offer in sorted(offers.offers, key=lambda o: o.price)[:5]:
        print(f"  {offer.price} {offer.currency}  {offer.seller_name}")
    print(f"bytes received: {market.bytes_received}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
