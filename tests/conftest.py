import os
from urllib.parse import urlsplit

import pytest

from shynbaga import db


@pytest.fixture
def conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    if not urlsplit(url).path.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a *_test database: the suite wipes it")
    with db.connect(url) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.commit()
        db.migrate(connection)
        yield connection
        connection.rollback()
