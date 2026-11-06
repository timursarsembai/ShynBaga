import json
from decimal import Decimal

import httpx
import pytest

from shinbaga.marketplaces import Blocked, MarketplaceError, Product
from shinbaga.marketplaces.kaspi import (
    ALMATY,
    BASE_URL,
    OFFERS_PAGE_SIZE,
    Kaspi,
    parse_offers,
    parse_products,
)

PRODUCT = Product(external_id="1", title="Phone", offers_ref="1")


def client(handler) -> Kaspi:
    return Kaspi(delay=0, transport=httpx.MockTransport(handler))


def raw_offers(start: int, count: int) -> list[dict]:
    return [
        {"merchantId": f"m{i}", "merchantName": f"Shop {i}", "price": 1000 + i}
        for i in range(start, start + count)
    ]


def test_parse_products_maps_a_card():
    [product] = parse_products(
        {
            "data": [
                {
                    "id": "123",
                    "title": "iPhone",
                    "shopLink": "/p/iphone-123/",
                    "previewImages": [{"medium": "https://img.test/m.jpg"}],
                    "brand": "Apple",
                    "categoryRu": ["Телефоны", "Смартфоны"],
                }
            ]
        }
    )
    assert product.external_id == "123"
    assert product.offers_ref == "123"
    assert product.url == f"{BASE_URL}/shop/p/iphone-123/"
    assert product.image_url == "https://img.test/m.jpg"
    assert product.category_path == ("Телефоны", "Смартфоны")


def test_fashion_variant_uses_bare_id_and_config_sku_for_offers():
    [product] = parse_products(
        {"data": [{"id": "16540138b", "title": "Shoes", "configSku": "15358806b"}]}
    )
    assert product.external_id == "16540138"
    assert product.offers_ref == "15358806b"


def test_card_without_title_is_skipped():
    assert parse_products({"data": [{"id": "1"}]}) == []


def test_malformed_card_fields_do_not_reach_the_database():
    [product] = parse_products(
        {
            "data": [
                "garbage",
                {"id": "1", "title": 5},
                {"id": "2", "title": "Ok", "shopLink": 7, "brand": {}, "previewImages": "x"},
            ]
        }
    )
    assert (product.external_id, product.url, product.brand) == ("2", None, None)


def test_product_list_without_data_is_an_error():
    with pytest.raises(MarketplaceError):
        parse_products({"unexpected": True})


def test_parse_offers_maps_an_offer():
    [offer] = parse_offers(
        {"offers": [{"merchantId": 42, "merchantName": "Alser", "price": 389990}]}
    )
    assert (offer.seller_id, offer.seller_name) == ("42", "Alser")
    assert offer.price == Decimal("389990")
    assert offer.currency == "KZT"


@pytest.mark.parametrize("price", [None, 0, -5, True, "n/a", "NaN"])
def test_offer_with_an_unusable_price_is_dropped(price):
    assert parse_offers({"offers": [{"merchantId": "1", "price": price}]}) == []


def test_malformed_offer_is_skipped():
    assert parse_offers({"offers": ["garbage"]}) == []


def test_empty_offers_is_a_valid_answer():
    assert parse_offers({"offers": []}) == []


@pytest.mark.parametrize("payload", [{}, {"offers": "x"}, []])
def test_changed_offers_format_is_an_error_not_an_empty_list(payload):
    with pytest.raises(MarketplaceError):
        parse_offers(payload)


@pytest.mark.parametrize("status", [403, 429])
def test_refusal_is_blocked(status):
    market = client(lambda request: httpx.Response(status, text="<html>"))
    with pytest.raises(Blocked):
        market.list_products("Smartphones", ALMATY, 0)
    assert market.bytes_received == len("<html>")


def test_html_with_status_200_is_blocked():
    market = client(lambda request: httpx.Response(200, text="<html>challenge"))
    with pytest.raises(Blocked):
        market.fetch_offers(PRODUCT, ALMATY)


def test_server_error_is_not_a_block():
    market = client(lambda request: httpx.Response(502))
    with pytest.raises(MarketplaceError) as caught:
        market.fetch_offers(PRODUCT, ALMATY)
    assert not isinstance(caught.value, Blocked)


def test_transport_failure_hides_the_proxy_url():
    def fail(request):
        raise httpx.ConnectError("http://user:secret@proxy:1 refused")

    with pytest.raises(MarketplaceError) as caught:
        client(fail).fetch_offers(PRODUCT, ALMATY)
    assert "secret" not in str(caught.value)


def test_request_carries_city_and_category():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"data": []})

    client(handler).list_products("Smartphones", ALMATY, 2)
    request = seen["request"]
    assert request.headers["x-ks-city"] == ALMATY
    assert request.url.path == "/yml/product-view/pl/results"
    assert request.url.params["q"] == ":category:Smartphones"
    assert request.url.params["page"] == "2"


def test_offers_are_read_across_pages():
    pages = {0: raw_offers(0, OFFERS_PAGE_SIZE), 1: raw_offers(OFFERS_PAGE_SIZE, 3)}

    def handler(request):
        return httpx.Response(200, json={"offers": pages[json.loads(request.content)["page"]]})

    offers = client(handler).fetch_offers(PRODUCT, ALMATY)
    assert len(offers.offers) == OFFERS_PAGE_SIZE + 3
    assert offers.complete


def test_server_ignoring_page_stops_and_is_incomplete():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"offers": raw_offers(0, OFFERS_PAGE_SIZE)})

    offers = client(handler).fetch_offers(PRODUCT, ALMATY)
    assert len(calls) == 2
    assert len(offers.offers) == OFFERS_PAGE_SIZE
    assert not offers.complete
