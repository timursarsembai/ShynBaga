import argparse
import logging
import os
import sys

from shinbaga import collector, db
from shinbaga.marketplaces import MarketplaceError, create

DEFAULT_REGION = "750000000"  # Almaty
DEFAULT_CATEGORY = "Smartphones"


def main() -> int:
    parser = argparse.ArgumentParser(prog="shinbaga")
    parser.add_argument("command", choices=["check", "migrate", "collect"])
    parser.add_argument("--marketplace", default="kaspi")
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--max-products", type=int, default=1000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "migrate":
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            db.migrate(conn)
        return 0

    market = create(
        args.marketplace,
        proxy=os.environ.get("SHINBAGA_PROXY_URL") or None,
        delay=float(os.environ.get("SHINBAGA_REQUEST_DELAY") or 10),
    )
    try:
        if args.command == "check":
            return _check(market, args.category, args.region)
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            result = collector.collect(
                conn, market, args.category, args.region, args.max_products
            )
        print(result)
        return 0 if result.status in ("completed", "partial") else 1
    finally:
        market.close()


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
