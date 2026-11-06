"""Kaspi.kz public catalog JSON, the same endpoints the shop website calls.

Endpoint shapes come from market-sources (MIT, github.com/octrow/market-sources).
"""

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from shinbaga.marketplaces import Blocked, MarketplaceError, Offer, OfferList, Product

BASE_URL = "https://kaspi.kz"
ALMATY = "750000000"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
OFFERS_PAGE_SIZE = 32
MAX_OFFER_PAGES = 5
CURRENCY = "KZT"

_LEADING_DIGITS = re.compile(r"^\d+")
_PRODUCT_PATH = re.compile(r"^/shop/(?:[^/]+/)?p/(?:[^/]*-)?(\d+)[a-z]*/?$", re.IGNORECASE)


def parse_product_url(url: str) -> str | None:
    url = url.strip()
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host != "kaspi.kz" and not host.endswith(".kaspi.kz"):
        return None
    match = _PRODUCT_PATH.match(parts.path)
    return match.group(1) if match else None


def product_url(link: str) -> str:
    """The public address without the city query, so one product has one URL."""
    path = link.split("?")[0]
    if path.startswith("http"):
        return path
    return f"{BASE_URL}/shop{path}" if path.startswith("/p/") else BASE_URL + path


def seller_url(seller_id: str) -> str:
    # Kaspi redirects mixed-case ids such as "Sulpak" to the lowercase address.
    return f"{BASE_URL}/shop/info/merchant/{quote(seller_id.lower(), safe='')}/"


def parse_products(payload: Any) -> list[Product]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise MarketplaceError("product list has no data list")
    cards = payload["data"]
    return [p for card in cards if isinstance(card, dict) and (p := _parse_card(card))]


def _parse_card(card: dict) -> Product | None:
    # Fashion cards carry ids like "16540138b"; the bare digits identify the product.
    match = _LEADING_DIGITS.match(str(card.get("id") or ""))
    title = card.get("title")
    if not match or not title or not isinstance(title, str):
        return None
    images = card.get("previewImages") or []
    image = images[0] if isinstance(images, list) and images and isinstance(images[0], dict) else {}
    link = _text(card.get("shopLink"))
    brand = _text(card.get("brand"))
    category = card.get("categoryRu") or card.get("category")
    return Product(
        external_id=match.group(),
        title=title,
        offers_ref=str(card.get("configSku") or match.group()),
        url=product_url(link) if link else None,
        image_url=_text(image.get("medium")) or _text(image.get("large")),
        brand=brand,
        category_path=tuple(c for c in category if isinstance(c, str))
        if isinstance(category, list)
        else (),
    )


def _text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def parse_product(payload: Any, external_id: str) -> Product:
    card = payload.get("card") if isinstance(payload, dict) else None
    title = _text(card.get("name")) if isinstance(card, dict) else None
    if not title:
        raise MarketplaceError(f"product {external_id} has no card")
    gallery = payload.get("galleryImages")
    image = gallery[0] if isinstance(gallery, list) and gallery else {}
    category = _text(payload.get("masterCategoryName"))
    return Product(
        external_id=external_id,
        title=title,
        offers_ref=external_id,
        url=product_url(f"/p/-{external_id}/"),
        image_url=(_text(image.get("medium")) or _text(image.get("large")))
        if isinstance(image, dict)
        else None,
        brand=_text(card.get("brand")),
        category_path=(category,) if category else (),
    )


def parse_offers(payload: Any) -> list[Offer]:
    if not isinstance(payload, dict) or not isinstance(payload.get("offers", []), list):
        raise MarketplaceError("offers response has an unexpected shape")
    if "offers" not in payload:
        # Empty must stay distinct from a changed format, or every seller reads as gone.
        raise MarketplaceError("offers response has no offers field")
    offers = []
    for raw in payload["offers"]:
        if not isinstance(raw, dict):
            continue
        price = _price(raw.get("price"))
        seller_id = raw.get("merchantId")
        if price is None or not seller_id:
            continue
        offers.append(
            Offer(
                seller_id=str(seller_id),
                seller_name=_text(raw.get("merchantName")) or str(seller_id),
                price=price,
                currency=CURRENCY,
                seller_url=seller_url(str(seller_id)),
            )
        )
    return offers


def _price(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        price = Decimal(str(raw))
    except InvalidOperation:
        return None
    return price if price.is_finite() and price > 0 else None


class Kaspi:
    key = "kaspi"

    def __init__(
        self,
        *,
        proxy: str | None = None,
        delay: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.bytes_received = 0
        self._delay = delay
        self._last_request = 0.0
        self._http = httpx.Client(
            proxy=proxy,
            transport=transport,
            timeout=30,
            headers={
                "user-agent": USER_AGENT,
                "accept": "application/json, text/*",
                "accept-language": "ru-RU,ru;q=0.9",
                "x-requested-with": "XMLHttpRequest",
            },
        )

    def list_products(self, category: str, region: str, page: int) -> list[Product]:
        payload = self._request(
            "GET",
            # /pl/filters is heavier and answers 400 from page 2 on.
            "/yml/product-view/pl/results",
            region,
            referer=f"{BASE_URL}/shop/c/{category.lower()}/?c={region}",
            params={
                "q": f":category:{category}",
                "page": page,
                "c": region,
                "ui": "d",
                "sort": "relevance",
                "i": -1,
            },
        )
        return parse_products(payload)

    def fetch_product(self, external_id: str, region: str) -> Product:
        payload = self._request(
            "GET",
            f"/yml/content/item/product/{external_id}",
            region,
            referer=f"{BASE_URL}/shop/p/-{external_id}/?c={region}",
            params={"c": region},
        )
        return parse_product(payload, external_id)

    def fetch_offers(self, product: Product, region: str) -> OfferList:
        offers: dict[str, Offer] = {}
        for page in range(MAX_OFFER_PAGES):
            batch = parse_offers(self._offers_page(product.offers_ref, region, page))
            known = len(offers)
            for offer in batch:
                current = offers.get(offer.seller_id)
                if current is None or offer.price < current.price:
                    offers[offer.seller_id] = offer
            if len(batch) < OFFERS_PAGE_SIZE:
                return OfferList(list(offers.values()))
            if len(offers) == known:
                # The server repeated a full page, so the rest is out of reach.
                break
        return OfferList(list(offers.values()), complete=False)

    def _offers_page(self, ref: str, region: str, page: int) -> Any:
        return self._request(
            "POST",
            f"/yml/offer-view/offers/{ref}",
            region,
            referer=f"{BASE_URL}/shop/p/-{ref}/?c={region}",
            json={
                "cityId": region,
                "id": ref,
                "merchantUID": "",
                "limit": OFFERS_PAGE_SIZE,
                "page": page,
                "sort": True,
                "installationId": "-1",
            },
        )

    def _request(self, method: str, path: str, region: str, *, referer: str, **kwargs: Any) -> Any:
        self._pace()
        headers = {
            "referer": referer,
            "x-ks-city": region,
            "cookie": f"kaspi.storefront.cookie.city={region}",
        }
        try:
            response = self._http.request(method, BASE_URL + path, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise MarketplaceError(f"{type(error).__name__} on {path}") from error
        self.bytes_received += len(response.content)
        if response.status_code in (403, 429):
            raise Blocked(f"HTTP {response.status_code} on {path}")
        if response.status_code != 200:
            raise MarketplaceError(f"HTTP {response.status_code} on {path}")
        try:
            return response.json()
        except ValueError:
            # A 200 HTML page instead of JSON is a challenge page.
            raise Blocked(f"non-JSON response on {path}") from None

    def _pace(self) -> None:
        wait = self._last_request + self._delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def close(self) -> None:
        self._http.close()
