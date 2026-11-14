"""Kaspi.kz public catalog JSON, the same endpoints the shop website calls.

Endpoint shapes come from market-sources (MIT, github.com/octrow/market-sources).
"""

import json
import logging
import re
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from datetime import datetime

from shynbaga.marketplaces import (
    BadRequest,
    Blocked,
    CardMetrics,
    Category,
    CategoryCheck,
    Facet,
    Facets,
    FacetValue,
    MarketplaceError,
    NotFound,
    Offer,
    OfferList,
    Product,
    SellerProfile,
)

BASE_URL = "https://kaspi.kz"
ALMATY = "750000000"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
OFFERS_PAGE_SIZE = 32
MAX_OFFER_PAGES = 5
CURRENCY = "KZT"
# The catalog root. Its answer lists the top-level nodes but trims their children
# and understates their popularity, so each top-level node is checked on its own.
ROOT_CATEGORY = "Categories"
# Page 300 answers, page 305 already 400 (2026-11-14): about 3600 cards per listing.
MAX_LISTING_PAGES = 300
# About 8.5 minutes in all: longer than the proxy outages seen so far.
RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0)

logger = logging.getLogger(__name__)


class _Transient(MarketplaceError):
    """The connection or the server failed, not Kaspi's judgement of us, so a retry may work."""



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


def seller_path(seller_id: str) -> str:
    # The bare /merchant/{id}/ answers 404; Kaspi redirects mixed-case ids to lowercase.
    return f"/shop/info/merchant/{quote(seller_id.lower(), safe='')}/address-tab/"


def seller_url(seller_id: str) -> str:
    return BASE_URL + seller_path(seller_id)


_SELLER_DATA = "BACKEND.components.merchant ="


def parse_seller_page(html: str) -> SellerProfile:
    start = html.find(_SELLER_DATA)
    if start < 0:
        raise MarketplaceError("seller page has no merchant data")
    begin = start + len(_SELLER_DATA)
    while begin < len(html) and html[begin].isspace():
        begin += 1
    try:
        data, _ = json.JSONDecoder().raw_decode(html, begin)
    except ValueError:
        raise MarketplaceError("seller page has unreadable merchant data") from None
    if not isinstance(data, dict):
        raise MarketplaceError("seller page has unreadable merchant data")
    metrics = data.get("merchantMetrics") if isinstance(data.get("merchantMetrics"), dict) else {}
    state = metrics.get("state") if isinstance(metrics.get("state"), dict) else {}
    orders = metrics.get("orderStatistics")
    segment = orders.get("ordersTotalSegment") if isinstance(orders, dict) else None
    created = _moment_or_naive(data.get("created"))
    return SellerProfile(
        joined_on=created.date() if created else None,
        logo_url=_text(data.get("logo")),
        state=_text(state.get("value")),
        score=_rating(state.get("score")),
        cancelled_pct=_percent(metrics, "cancelled"),
        returned_pct=_percent(metrics, "returned"),
        late_delivery_pct=_percent(metrics, "lateKaspiDelivery"),
        orders_segment=int(segment) if isinstance(segment, str) and segment.isdigit() else None,
    )


def _percent(metrics: dict, name: str) -> Decimal | None:
    entry = metrics.get(name)
    if not isinstance(entry, dict) or entry.get("notEnoughData") is True:
        return None
    raw = entry.get("percentage")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 100:
        return None
    return Decimal(str(raw)).quantize(Decimal("0.001"))


def _moment_or_naive(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        listed_at=_moment(card.get("createdTime")),
        metrics=CardMetrics(
            rating=_rating(card.get("rating")),
            reviews_count=_whole(card.get("reviewsQuantity")),
            price=_price(card.get("unitSalePrice")),
            list_price=_price(card.get("unitPrice")),
            price_with_bonus=_price(card.get("priceMinusBonus")),
            delivery=_text(card.get("deliveryDuration")),
            promos=_promos(card.get("promo")),
        ),
    )


def _text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _whole(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _rating(raw: object) -> Decimal | None:
    # Out of range would not fit the column and fail the whole run, not one product.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 5:
        return None
    return Decimal(str(raw)).quantize(Decimal("0.01"))


def _moment(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _promos(raw: object) -> tuple[dict[str, Any], ...]:
    """Kaspi's promo badges without their images: codes, types and bonus amounts."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        {k: v for k, v in promo.items() if k != "img" and isinstance(v, (str, int, float))}
        for promo in raw
        if isinstance(promo, dict)
    )


def parse_category(payload: Any, code: str) -> CategoryCheck:
    """The node and its children from a /pl/filters answer.

    The tree in the answer runs from the top level down to the asked node and its
    children. A child with an empty list of its own children is a leaf: that holds
    under any node but the root, whose answer trims the lists.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    tree = data.get("treeCategory") if isinstance(data, dict) else None
    items = tree.get("items") if isinstance(tree, dict) else None
    if not isinstance(items, list):
        raise MarketplaceError(f"category {code} answer has no tree")
    total = _whole(data.get("total")) or 0
    if code == ROOT_CATEGORY:
        children = [c for item in items if (c := _category(item, None))]
        return CategoryCheck(Category(code, "Все категории", total, 0, False), children)
    node = _find_node(items, code)
    if node is None:
        raise MarketplaceError(f"category {code} is missing from its own tree")
    found = _category(node, False)
    if found is None:
        raise MarketplaceError(f"category {code} has an unreadable node")
    raw_children = node.get("items") if isinstance(node.get("items"), list) else []
    children = []
    for item in raw_children:
        grandchildren = item.get("items") if isinstance(item, dict) else None
        is_leaf = not grandchildren
        if child := _category(item, is_leaf):
            children.append(child)
    return CategoryCheck(
        Category(found.code, found.title, found.products_count, found.popularity, not children),
        children,
    )


def parse_facets(payload: Any, query: str) -> Facets:
    """The filters a /pl/filters answer offers for a listing, with value counts.

    A filter's counts ignore that filter's own selection, so a filter already in the
    query lists values beyond it; callers skip those. Only the largest 255 values of
    a filter are listed.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    raw = data.get("filters") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise MarketplaceError(f"filters for {query} are missing")
    facets = []
    for item in raw:
        if not isinstance(item, dict) or not _text(item.get("id")):
            continue
        values = []
        for row in item.get("rows") or []:
            if not isinstance(row, dict):
                continue
            term, count = _text(row.get("id")), _whole(row.get("count"))
            if term and term.startswith(":") and count:
                values.append(FacetValue(term, _text(row.get("title")) or term, count))
        if values:
            facets.append(Facet(item["id"], _text(item.get("title")) or item["id"], values))
    return Facets(_whole(data.get("total")) or 0, facets)


def _find_node(items: list, code: str) -> dict | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if _category_code(item.get("id")) == code:
            return item
        if isinstance(item.get("items"), list) and (found := _find_node(item["items"], code)):
            return found
    return None


def _category_code(raw: object) -> str | None:
    prefix = ":category:"
    if isinstance(raw, str) and raw.startswith(prefix) and len(raw) > len(prefix):
        return raw[len(prefix):]
    return None


def _category(item: object, is_leaf: bool | None) -> Category | None:
    if not isinstance(item, dict):
        return None
    code = _category_code(item.get("id"))
    if code is None:
        return None
    title = _text(item.get("titleRu")) or _text(item.get("title")) or code
    return Category(
        code=code,
        title=title,
        products_count=_whole(item.get("count")) or 0,
        popularity=_whole(item.get("popularity")) or 0,
        is_leaf=is_leaf,
    )


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
                seller_rating=_rating(raw.get("merchantRating")),
                seller_reviews_count=_whole(raw.get("merchantReviewsQuantity")),
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


def _category_referer(category: str, region: str) -> str:
    return f"{BASE_URL}/shop/c/{quote(category.lower())}/?c={region}"


def _listing_params(
    category: str, region: str, page: int, filters: str = "", sort: str = "relevance"
) -> dict[str, Any]:
    # Filters are terms like ":manufacturerName:Apple:price:до 10 000 т" (2026-11-14:
    # terms combine; a price range outside Kaspi's fixed buckets is silently ignored).
    # Sorts: relevance, price-asc, price-desc, created-desc, rating; each serves its
    # own first ~300 pages.
    return {
        "q": f":category:{category}{filters}",
        "page": page,
        "c": region,
        "ui": "d",
        "sort": sort,
        "i": -1,
    }


class Kaspi:
    key = "kaspi"
    max_listing_pages = MAX_LISTING_PAGES
    root_category = ROOT_CATEGORY

    def __init__(
        self,
        *,
        proxies: list[str] | None = None,
        delay: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bytes_received = 0
        self._delay = delay
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._last_request = 0.0
        self._proxies = proxies or []
        self._proxy_index = 0
        self._transport = transport
        self._client_options = {
            "timeout": 30,
            "headers": {
                "user-agent": USER_AGENT,
                "accept": "application/json, text/*",
                "accept-language": "ru-RU,ru;q=0.9",
                "x-requested-with": "XMLHttpRequest",
            },
        }
        self._http = self._new_client()

    @property
    def proxy_number(self) -> int:
        """1-based number of the proxy in use, for logs: never log the URL, it holds a password."""
        return self._proxy_index + 1

    def _new_client(self) -> httpx.Client:
        # A test transport replaces the network, proxy included.
        proxy = self._proxies[self._proxy_index] if self._proxies and not self._transport else None
        return httpx.Client(proxy=proxy, transport=self._transport, **self._client_options)

    def list_products(
        self, category: str, region: str, page: int, filters: str = "", sort: str = "relevance"
    ) -> list[Product]:
        try:
            payload = self._request(
                "GET",
                # /pl/filters is heavier and answers 400 from page 2 on.
                "/yml/product-view/pl/results",
                region,
                referer=_category_referer(category, region),
                params=_listing_params(category, region, page, filters, sort),
            )
        except BadRequest:
            if page == 0:
                raise
            return []  # past the deepest page Kaspi serves
        return parse_products(payload)

    def fetch_category(self, code: str, region: str) -> CategoryCheck:
        payload = self._request(
            "GET",
            # Only page 0 of /pl/filters works, and that is all the tree needs.
            "/yml/product-view/pl/filters",
            region,
            referer=_category_referer(code, region),
            params=_listing_params(code, region, 0),
        )
        return parse_category(payload, code)

    def fetch_facets(self, category: str, filters: str, region: str) -> Facets:
        payload = self._request(
            "GET",
            "/yml/product-view/pl/filters",
            region,
            referer=_category_referer(category, region),
            params=_listing_params(category, region, 0, filters),
        )
        return parse_facets(payload, f"{category}{filters}")

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
        total = None
        for page in range(MAX_OFFER_PAGES):
            payload = self._offers_page(product.offers_ref, region, page)
            batch = parse_offers(payload)
            if page == 0:
                total = _whole(payload.get("offersCount"))
            known = len(offers)
            for offer in batch:
                current = offers.get(offer.seller_id)
                if current is None or offer.price < current.price:
                    offers[offer.seller_id] = offer
            if len(batch) < OFFERS_PAGE_SIZE:
                return OfferList(list(offers.values()), total=total)
            if len(offers) == known:
                # The server repeated a full page, so the rest is out of reach.
                break
        return OfferList(list(offers.values()), complete=False, total=total)

    def fetch_seller(self, seller_id: str) -> SellerProfile:
        html = self._request(
            "GET",
            seller_path(seller_id),
            ALMATY,
            referer=f"{BASE_URL}/shop/",
            accept="text/html",
        )
        return parse_seller_page(html)

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
        if self._proxy_index != 0:
            # Back to our own proxy: parallel workers each have one, and staying on
            # the backup after a hiccup would leave two workers on a single IP.
            self._proxy_index = -1
            self._switch_proxy()
        # Refusals (Blocked) are never retried: hammering a block only makes it worse.
        for pause in (*self._retry_delays, None):
            try:
                return self._request_once(method, path, region, referer=referer, **kwargs)
            except _Transient as error:
                # A failed proxy connection can stay stuck in the pool, and then every
                # later request times out waiting for it (PoolTimeout on 2026-11-08).
                # Switching proxies is for broken connections only: after a Blocked we
                # stop rather than hide from the block behind another IP.
                self._switch_proxy()
                if pause is None:
                    raise
                logger.warning(
                    "%s, retrying in %.0f s via proxy %s", error, pause, self.proxy_number
                )
                self._sleep(pause)

    def _request_once(
        self,
        method: str,
        path: str,
        region: str,
        *,
        referer: str,
        accept: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Decoded JSON, or the page text when `accept` asks for something else."""
        self._pace()
        headers = {
            "referer": referer,
            "x-ks-city": region,
            "cookie": f"kaspi.storefront.cookie.city={region}",
        }
        if accept:
            headers["accept"] = accept
        try:
            response = self._http.request(method, BASE_URL + path, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise _Transient(f"{type(error).__name__} on {path}") from error
        self.bytes_received += len(response.content)
        if response.status_code in (403, 429):
            raise Blocked(f"HTTP {response.status_code} on {path}")
        if response.status_code >= 500:
            raise _Transient(f"HTTP {response.status_code} on {path}")
        if response.status_code == 404:
            raise NotFound(f"HTTP 404 on {path}")
        if response.status_code == 400:
            # Kaspi answers this past the deepest listing page, too.
            raise BadRequest(f"HTTP 400 on {path}")
        if response.status_code != 200:
            raise MarketplaceError(f"HTTP {response.status_code} on {path}")
        if accept and "json" not in accept:
            return response.text
        try:
            return response.json()
        except ValueError:
            # A 200 HTML page instead of JSON is a challenge page.
            raise Blocked(f"non-JSON response on {path}") from None

    def _switch_proxy(self) -> None:
        if self._proxies:
            self._proxy_index = (self._proxy_index + 1) % len(self._proxies)
        self._http.close()
        self._http = self._new_client()

    def _pace(self) -> None:
        wait = self._last_request + self._delay - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def close(self) -> None:
        self._http.close()
