"""Shared fixtures.  Nothing here touches Amazon or a real MySQL server.

Database tests run against in-memory SQLite.  The table is created by the
code under test from the report's columns, exactly as the first real run does
on itrend, so creation, upserts and rollbacks are exercised for real.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings, load_settings
from app.database import PlacementDatabase

REPORT_ID_TODAY = "79745ecf-57c1-48de-a3f7-51c1feab3658"
REPORT_ID_YESTERDAY = "15a08763-73ca-4172-8029-521d5ed7d090"
ENTITY_ID = "ENTITY1YST5GXMAIR36"

BASE_ENV = {
    "DB_HOST": "localhost",
    "DB_NAME": "itrend",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
    "AMAZON_ACCOUNT_TIMEZONE": "America/Los_Angeles",
    "AMAZON_ENTITY_ID": ENTITY_ID,
    "REPORT_ID_TODAY": REPORT_ID_TODAY,
}

#: A placement search term export.  "Long-term sales" is not in KNOWN_COLUMNS,
#: so it exercises the inferred-column path.
COLUMNS = [
    "Date", "Hour", "Campaign ID", "Campaign name", "Campaign country", "Ad group ID", "Ad group name",
    "Target ID", "Targeting", "Targeting match type", "Search term", "Placement", "Impressions", "Clicks",
    "Total cost", "Purchases", "Sales", "Units sold", "CPC", "CTR", "Long-term sales",
]
HEADER = ",".join(COLUMNS)


def line(day: str = "Nov 19, 2026", hour: int = 2, *, placement: str = "Top of Search on-Amazon",
         term: str = "rain poncho", target: str = "22559448179860", campaign: str = "141242384338773",
         ad_group: str = "188245137651693", country: str = "US", imp: int = 10, clicks: int = 1,
         cost: str = "0.45000", purchases: int = 0, sales: str = "0.00", units: int = 0,
         cpc: str = "0.45000", ctr: str = "10.0000%", long_term: str = "1.50") -> str:
    """One data row, formatted the way Amazon writes its CSV exports."""
    return (
        f'"{day}",{hour},"=""{campaign}""",rainponcho_Phrase,{country},"=""{ad_group}""",Ad Group 1,'
        f"{target},disposable rain poncho,PHRASE,{term},{placement},{imp},{clicks},{cost},{purchases},"
        f"{sales},{units},{cpc},{ctr},{long_term}"
    )


def csv_text(rows: list[str]) -> str:
    return "﻿" + "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ignore the developer's .env and exported variables."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *_a, **_k: False)
    for key in list(os.environ):
        if key.startswith(("AMAZON_", "DB_", "REPORT_", "CSV_", "DOWNLOAD_", "REPORTS_", "MAX_REPORT",
                           "ARCHIVE_", "FAILED_", "LOG_", "SCREENSHOT_", "SESSION_", "LOCK_", "YESTERDAY_",
                           "HEADLESS", "BROWSER_", "MAX_RETRIES", "RETRY_", "FILE_RETENTION")):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, name in (
        ("DOWNLOAD_DIR", "downloads"),
        ("REPORTS_DIR", "reports"),
        ("ARCHIVE_DIR", "archive"),
        ("FAILED_DIR", "failed"),
        ("SCREENSHOT_DIR", "screenshots"),
        ("LOG_DIR", "logs"),
        ("SESSION_DIR", "session"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "run.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    loaded = load_settings()
    loaded.ensure_directories()
    return loaded


@pytest.fixture(autouse=True)
def no_ddl_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Table creation in tests writes its DDL to tmp, never to sql/."""
    monkeypatch.setattr("app.database.DDL_FILE", tmp_path / "create.sql")
    monkeypatch.setattr("app.main.DDL_FILE", tmp_path / "create.sql")


@pytest.fixture
def engine():
    """An empty SQLite database: the code under test creates the table."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


def open_database(engine) -> PlacementDatabase:
    database = PlacementDatabase(engine, chunk_size=3)
    if database.exists():
        database.validate_table()
    return database
