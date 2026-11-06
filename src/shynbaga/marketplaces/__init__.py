from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


class MarketplaceError(Exception):
    """A request failed or returned something unusable; other products may still work."""


class Blocked(MarketplaceError):
    """The marketplace refused us; continuing would only make the block worse."""


@dataclass(frozen=True)
class Product:
    external_id: str
    title: str
    # The id the marketplace's offers endpoint answers to.
    offers_ref: str
    url: str | None = None
    image_url: str | None = None
    brand: str | None = None
    category_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class Offer:
    seller_id: str
    seller_name: str
    price: Decimal
    currency: str
    seller_url: str | None = None


@dataclass(frozen=True)
class OfferList:
    offers: list[Offer] = field(default_factory=list)
    # False when sellers may be missing, so absence must not read as "stopped selling".
    complete: bool = True


class Marketplace(Protocol):
    key: str
    bytes_received: int

    def list_products(self, category: str, region: str, page: int) -> list[Product]: ...

    def fetch_product(self, external_id: str, region: str) -> Product: ...

    def fetch_offers(self, product: Product, region: str) -> OfferList: ...

    def close(self) -> None: ...


def identify(url: str) -> tuple[str, str] | None:
    """(marketplace key, product id) for a product link, None if we do not recognize it."""
    from shynbaga.marketplaces.kaspi import parse_product_url

    external_id = parse_product_url(url)
    return ("kaspi", external_id) if external_id else None


def create(key: str, *, proxy: str | None, delay: float) -> Marketplace:
    if key == "kaspi":
        from shynbaga.marketplaces.kaspi import Kaspi

        return Kaspi(proxy=proxy, delay=delay)
    raise ValueError(f"unknown marketplace: {key}")
