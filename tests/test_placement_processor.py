"""CSV -> rows: column mapping, natural key, duplicates, dates."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.exceptions import CSVValidationError, DataTransformationError, DateMismatchError
from app.placement_processor import (
    infer_kind,
    key_columns,
    read_placement_report,
    row_key,
    snake_case,
    transform_placement_report,
)

from .conftest import csv_text, line

LOADED = datetime(2026, 11, 20, 9, 15, tzinfo=timezone.utc)


def _report(tmp_path: Path, rows: list[str], *, today: date | None = date(2026, 11, 19), **kwargs):
    path = tmp_path / "placement.csv"
    path.write_text(csv_text(rows), encoding="utf-8")
    return transform_placement_report(
        read_placement_report(path), report_id="r1", loaded_at=LOADED, today=today, max_age_days=95, **kwargs
    )


def test_key_includes_placement_and_search_term(tmp_path):
    report = _report(tmp_path, [line()])
    assert report.key == (
        "report_date", "hour", "campaign_country", "campaign_id", "ad_group_id", "target_id", "search_term",
        "placement",
    )


def test_values_are_converted(tmp_path):
    frame = _report(tmp_path, [line(cost="1.25000", ctr="12.5000%", clicks=3)]).frame
    row = frame.iloc[0]
    assert row["report_date"] == date(2026, 11, 19)
    assert row["campaign_id"] == 141242384338773 and row["ad_group_id"] == 188245137651693
    assert row["total_cost"] == pytest.approx(1.25) and row["ctr"] == pytest.approx(12.5)
    assert row["clicks"] == 3 and row["placement"] == "Top of Search on-Amazon"
    assert row["long_term_sales"] == pytest.approx(1.5)  # unknown column, inferred as a number
    assert row["report_id"] == "r1" and len(row["row_key"]) == 64


def test_same_term_in_different_placements_is_two_rows(tmp_path):
    report = _report(tmp_path, [line(placement="Top of Search on-Amazon"), line(placement="Rest of Search")])
    assert report.row_count == 2 and report.frame["row_key"].nunique() == 2


def test_row_repeating_the_key_with_other_values_is_refused(tmp_path):
    with pytest.raises(DataTransformationError, match="differ in: Impressions"):
        _report(tmp_path, [line(imp=10), line(imp=99)])


def test_exact_duplicate_rows_are_dropped(tmp_path):
    report = _report(tmp_path, [line(), line(), line(hour=3)])
    assert report.row_count == 2


def test_key_is_stable_across_runs(tmp_path):
    first = _report(tmp_path, [line(clicks=1)]).frame["row_key"].iloc[0]
    later = _report(tmp_path, [line(clicks=7, sales="12.00")]).frame["row_key"].iloc[0]
    assert first == later  # same row, new metrics -> upsert updates it


def test_dates_outside_the_window_are_refused(tmp_path):
    with pytest.raises(DateMismatchError):
        _report(tmp_path, [line(day="Jan 01, 2026")])
    assert _report(tmp_path, [line(day="Jan 01, 2026")], today=None).row_count == 1  # backfill


def test_existing_table_columns_decide_what_is_stored(tmp_path):
    table = {name: kind for name, kind in [
        ("report_date", "date"), ("hour", "hour"), ("campaign_country", "text"), ("campaign_id", "id?"),
        ("ad_group_id", "id?"), ("target_id", "id?"), ("search_term", "text"), ("placement", "text"),
        ("clicks", "count"), ("new_metric", "number"),
    ]}
    report = _report(tmp_path, [line()], table_columns=table)
    assert list(report.frame.columns)[:10] == list(table)
    assert "impressions" not in report.frame.columns  # not in the table: skipped
    assert report.frame["new_metric"].isna().all()  # not in the file: NULL


def test_table_with_a_date_column_gets_the_report_date(tmp_path):
    table = {"date": "date", "hour": "hour", "campaign_country": "text", "campaign_id": "id?",
             "ad_group_id": "id?", "target_id": "id?", "search_term": "text", "placement": "text"}
    report = _report(tmp_path, [line(), line(day="Nov 18, 2026")], table_columns=table)
    assert report.key[0] == "date" and "report_date" not in report.frame.columns
    assert set(report.frame["date"]) == {date(2026, 11, 18), date(2026, 11, 19)}
    assert report.frame["row_key"].nunique() == 2  # the date is part of the key
    assert report.dates == (date(2026, 11, 18), date(2026, 11, 19))


def test_total_cost_is_stored_in_a_spend_column(tmp_path):
    table = {"date": "date", "hour": "hour", "campaign_country": "text", "campaign_id": "id?",
             "ad_group_id": "id?", "target_id": "id?", "search_term": "text", "placement": "text",
             "spend": "metric"}
    frame = _report(tmp_path, [line(cost="1.25000")], table_columns=table).frame
    assert frame["spend"].iloc[0] == pytest.approx(1.25) and "total_cost" not in frame.columns


def test_rows_before_load_from_are_skipped(tmp_path):
    rows = [line(day="Nov 17, 2026"), line(day="Nov 18, 2026"), line(day="Nov 19, 2026")]
    report = _report(tmp_path, rows, load_from=date(2026, 11, 18))
    assert report.dates == (date(2026, 11, 18), date(2026, 11, 19))
    assert _report(tmp_path, rows[:1], load_from=date(2026, 11, 18)).row_count == 0


def test_key_includes_advertised_product():
    assert "advertised_product_id" in key_columns(["date", "campaign_id", "advertised_product_id", "search_term"])


def test_missing_key_column_is_refused(tmp_path):
    table = {"report_date": "date", "campaign_id": "id?", "placement": "text", "site_or_platform": "text"}
    with pytest.raises(CSVValidationError, match="site_or_platform"):
        _report(tmp_path, [line()], table_columns=table)


def test_key_falls_back_to_names_without_ids():
    assert key_columns(["report_date", "campaign_name", "ad_group_name", "targeting", "search_term",
                        "placement_classification", "clicks"]) == (
        "report_date", "campaign_name", "ad_group_name", "targeting", "search_term", "placement_classification",
    )


def test_row_key_treats_null_as_empty():
    assert row_key((date(2026, 11, 19), None, "x")) == row_key((date(2026, 11, 19), pd.NA, "x"))
    assert row_key((date(2026, 11, 19), "Rain poncho")) != row_key((date(2026, 11, 19), "rain poncho"))


def test_snake_case_and_kind_inference():
    assert snake_case("Top-of-search IS (%)") == "top_of_search_is"
    assert snake_case("ID") == "amazon_id"
    assert infer_kind(pd.Series(["$1,204.55", "12.5%", ""])) == "number"
    assert infer_kind(pd.Series(['="123"', '="456"'])) == "id?"
    assert infer_kind(pd.Series(["Top of Search", "Rest"])) == "text"


def test_header_only_file(tmp_path):
    assert _report(tmp_path, []).row_count == 0
