"""Integration fixtures intentionally require a real, isolated MySQL database."""
import os
import re

import pytest

from backend import db


@pytest.fixture
def mysql_database(tmp_path, monkeypatch):
    name = os.environ.get("MK_DB_NAME", "")
    if os.getenv("MK_TEST_ALLOW_RESET") != "1" or not re.fullmatch(r"[a-zA-Z0-9_]+_test", name):
        pytest.fail("Integration tests require dedicated MK_DB_NAME ending _test and MK_TEST_ALLOW_RESET=1; production is never reset")
    monkeypatch.setenv("MK_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("MK_DEMO_PASSWORD", raising=False)
    monkeypatch.setenv("MK_DEMO", "0")
    db.init_schema()
    with db.connect() as con:
        con.execute("SET FOREIGN_KEY_CHECKS=0")
        try:
            for table in reversed(db.TABLES):
                con.execute(f"TRUNCATE TABLE `{table}`")
        finally:
            con.execute("SET FOREIGN_KEY_CHECKS=1")
    db.init_db()
    return name


@pytest.fixture(autouse=True)
def disable_correlation_scheduler_in_tests(monkeypatch):
    from backend import correlation
    monkeypatch.setattr(correlation, "start_scheduler", lambda: None)
    monkeypatch.setattr(correlation, "stop_scheduler", lambda: None)
