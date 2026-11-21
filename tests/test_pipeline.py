"""End to end without Amazon: fake download, SQLite database."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import load_settings
from app.downloader import DownloadResult
from app.exceptions import ConfigurationError, ReportNotReadyError, ReportsNotSavedError
from app.main import PlacementPipeline, RunMode, RunOptions
from app.report_saver import reports_for_run

from .conftest import ENTITY_ID, REPORT_ID_TODAY, REPORT_ID_YESTERDAY, csv_text, line, open_database

#: 07:15 UTC = 12:45 IST = Nov 18 23:15 in Los Angeles (PST).
EVENING_LA = datetime(2026, 11, 19, 7, 15, tzinfo=timezone.utc)
#: 08:15 UTC = 13:45 IST = Nov 19 00:15 in Los Angeles: a new day has begun.
MIDNIGHT_LA = datetime(2026, 11, 19, 8, 15, tzinfo=timezone.utc)
PUBLISHED = "amazon_ad_placement_search_term_today.csv"


class FakeDownloads:
    def __init__(self, settings, contents: dict[str, str]) -> None:
        self.settings = settings
        self.contents = contents
        self.calls: list[str] = []

    def __call__(self, report) -> DownloadResult:
        self.calls.append(report.kind)
        content = self.contents[report.kind]
        if isinstance(content, Exception):
            raise content
        path = self.settings.download_dir / f"20261119T071500Z_{report.report_id[:8]}_placement.csv"
        path.write_text(content, encoding="utf-8")
        return DownloadResult(path=path, suggested_filename="placement.csv", size_bytes=len(content))


def _pipeline(settings, engine, downloads, *, mode=RunMode.LOAD, dry_run=False, reference=EVENING_LA):
    return PlacementPipeline(
        settings,
        RunOptions(mode=mode, dry_run=dry_run, use_lock=False),
        database_factory=lambda: open_database(engine),
        download_fn=downloads,
        reference_time=reference,
    )


def _count(engine) -> int:
    database = open_database(engine)
    if not database.exists():
        return 0
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(database.table)).scalar_one()


def test_hourly_runs_upsert_the_day_without_duplicates(settings, engine):
    first = [line(day="Nov 18, 2026", hour=h) for h in range(3)]
    downloads = FakeDownloads(settings, {"today": csv_text(first)})
    summary = _pipeline(settings, engine, downloads).run()  # creates the table
    assert summary.succeeded and _count(engine) == 3
    assert summary.saved_reports[0].write.inserted == 3

    later = [line(day="Nov 18, 2026", hour=h, clicks=4) for h in range(5)]
    downloads.contents["today"] = csv_text(later)
    write = _pipeline(settings, engine, downloads).run().saved_reports[0].write
    assert (write.inserted, write.updated) == (2, 3)
    assert _count(engine) == 5
    assert open_database(engine).duplicate_keys() == 0
    assert (settings.reports_dir / PUBLISHED).is_file()
    assert not list(settings.download_dir.iterdir())  # archived
    assert len(list(settings.archive_dir.iterdir())) == 1  # same file name both runs


def test_dry_run_writes_nothing(settings, engine):
    downloads = FakeDownloads(settings, {"today": csv_text([line(day="Nov 18, 2026")])})
    _pipeline(settings, engine, downloads, dry_run=True).run()
    assert not open_database(engine).exists()


def test_no_auto_create_refuses(settings, engine):
    settings = dataclasses.replace(settings, auto_create_table=False)
    downloads = FakeDownloads(settings, {"today": csv_text([line(day="Nov 18, 2026")])})
    with pytest.raises(ReportsNotSavedError, match="DB_AUTO_CREATE_TABLE"):
        _pipeline(settings, engine, downloads).run()


def test_save_reports_does_not_touch_mysql(settings, engine):
    downloads = FakeDownloads(settings, {"today": csv_text([line(day="Nov 18, 2026")])})
    pipeline = PlacementPipeline(
        settings, RunOptions(mode=RunMode.SAVE_REPORTS, use_lock=False),
        database_factory=lambda: pytest.fail("MySQL used"), download_fn=downloads, reference_time=EVENING_LA,
    )
    assert pipeline.run().succeeded
    assert (settings.reports_dir / PUBLISHED).is_file()


def test_failed_download_changes_nothing(settings, engine):
    downloads = FakeDownloads(settings, {"today": csv_text([line(day="Nov 18, 2026")])})
    _pipeline(settings, engine, downloads).run()
    downloads.contents["today"] = ReportNotReadyError("still '-'")
    with pytest.raises(ReportsNotSavedError):
        _pipeline(settings, engine, downloads).run()
    assert _count(engine) == 1


def test_conflicting_rows_go_to_failed_and_keep_rows(settings, engine):
    downloads = FakeDownloads(settings, {"today": csv_text([line(day="Nov 18, 2026")])})
    _pipeline(settings, engine, downloads).run()
    downloads.contents["today"] = csv_text([line(day="Nov 18, 2026", imp=1), line(day="Nov 18, 2026", imp=2)])
    with pytest.raises(ReportsNotSavedError, match="DataTransformationError"):
        _pipeline(settings, engine, downloads).run()
    assert _count(engine) == 1
    assert len(list(settings.failed_dir.iterdir())) == 1


def test_yesterday_report_runs_only_after_midnight_la(settings, engine, monkeypatch):
    monkeypatch.setenv("REPORT_ID_YESTERDAY", REPORT_ID_YESTERDAY)
    with_yesterday = load_settings()
    with_yesterday = dataclasses.replace(
        with_yesterday, **{f.name: getattr(settings, f.name) for f in dataclasses.fields(settings)
                          if f.name.endswith("_dir") or f.name == "lock_file"}
    )
    assert [r.kind for r in reports_for_run(with_yesterday, reference=EVENING_LA)] == ["today"]
    assert [r.kind for r in reports_for_run(with_yesterday, reference=MIDNIGHT_LA)] == ["today", "yesterday"]

    downloads = FakeDownloads(with_yesterday, {
        "today": csv_text([line(day="Nov 19, 2026")]),
        "yesterday": csv_text([line(day="Nov 18, 2026", hour=h) for h in range(24)]),
    })
    _pipeline(with_yesterday, engine, downloads, reference=MIDNIGHT_LA).run()
    assert downloads.calls == ["today", "yesterday"]
    assert _count(engine) == 25


def test_header_only_report_keeps_existing_rows(settings, engine):
    downloads = FakeDownloads(settings, {"today": csv_text([line(day="Nov 18, 2026")])})
    _pipeline(settings, engine, downloads).run()
    downloads.contents["today"] = csv_text([])
    assert _pipeline(settings, engine, downloads).run().succeeded
    assert _count(engine) == 1


def test_process_file_and_create_table(settings, engine, tmp_path: Path):
    path = tmp_path / "backfill.csv"
    path.write_text(csv_text([line(day="Jan 01, 2026", placement=p) for p in ("Rest of Search", "Product pages")]),
                    encoding="utf-8")
    PlacementPipeline(
        settings, RunOptions(mode=RunMode.CREATE_TABLE, process_file=path, use_lock=False),
        database_factory=lambda: open_database(engine), reference_time=EVENING_LA,
    ).run()
    assert open_database(engine).exists() and _count(engine) == 0
    PlacementPipeline(
        settings, RunOptions(mode=RunMode.PROCESS_FILE, process_file=path, use_lock=False),
        database_factory=lambda: open_database(engine), reference_time=EVENING_LA,
    ).run()
    assert _count(engine) == 2 and path.is_file()


def test_config_requires_today_report(env, monkeypatch):
    monkeypatch.delenv("REPORT_ID_TODAY")
    with pytest.raises(ConfigurationError, match="placment_search_term_Automation"):
        load_settings()


def test_config_refuses_another_database(env, monkeypatch):
    monkeypatch.setenv("DB_NAME", "powerbidb")
    with pytest.raises(ConfigurationError, match="itrend"):
        load_settings()


def test_config_refuses_replace_mode(env, monkeypatch):
    monkeypatch.setenv("DB_LOAD_MODE", "replace")
    with pytest.raises(ConfigurationError, match="upsert"):
        load_settings()


def test_history_url_is_the_us_entity(settings):
    assert settings.load_mode == "upsert"
    assert settings.history_url(REPORT_ID_TODAY) == (
        f"https://advertising.amazon.com/reporting/history?entityId={ENTITY_ID}&reportId={REPORT_ID_TODAY}"
    )
