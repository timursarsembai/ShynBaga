"""Draft admin site: everything collected so far, behind one login. Desktop only."""

import os
from collections import namedtuple
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from shynbaga import db
from shynbaga.web import auth, queries

COOKIE = "shynbaga_session"
ALMATY = ZoneInfo("Asia/Almaty")
Period = namedtuple("Period", "price first_seen_at last_seen_at")

_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' https: data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}


def _when(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone(ALMATY).strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def _money(value) -> str:
    if value is None:
        return "—"
    whole = f"{Decimal(value):,.0f}" if Decimal(value) == int(value) else f"{Decimal(value):,.2f}"
    return whole.replace(",", " ") + " ₸"


def _num(value, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def _took(value) -> str:
    if value is None:
        return "—"
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60} ч {minutes % 60:02d} мин"


_DELIVERY = {
    "EXPRESS": "экспресс", "TODAY": "сегодня", "TOMORROW": "завтра",
    "TILL_2_DAYS": "до 2 дней", "TILL_5_DAYS": "до 5 дней", "OTHER": "другое",
}


def _delivery(value) -> str:
    return _DELIVERY.get(value, value) if value else "—"


def _promo(promo) -> str:
    if not isinstance(promo, dict):
        return str(promo)
    kind = promo.get("type")
    if kind == "LOAN":
        months = promo.get("displayDuration")
        return f"рассрочка {months} мес" if months else "рассрочка"
    if kind == "BONUS" and promo.get("bonusInCurrency") is not None:
        return f"бонус {_money(promo['bonusInCurrency'])}"
    return str(promo.get("code") or kind or "?")


def _safe_next(target: str | None) -> str:
    # Only paths on this site: '//host' and '/\host' would leave it.
    if not target or not target.startswith("/") or target[1:2] in ("/", "\\"):
        return "/"
    return target


def _chart(offers: list[dict]) -> dict | None:
    """Lowest price over time, as SVG polyline points: among sellers' price stretches,
    or of listing-card stretches, which are already lowest prices."""
    series = db.lowest_prices(
        [Period(o["price"], o["first_seen_at"], o["last_seen_at"]) for o in offers]
    )
    if not series:
        return None
    last_seen = max(o["last_seen_at"] for o in offers)
    if series[-1][0] < last_seen:
        series.append((last_seen, series[-1][1]))
    width, height, pad = 900, 220, 10
    t0, t1 = series[0][0].timestamp(), series[-1][0].timestamp()
    prices = [p for _, p in series]
    low, high = min(prices), max(prices)
    span_t, span_p = (t1 - t0) or 1, (high - low) or 1

    def x(moment):
        return pad + (moment.timestamp() - t0) / span_t * (width - 2 * pad)

    def y(price):
        return height - pad - float((price - low) / span_p) * (height - 2 * pad)

    points, prev = [], None
    for moment, price in series:
        if prev is not None:  # a step: the price held until it changed
            points.append(f"{x(moment):.1f},{y(prev):.1f}")
        points.append(f"{x(moment):.1f},{y(price):.1f}")
        prev = price
    return {
        "points": " ".join(points), "width": width, "height": height,
        "low": low, "high": high, "start": series[0][0], "end": series[-1][0],
    }


def create_app(
    *, database_url: str | None = None, user: str | None = None,
    password_hash: str | None = None, secret: str | None = None,
) -> FastAPI:
    database_url = database_url or os.environ["SHYNBAGA_WEB_DATABASE_URL"]
    user = user or os.environ.get("SHYNBAGA_WEB_USER") or "admin"
    password_hash = password_hash or os.environ["SHYNBAGA_WEB_PASSWORD_HASH"]
    secret = secret or os.environ["SHYNBAGA_WEB_SECRET"]
    if len(secret) < 32:
        raise RuntimeError("SHYNBAGA_WEB_SECRET must be at least 32 characters")

    sessions = auth.Sessions(secret, password_hash)
    throttle = auth.Throttle()
    pool = ConnectionPool(
        database_url, min_size=1, max_size=4, open=False,
        kwargs={"options": "-c default_transaction_read_only=on -c statement_timeout=15000"},
    )

    @asynccontextmanager
    async def lifespan(_app):
        pool.open()
        yield
        pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters |= {"when": _when, "money": _money, "num": _num, "took": _took,
                              "delivery": _delivery, "promo": _promo}
    css = (files("shynbaga.web") / "static" / "app.css").read_text()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        open_paths = ("/login", "/static/app.css", "/robots.txt")
        if request.url.path not in open_paths and not sessions.valid(request.cookies.get(COOKIE)):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            response = RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)
        else:
            response = await call_next(request)
        response.headers.update(_HEADERS)
        return response

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def query_with(request: Request, **changes) -> str:
        params = dict(request.query_params) | {k: str(v) for k, v in changes.items()}
        return "?" + urlencode({k: v for k, v in params.items() if v})

    templates.env.globals["query_with"] = query_with

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n"

    @app.get("/static/app.css")
    def stylesheet():
        return Response(css, media_type="text/css")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return page(request, "login.html", next=_safe_next(next), error=None)

    @app.post("/login")
    def login(
        request: Request, username: str = Form(""), password: str = Form(""),
        next: str = Form("/"),
    ):
        ip = request.client.host if request.client else "?"
        if not throttle.allowed(ip):
            return page(request, "login.html", next=_safe_next(next),
                        error="Слишком много попыток. Подождите 15 минут.")
        # Check the password even for a wrong login, so timing does not tell them apart.
        good_password = auth.verify_password(password, password_hash)
        if not (good_password and username.strip() == user):
            throttle.failed(ip)
            return page(request, "login.html", next=_safe_next(next),
                        error="Неверный логин или пароль.")
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(
            COOKIE, sessions.issue(), max_age=auth.SESSION_SECONDS, httponly=True,
            secure=request.url.scheme == "https", samesite="lax",
        )
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    def products_page(
        request: Request, q: str = "", brand: str = "", price_from: str = "",
        price_to: str = "", only: str = "", category: str = "", sort: str = "popular",
        page_no: int = 1,
    ):
        with pool.connection() as conn:
            rows, total, capped = queries.products(
                conn, q=q, brand=brand, price_from=price_from, price_to=price_to,
                only=only, category=category, sort=sort, page=page_no,
            )
            return page(
                request, "products.html", rows=rows, total=total, capped=capped,
                page_no=max(page_no, 1),
                pages=max(1, -(-total // queries.PAGE_SIZE)),
                totals=queries.totals(conn), f={"q": q, "brand": brand, "price_from": price_from,
                "price_to": price_to, "only": only, "category": category, "sort": sort},
                section="products",
            )

    @app.get("/products/{product_id}", response_class=HTMLResponse)
    def product_page(request: Request, product_id: int):
        with pool.connection() as conn:
            item = queries.product(conn, product_id)
            if item is None:
                return page(request, "missing.html", status=404, what="Товар не найден")
            offers = queries.product_offers(conn, product_id)
            cards = queries.card_prices(conn, product_id)
            return page(
                request, "product.html", p=item, offers=offers, cards=cards,
                card_chart=_chart(cards) if cards else None,
                current=[o for o in offers if o["is_current"]],
                past=[o for o in offers if not o["is_current"]],
                snapshots=queries.product_snapshots(conn, product_id),
                chart=_chart(offers) if offers else None, section="products",
            )

    @app.get("/sellers", response_class=HTMLResponse)
    def sellers_page(
        request: Request, q: str = "", only: str = "", sort: str = "products", page_no: int = 1,
    ):
        with pool.connection() as conn:
            rows, total = queries.sellers(conn, q=q, only=only, sort=sort, page=page_no)
            return page(
                request, "sellers.html", rows=rows, total=total, page_no=max(page_no, 1),
                pages=max(1, -(-total // queries.PAGE_SIZE)),
                f={"q": q, "only": only, "sort": sort}, section="sellers",
            )

    @app.get("/sellers/{seller_id}", response_class=HTMLResponse)
    def seller_page(request: Request, seller_id: int):
        with pool.connection() as conn:
            item = queries.seller(conn, seller_id)
            if item is None:
                return page(request, "missing.html", status=404, what="Продавец не найден")
            profiles, ratings = queries.seller_history(conn, seller_id)
            return page(
                request, "seller.html", s=item, products=queries.seller_products(conn, seller_id),
                profiles=profiles, ratings=ratings, section="sellers",
            )

    @app.get("/categories", response_class=HTMLResponse)
    def categories_page(
        request: Request, q: str = "", only: str = "", sort: str = "popular", page_no: int = 1,
    ):
        with pool.connection() as conn:
            rows, total = queries.categories(conn, q=q, only=only, sort=sort, page=page_no)
            return page(
                request, "categories.html", rows=rows, total=total, page_no=max(page_no, 1),
                pages=max(1, -(-total // queries.PAGE_SIZE)),
                totals=queries.catalog_totals(conn),
                f={"q": q, "only": only, "sort": sort}, section="categories",
            )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        with pool.connection() as conn:
            return page(request, "runs.html", runs=queries.runs(conn), section="runs")

    return app
