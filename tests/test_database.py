"""Table creation from a report, and upserts on row_key (SQLite)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, func, select

from app.database import PlacementDatabase, mysql_ddl, table_for_report
from app.exceptions import DatabaseError
from app.placement_processor import read_placement_report, transform_placement_report

from .conftest import csv_text, line, open_database

LOADED = datetime(2026, 11, 20, 9, 15, tzinfo=timezone.utc)


def _report(tmp_path: Path, rows: list[str], database: PlacementDatabase | None = None):
    path = tmp_path / "placement.csv"
    path.write_text(csv_text(rows), encoding="utf-8")
    columns = database.report_columns() if database is not None and database.exists() else None
    return transform_placement_report(
        read_placement_report(path), report_id="r1", loaded_at=LOADED, today=None, max_age_days=95,
        table_columns=columns,
    )


def _load(database: PlacementDatabase, report, mode: str = "upsert"):
    if not database.exists():
        database.create_table(report)
        database.validate_table()
    return database.write(mode, report.slices, database.lazy_records(report.frame))


def _rows(database: PlacementDatabase) -> list[tuple]:
    t = database.table
    with database.engine.connect() as conn:
        return sorted(conn.execute(select(t.c.hour, t.c.placement, t.c.clicks, t.c.sales)))


def test_only_the_placement_table_is_allowed(engine):
    with pytest.raises(DatabaseError, match="search_term_placement_data"):
        PlacementDatabase(engine, "searchtermreport")


def test_first_load_creates_the_table_with_a_unique_key(engine, tmp_path):
    database = open_database(engine)
    report = _report(tmp_path, [line(hour=h) for h in range(3)])
    result = _load(database, report)
    assert (result.inserted, result.updated) == (3, 0)
    assert database.has_unique_row_key()
    assert "placement" in database.report_columns() and "long_term_sales" in database.report_columns()


def test_hourly_reruns_update_instead_of_duplicating(engine, tmp_path):
    database = open_database(engine)
    _load(database, _report(tmp_path, [line(hour=0, clicks=1), line(hour=1, clicks=1)]))
    later = _report(tmp_path, [line(hour=0, clicks=5, sales="9.99"), line(hour=1, clicks=1), line(hour=2)], database)
    result = _load(database, later)
    assert (result.inserted, result.updated) == (1, 2)
    rows = [(h, p, c, float(s)) for h, p, c, s in _rows(database)]
    assert rows == [(0, "Top of Search on-Amazon", 5, 9.99),
                    (1, "Top of Search on-Amazon", 1, 0.0), (2, "Top of Search on-Amazon", 1, 0.0)]
    assert database.duplicate_keys() == 0


def test_same_file_twice_changes_nothing(engine, tmp_path):
    database = open_database(engine)
    rows = [line(placement=p) for p in ("Top of Search on-Amazon", "Rest of Search", "Product pages")]
    _load(database, _report(tmp_path, rows))
    _load(database, _report(tmp_path, rows, database))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(database.table)).scalar_one() == 3


def test_insert_mode_keeps_existing_values(engine, tmp_path):
    database = open_database(engine)
    _load(database, _report(tmp_path, [line(clicks=1)]))
    result = _load(database, _report(tmp_path, [line(clicks=9)], database), mode="insert")
    assert (result.inserted, result.skipped) == (0, 1)
    assert _rows(database)[0][2] == 1


def test_table_without_unique_row_key_is_refused(engine):
    metadata = MetaData()
    from sqlalchemy import BigInteger, Column, DateTime, String

    Table("search_term_placement_data", metadata, Column("id", BigInteger, primary_key=True),
          Column("report_id", String(36)), Column("loaded_at", DateTime), Column("row_key", String(64)))
    metadata.create_all(engine)
    with pytest.raises(DatabaseError, match="UNIQUE"):
        PlacementDatabase(engine).validate_table()


def test_long_text_is_shortened_not_rejected(engine, tmp_path):
    database = open_database(engine)
    _load(database, _report(tmp_path, [line()]))
    report = _report(tmp_path, [line(term="x" * 600)], database)
    _load(database, report)
    t = database.table
    with engine.connect() as conn:
        lengths = sorted(len(v or "") for (v,) in conn.execute(select(t.c.search_term)))
    assert lengths == [11, 512]


def test_mysql_ddl(tmp_path):
    ddl = mysql_ddl(table_for_report(_report(tmp_path, [line()])))
    assert "CREATE TABLE search_term_placement_data" in ddl
    assert "row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL" in ddl
    assert "CREATE UNIQUE INDEX uk_row_key ON search_term_placement_data (row_key)" in ddl
    assert "report_date DATE NOT NULL" in ddl and "placement VARCHAR(512)" in ddl
    assert "ENGINE=InnoDB" in ddl and "CHARSET=utf8mb4" in ddl
    assert "search_term(100)" in ddl
