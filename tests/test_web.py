import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from shynbaga import db
from shynbaga.web import auth
from shynbaga.web.app import _safe_next, create_app

PASSWORD = "correct horse battery"
T1 = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def password_hash():
    return auth.hash_password(PASSWORD)


@pytest.fixture
def data(conn):
    conn.execute(
        """INSERT INTO products (id, marketplace, external_id, title, brand, category_path,
                                 url, first_seen_at, last_seen_at)
           VALUES (1, 'kaspi', '111', 'Phone <b>One</b>', 'Apple', '{Телефоны,Смартфоны}',
                   'https://kaspi.kz/shop/p/one-111/', %(t1)s, %(t2)s),
                  (2, 'kaspi', '222', 'Phone Two', 'Samsung', '{}', NULL, %(t1)s, %(t2)s)""",
        {"t1": T1, "t2": T2},
    )
    conn.execute(
        """INSERT INTO sellers (id, marketplace, external_id, name, url, first_seen_at,
                                last_seen_at, joined_on)
           VALUES (1, 'kaspi', 'AAA', 'Shop A', NULL, %(t1)s, %(t2)s, '2020-01-02'),
                  (2, 'kaspi', 'BBB', 'Shop B', NULL, %(t1)s, %(t2)s, NULL)""",
        {"t1": T1, "t2": T2},
    )
    conn.execute(
        """INSERT INTO price_intervals (product_id, seller_id, region, price, currency,
                                        first_seen_at, last_seen_at, is_current)
           VALUES (1, 1, '750000000', 100000, 'KZT', %(t1)s, %(t1)s, false),
                  (1, 1, '750000000', 120000, 'KZT', %(t2)s, %(t2)s, true),
                  (1, 2, '750000000', 110000, 'KZT', %(t1)s, %(t2)s, true),
                  (2, 2, '750000000', 50000, 'KZT', %(t1)s, %(t2)s, true)""",
        {"t1": T1, "t2": T2},
    )
    conn.execute(
        """INSERT INTO product_snapshots (product_id, region, observed_at, position, rating,
                                          reviews_count, price, promos, offers_total, offers_seen)
           VALUES (1, '750000000', %s, 3, 4.8, 1200, 110000,
                   '[{"type": "LOAN", "displayDuration": 24}]', 2, 2)""",
        (T2,),
    )
    conn.execute(
        """INSERT INTO seller_profiles (seller_id, observed_on, state, score, cancelled_pct)
           VALUES (1, '2026-11-09', 'TOP', 4.9, 1.5)"""
    )
    conn.execute(
        """INSERT INTO categories (marketplace, code, parent_code, title, depth,
                                   products_count, popularity, is_leaf, checked_at, swept_at,
                                   swept_products)
           VALUES ('kaspi', 'Categories', NULL, 'Все категории', 0, 0, 0, false, %(t1)s,
                   NULL, NULL),
                  ('kaspi', 'Lamps', 'Categories', 'Лампы', 1, 40, 200, true, %(t1)s,
                   %(t2)s, 1)""",
        {"t1": T1, "t2": T2},
    )
    conn.execute(
        """INSERT INTO products (id, marketplace, external_id, title, category_path,
                                 first_seen_at, last_seen_at, category_code, position,
                                 card_price, card_seen_at, card)
           VALUES (3, 'kaspi', '333', 'Lamp Three', '{}', %(t1)s, %(t2)s, 'Lamps', 1,
                   7000, %(t2)s, '{"rating": "4.5", "reviews_count": 12, "promos": []}')""",
        {"t1": T1, "t2": T2},
    )
    conn.execute(
        """INSERT INTO card_prices (product_id, region, price, first_seen_at, last_seen_at,
                                    is_current)
           VALUES (3, '750000000', 8000, %(t1)s, %(t1)s, false),
                  (3, '750000000', 7000, %(t2)s, %(t2)s, true)""",
        {"t1": T1, "t2": T2},
    )
    conn.execute(
        """INSERT INTO category_segments (marketplace, category_code, filters, facet_ids,
                                          title, products_count, popularity, swept_at,
                                          swept_products)
           VALUES ('kaspi', 'Lamps', ':manufacturerName:Philips', '{manufacturerName}',
                   'Philips', 20, 50, %(t2)s, 17)""",
        {"t2": T2},
    )
    conn.execute("UPDATE products SET card_changes = 1, score = 200 WHERE id = 3")
    for product_id in (1, 2):
        db._summarize_offers(conn, product_id)
    conn.commit()
    return conn


@pytest.fixture
def client(data, password_hash):
    app = create_app(
        database_url=os.environ["TEST_DATABASE_URL"], user="admin",
        password_hash=password_hash, secret="s" * 40,
    )
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def login(client, password=PASSWORD, username="admin", next="/"):
    return client.post(
        "/login", data={"username": username, "password": password, "next": next},
        follow_redirects=False,
    )


def test_password_hash_round_trip():
    stored = auth.hash_password("abc")
    assert auth.verify_password("abc", stored)
    assert not auth.verify_password("abd", stored)
    assert not auth.verify_password("abc", "garbage")
    assert "$" not in stored  # docker compose would expand it in .env


def test_sessions_expire_and_follow_the_password():
    sessions = auth.Sessions("k" * 40, "hash1")
    token = sessions.issue(now=1000)
    assert sessions.valid(token, now=1001)
    assert not sessions.valid(token, now=1000 + auth.SESSION_SECONDS + 1)
    assert not auth.Sessions("k" * 40, "hash2").valid(token, now=1001)
    assert not sessions.valid(token.replace(".", ".0"), now=1001)
    assert not sessions.valid("abc.def", now=1001)


def test_throttle_counts_per_ip_and_in_total():
    throttle = auth.Throttle(per_ip=2, total=3, window=60)
    throttle.failed("a", now=0)
    throttle.failed("a", now=1)
    assert not throttle.allowed("a", now=2)
    assert throttle.allowed("b", now=2)
    throttle.failed("b", now=3)
    assert not throttle.allowed("c", now=4)
    assert throttle.allowed("a", now=100)


def test_next_stays_on_site():
    assert _safe_next("/sellers?q=1") == "/sellers?q=1"
    for bad in ("//evil.com", "/\\evil.com", "https://evil.com", "", None):
        assert _safe_next(bad) == "/"


def test_pages_need_login(client):
    for path in ("/", "/products/1", "/sellers", "/sellers/1", "/runs", "/categories"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")
    assert client.get("/login").status_code == 200


def test_wrong_login_is_refused(client):
    assert login(client, password="wrong").status_code == 200
    assert login(client, username="root").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_shows_every_page(client):
    response = login(client, next="/sellers")
    assert response.status_code == 303 and response.headers["location"] == "/sellers"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "secure" in response.headers["set-cookie"].lower()

    home = client.get("/")
    assert home.status_code == 200
    assert "Phone &lt;b&gt;One&lt;/b&gt;" in home.text  # escaped
    assert "Phone Two" in home.text
    assert "рассрочка 24 мес" in home.text

    product = client.get("/products/1")
    assert product.status_code == 200
    assert "Shop A" in product.text and "Shop B" in product.text
    assert "<polyline" in product.text

    sellers = client.get("/sellers")
    assert "Shop A" in sellers.text and "TOP" in sellers.text

    seller = client.get("/sellers/2")
    assert "Phone Two" in seller.text and "лучшая" in seller.text
    assert client.get("/sellers/1").text.count("/products/1") >= 1

    assert client.get("/runs").status_code == 200
    assert client.get("/products/999").status_code == 404
    assert client.get("/sellers/999").status_code == 404


def test_product_filters(client):
    login(client)
    assert "Phone Two" not in client.get("/?q=b>one").text
    assert "Phone Two" in client.get("/?q=222").text
    assert "Phone Two" in client.get("/?brand=Samsung").text
    only_cheap = client.get("/?price_to=60000").text
    assert "Phone Two" in only_cheap and "Phone &lt;b&gt;One" not in only_cheap
    assert "Ничего не найдено" in client.get("/?q=%25%25nothing").text
    assert client.get("/?sort=bogus;drop").status_code == 200
    assert "Ничего не найдено" in client.get("/?page_no=5").text


def test_catalog_products_and_categories(client):
    login(client)
    lamps = client.get("/?category=Lamps").text
    assert "Lamp Three" in lamps and "Phone Two" not in lamps
    assert "Lamp Three" in client.get("/?price_to=7500").text
    assert "Lamp Three" in client.get("/?only=card_changed").text
    lamp = client.get("/products/3").text
    assert "<polyline" in lamp and "товары категории" in lamp
    categories = client.get("/categories").text
    assert "Лампы" in categories and "конечная" in categories
    assert "1 (1)" in categories  # one part, swept
    assert "Лампы" not in client.get("/categories?only=unswept").text
    assert client.get("/categories?sort=bogus;drop").status_code == 200
    assert client.get("/categories?only=split").status_code == 200


def test_seller_filters(client):
    login(client)
    assert "Shop B" not in client.get("/sellers?q=shop a").text
    assert "Shop B" not in client.get("/sellers?only=profiled").text


def test_logout_clears_the_cookie(client):
    login(client)
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303
