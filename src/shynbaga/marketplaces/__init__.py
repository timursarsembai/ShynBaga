from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol


class MarketplaceError(Exception):
    """A request failed or returned something unusable; other products may still work."""


class Blocked(MarketplaceError):
    """The marketplace refused us; continuing would only make the block worse."""


class NotFound(MarketplaceError):
    """The marketplace has no such page: asking again will not help."""


@dataclass(frozen=True)
class CardMetrics:
    """What a listing card says about a product at one moment, kept for analytics."""

    rating: Decimal | None = None
    reviews_count: int | None = None
    price: Decimal | None = None
    # The crossed-out price; above `price` it claims a discount.
    list_price: Decimal | None = None
    price_with_bonus: Decimal | None = None
    delivery: str | None = None
    promos: tuple[dict[str, Any], ...] = ()


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
    listed_at: datetime | None = None
    metrics: CardMetrics | None = None


@dataclass(frozen=True)
class Offer:
    seller_id: str
    seller_name: str
    price: Decimal
    currency: str
    seller_url: str | None = None
    seller_rating: Decimal | None = None
    seller_reviews_count: int | None = None


@dataclass(frozen=True)
class SellerProfile:
    """A seller's page at one moment. The phone is left out on purpose: for a sole
    trader it is personal data, and analytics does not need it."""

    joined_on: date | None = None
    logo_url: str | None = None
    state: str | None = None
    score: Decimal | None = None
    cancelled_pct: Decimal | None = None
    returned_pct: Decimal | None = None
    late_delivery_pct: Decimal | None = None
    # A size bucket, such as 20000 for "about 20 000 orders".
    orders_segment: int | None = None


@dataclass(frozen=True)
class Category:
    """A node of the catalog tree as one answer showed it."""

    code: str
    title: str
    products_count: int
    popularity: int
    # None when this answer cannot tell whether the node has children.
    is_leaf: bool | None


@dataclass(frozen=True)
class CategoryCheck:
    """A node's own answer: its figures and the full list of its children."""

    node: Category
    children: list[Category]


@dataclass(frozen=True)
class OfferList:
    offers: list[Offer] = field(default_factory=list)
    # False when sellers may be missing, so absence must not read as "stopped selling".
    complete: bool = True
    # How many offers the marketplace says the product has.
    total: int | None = None


class Marketplace(Protocol):
    key: str
    bytes_received: int

    # The deepest listing page served; past it the marketplace refuses.
    max_listing_pages: int
    # The code of the catalog's top node.
    root_category: str

    def list_products(self, category: str, region: str, page: int) -> list[Product]: ...

    def fetch_category(self, code: str, region: str) -> CategoryCheck: ...

    def fetch_product(self, external_id: str, region: str) -> Product: ...

    def fetch_offers(self, product: Product, region: str) -> OfferList: ...

    def fetch_seller(self, seller_id: str) -> SellerProfile: ...

    def close(self) -> None: ...


def identify(url: str) -> tuple[str, str] | None:
    """(marketplace key, product id) for a product link, None if we do not recognize it."""
    from shynbaga.marketplaces.kaspi import parse_product_url

    external_id = parse_product_url(url)
    return ("kaspi", external_id) if external_id else None


def create(key: str, *, proxies: list[str], delay: float) -> Marketplace:
    if key == "kaspi":
        from shynbaga.marketplaces.kaspi import Kaspi

        return Kaspi(proxies=proxies, delay=delay)
    raise ValueError(f"unknown marketplace: {key}")
