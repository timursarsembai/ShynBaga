import json
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from shynbaga import db, notify
from shynbaga.collector import RunResult, alert_text
from shynbaga.marketplaces import Blocked, OfferList
from tests.test_collector import REGION, T0, FakeMarket, offer, run


def test_blocked_run_alerts_with_what_was_saved():
    text = alert_text("kaspi", "Smartphones", RunResult("blocked", 40, 3, "HTTP 429 on /x"))
    assert "заблокирован" in text
    assert "HTTP 429" in text
    assert "40" in text


def test_failed_run_alerts():
    assert "упал" in alert_text("kaspi", "Smartphones", RunResult("failed", error="HTTP 500"))


def test_clean_run_is_silent():
    assert alert_text("kaspi", "Smartphones", RunResult("completed", 1000)) is None


def test_few_failed_products_are_silent():
    assert alert_text("kaspi", "Smartphones", RunResult("partial", 95, 5)) is None


def test_many_failed_products_alert():
    assert "Ошибок: 20 из 100" in alert_text("k", "c", RunResult("partial", 80, 20))


def test_cut_listing_alerts_even_without_failed_products():
    text = alert_text("k", "c", RunResult("partial", 24, 0, "HTTP 400 on /x"))
    assert "HTTP 400" in text


def test_real_blocked_run_produces_an_alert(conn):
    result = run(conn, FakeMarket({str(i): Blocked("HTTP 429") for i in range(5)}))
    assert "заблокирован" in alert_text("fake", "Cat", result)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def configure(monkeypatch):
    monkeypatch.setenv("SHYNBAGA_TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("SHYNBAGA_TELEGRAM_CHAT_ID", "42")


def test_send_posts_to_the_chat(monkeypatch):
    configure(monkeypatch)
    seen = {}

    def urlopen(request, timeout):
        seen["request"] = request
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert notify.send("hello") is True
    assert "123:secret" in seen["request"].full_url
    assert json.loads(seen["request"].data) == {"chat_id": "42", "text": "hello"}


def test_send_without_configuration_does_not_call_out(monkeypatch):
    monkeypatch.delenv("SHYNBAGA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SHYNBAGA_TELEGRAM_CHAT_ID", raising=False)

    def urlopen(request, timeout):
        pytest.fail("must not send")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert notify.send("hello") is False


def test_failed_send_never_breaks_the_run_or_logs_the_token(monkeypatch, caplog):
    configure(monkeypatch)

    def urlopen(request, timeout):
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert notify.send("hello") is False
    assert "secret" not in caplog.text


def test_last_run_ignores_interrupted_runs(conn):
    assert db.last_run_started_at(conn, "fake", "Cat", REGION) is None
    run(conn, FakeMarket({"1": OfferList([offer("a", 100)])}))
    assert db.last_run_started_at(conn, "fake", "Cat", REGION) == T0
    conn.execute(
        """INSERT INTO crawl_runs (marketplace, category, region, started_at, finished_at, status)
           VALUES ('fake', 'Cat', %s, %s, %s, 'interrupted')""",
        (REGION, T0 + timedelta(days=1), T0 + timedelta(days=1)),
    )
    assert db.last_run_started_at(conn, "fake", "Cat", REGION) == T0
