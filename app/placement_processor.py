"""Placement search term CSV -> rows for ``itrend.search_term_placement_data``.

The saved Amazon Ads report (``placment_search_term_Automation``) is read by
its header, so the job keeps working whichever columns the report is set up
with:

* a known Amazon column gets a fixed database name and type
  (:data:`KNOWN_COLUMNS`); any other column is named in snake_case and typed
  from its values (numbers -> DECIMAL, otherwise text);
* ``="123"`` IDs become exact 64-bit integers (never through float);
* money, counts and ``12.5%`` become numbers (percentages stay percentages);
* blank text becomes NULL;
* ``report_date`` is the parsed Date; ``report_id`` / ``loaded_at`` record which
  saved report and which run produced the row;
* ``row_key`` is the SHA-256 of the row's **natural key** (:func:`key_columns`):
  date, hour, marketplace, campaign, ad group, target, search term and every
  placement column the report has.  It is UNIQUE in the table, so an upsert
  updates a row instead of adding it twice -- re-running a report, or a later
  hour of the same day, never creates duplicates.

Once the table exists, its columns are the contract: a column the CSV adds
later is logged and skipped, a column the CSV drops is stored as NULL, and a
natural-key column the CSV drops stops the run (a different key would mean
duplicates).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import ROW_KEY_COLUMN
from .exceptions import CSVValidationError, DataTransformationError, DateMismatchError
from .logging_config import get_logger
from .report_csv import check_field_counts, detect_header, numeric, parse_report_dates, read_text

logger = get_logger("placement")

#: Column kinds.  ``id`` = required ID, ``id?`` = optional ID, ``text!`` = text
#: that may not be blank, ``count`` = whole number (blank = 0), ``metric`` =
#: decimal (blank = 0), ``number`` = decimal (blank = NULL).
KINDS = frozenset({"date", "hour", "id", "id?", "text", "text!", "count", "metric", "number"})

#: Amazon column -> (database column, kind).
KNOWN_COLUMNS: dict[str, tuple[str, str]] = {
    "Date": ("report_date", "date"),
    "Hour": ("hour", "hour"),
    "Campaign ID": ("campaign_id", "id?"),
    "Campaign name": ("campaign_name", "text"),
    "Campaign type": ("campaign_type", "text"),
    "Campaign status": ("campaign_status", "text"),
    "Campaign budget amount": ("campaign_budget_amount", "number"),
    "Campaign budget type": ("campaign_budget_type", "text"),
    "Campaign delivery status": ("campaign_delivery_status", "text"),
    "Campaign country": ("campaign_country", "text"),
    "Campaign bidding strategy": ("campaign_bidding_strategy", "text"),
    "Budget currency": ("budget_currency", "text"),
    "Ad product": ("ad_product", "text"),
    "Ad group ID": ("ad_group_id", "id?"),
    "Ad group name": ("ad_group_name", "text"),
    "Portfolio ID": ("portfolio_id", "id?"),
    "Portfolio name": ("portfolio_name", "text"),
    "Target ID": ("target_id", "id?"),
    "Target bid": ("target_bid", "number"),
    "Target type": ("target_type", "text"),
    "Target status": ("target_status", "text"),
    "Targeting": ("targeting", "text"),
    "Targeting match type": ("targeting_match_type", "text"),
    "Match type": ("match_type", "text"),
    "Search term": ("search_term", "text"),
    "Customer search term": ("search_term", "text"),
    "Placement": ("placement", "text"),
    "Placement type": ("placement_type", "text"),
    "Placement classification": ("placement_classification", "text"),
    "Site or platform": ("site_or_platform", "text"),
    "Country": ("country", "text"),
    "Country code": ("country_code", "text"),
    "Impressions": ("impressions", "count"),
    "Clicks": ("clicks", "count"),
    "Total cost": ("total_cost", "metric"),
    "Spend": ("spend", "metric"),
    "Purchases": ("purchases", "count"),
    "Purchases (multi-touch)": ("purchases_multi_touch", "metric"),
    "Sales": ("sales", "metric"),
    "Sales (multi-touch)": ("sales_multi_touch", "metric"),
    "Units sold": ("units_sold", "count"),
    "Units sold (multi-touch)": ("units_sold_multi_touch", "metric"),
    "CPC": ("cpc", "number"),
    "CTR": ("ctr", "number"),
    "ACOS": ("acos", "number"),
    "ROAS": ("roas", "number"),
    "Top-of-search impression share": ("top_of_search_impression_share", "number"),
}

#: Columns the loader adds after the report's own columns.
META_COLUMNS: tuple[str, ...] = ("report_id", "loaded_at", ROW_KEY_COLUMN)
#: Names a report column may not take (they belong to the table itself).
RESERVED = frozenset({"id", *META_COLUMNS})

DATE_COLUMN = "report_date"
#: Other names an existing table may use for the report date (an older loader
#: created ``itrend.search_term_placement_data`` with ``date``).
DATE_ALIASES: tuple[str, ...] = ("date",)
#: Report column -> names an existing table may store it under instead.
#: Used only when the table lacks the report's own name and the file does not
#: also have a column with the alias name.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    DATE_COLUMN: DATE_ALIASES,
    "total_cost": ("spend",),
}

#: Natural-key columns, in key order, when present.  A missing ID falls back
#: to the name columns that identify the same thing.
_KEY_ORDER: tuple[str, ...] = (
    "report_date", "hour", "campaign_country", "country_code",
    "campaign_id", "ad_group_id", "target_id", "advertised_product_id", "search_term",
)
_KEY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "report_date": DATE_ALIASES,
    "campaign_id": ("campaign_name",),
    "ad_group_id": ("ad_group_name",),
    "target_id": ("targeting", "targeting_match_type", "match_type"),
}


def is_placement_column(name: str) -> bool:
    return "placement" in name or name == "site_or_platform"


def key_columns(columns: Iterable[str]) -> tuple[str, ...]:
    """The natural key for a table / report with these columns.

    Date, hour, marketplace, campaign, ad group, target, search term, then
    every placement column -- each only when present.  The same column set
    always gives the same key, so it is stable for a table's lifetime.
    """
    present = list(dict.fromkeys(columns))
    have = set(present)
    key: list[str] = []
    for column in _KEY_ORDER:
        if column in have:
            key.append(column)
        elif column in _KEY_FALLBACKS:
            key.extend(c for c in _KEY_FALLBACKS[column] if c in have)
    key.extend(c for c in present if is_placement_column(c) and c not in key)
    return tuple(key)


def snake_case(header: str) -> str:
    """``"Top-of-search IS (%)"`` -> ``top_of_search_is``; at most 64 characters."""
    name = re.sub(r"[^0-9a-z]+", "_", header.strip().lower()).strip("_")
    if not name:
        name = "column"
    if name[0].isdigit():
        name = f"c_{name}"
    if name in RESERVED:
        name = f"amazon_{name}"
    return name[:64]


def row_key(values: tuple[object, ...]) -> str:
    """SHA-256 hex of one row's natural-key values joined by ``|`` (NULL = '').

    Blank text is always stored as NULL, never '', so the two cannot collide.
    Unlike a UNIQUE index on the raw columns, the hash treats NULLs as equal
    and compares search terms exactly (the table's collation ignores case).
    """
    parts = []
    for value in values:
        if value is None or value is pd.NA or (isinstance(value, float) and value != value):
            parts.append("")
        elif isinstance(value, date):
            parts.append(value.isoformat())
        else:
            parts.append(str(value))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ColumnSpec:
    source: str  #: Amazon header ("" for a table column the file does not have)
    name: str  #: database column
    kind: str


@dataclass(frozen=True)
class PlacementReport:
    frame: pd.DataFrame
    #: The report's columns, in file order (no meta columns).
    columns: tuple[ColumnSpec, ...]
    key: tuple[str, ...]
    #: (report_date, marketplace or "-") pairs the file contains -- for the logs.
    slices: tuple[tuple[date, str], ...]
    #: Longest value of every text column, for sizing a new table.
    text_lengths: Mapping[str, int]

    @property
    def row_count(self) -> int:
        return len(self.frame)

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(sorted({d for d, _ in self.slices}))

    def describe(self) -> str:
        if not self.slices:
            return "no rows"
        per_date: dict[date, list[str]] = {}
        for day, country in self.slices:
            per_date.setdefault(day, []).append(country)
        return "; ".join(
            f"{d.isoformat()}" + (f" [{', '.join(sorted(c))}]" if c != ["-"] else "")
            for d, c in sorted(per_date.items())
        )


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_placement_report(path: Path) -> pd.DataFrame:
    """The CSV as strings, header located, every row's field count checked.

    Raises:
        CSVValidationError: Unreadable, no header, malformed rows, or no Date column.
    """
    content, encoding = read_text(path)
    header_row, delimiter = detect_header(content)
    check_field_counts(content, header_row, delimiter, path)
    try:
        frame = pd.read_csv(
            path,
            encoding=encoding,
            sep=delimiter,
            skiprows=header_row,
            dtype=str,
            keep_default_na=False,
            engine="c",
            low_memory=False,
        )
    except Exception as exc:
        raise CSVValidationError(f"Could not read {path.name}: {exc}") from exc
    frame.columns = [str(c).strip().lstrip("﻿") for c in frame.columns]
    if "Date" not in frame.columns:
        raise CSVValidationError(
            f"{path.name} has no 'Date' column. Add Date to the saved report's columns in Amazon Ads."
        )
    if not {"Campaign ID", "Campaign name"} & set(frame.columns):
        raise CSVValidationError(f"{path.name} has neither 'Campaign ID' nor 'Campaign name'.")
    blank = frame.apply(lambda col: col.str.strip() == "").all(axis=1)
    if bool(blank.any()):
        frame = frame.loc[~blank]
    logger.info("Read %s: %d data row(s), %d column(s)", path.name, len(frame), len(frame.columns))
    return frame.reset_index(drop=True)


# ---------------------------------------------------------------------------
# column mapping
# ---------------------------------------------------------------------------


_NUMBERISH = re.compile(r"^\(?-?[$€£₹¥]?\s?-?[\d,]*\.?\d+\s?%?\)?$")
_ID_LIKE = re.compile(r'^="?-?\d+"?$')


def infer_kind(series: pd.Series) -> str:
    """Kind of a column Amazon added that :data:`KNOWN_COLUMNS` does not list."""
    values = series.astype(str).str.strip()
    values = values[(values != "") & (values != "-") & (values != "--")]
    if values.empty:
        return "text"
    if bool(values.str.match(_ID_LIKE).all()) and bool(values.str.startswith("=").any()):
        return "id?"
    if bool(values.str.match(_NUMBERISH).all()):
        return "number"
    return "text"


def map_columns(headers: Iterable[str], frame: pd.DataFrame | None = None) -> list[ColumnSpec]:
    """Amazon headers -> database columns, in file order.

    Raises:
        CSVValidationError: Two headers would land in the same database column.
    """
    specs: list[ColumnSpec] = []
    seen: dict[str, str] = {}
    for header in headers:
        if header in KNOWN_COLUMNS:
            name, kind = KNOWN_COLUMNS[header]
        else:
            name = snake_case(header)
            kind = infer_kind(frame[header]) if frame is not None else "text"
        if name in seen:
            raise CSVValidationError(
                f"Columns {seen[name]!r} and {header!r} would both be stored as {name!r}; "
                "remove one of them from the saved report."
            )
        seen[name] = header
        specs.append(ColumnSpec(header, name, kind))
    return specs


# ---------------------------------------------------------------------------
# value conversion
# ---------------------------------------------------------------------------


_ID = re.compile(r"^-?\d+$")
_INT64_MAX = 2**63 - 1
_PLAIN_NUMBER = re.compile(r"^-?(\d+\.?\d*|\.\d+)$")


def _ids(series: pd.Series, column: str, *, required: bool) -> pd.Series:
    """``="123"`` / ``123`` / ``-1`` / blank -> nullable Int64, digit by digit."""
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace('="', "", regex=False)
        .str.replace('"', "", regex=False)
        .str.lstrip("=")
        .str.strip()
    )
    blank = cleaned.isin(["", "-", "--"])
    if required and bool(blank.any()):
        raise DataTransformationError(f"{int(blank.sum())} row(s) have no {column}")
    bad = ~blank & ~cleaned.str.match(_ID)
    if bool(bad.any()):
        raise DataTransformationError(
            f"{int(bad.sum())} {column} value(s) are not whole numbers, e.g. {series[bad].head(3).tolist()}"
        )
    values = [None if b else int(v) for v, b in zip(cleaned, blank)]
    if any(v is not None and abs(v) > _INT64_MAX for v in values):
        raise DataTransformationError(f"A {column} value exceeds the 64-bit range")
    return pd.Series(pd.array(values, dtype="Int64"), index=series.index)


def _text(series: pd.Series) -> pd.Series:
    """Strip; only a blank cell is NULL.  "none", "na" or "-" are real search terms."""
    stripped = series.astype("string").str.strip()
    return stripped.mask(stripped == "", pd.NA)


def _numbers(series: pd.Series, column: str) -> pd.Series:
    """Fast path for Amazon's plain ``12.34000`` / ``0.3053%``; anything else
    (currency signs, thousands separators, ``-``) goes through
    :func:`app.report_csv.numeric`."""
    cleaned = series.astype(str).str.strip().str.removesuffix("%")
    blank = cleaned == ""
    if bool(cleaned[~blank].str.match(_PLAIN_NUMBER).all()):
        return pd.to_numeric(cleaned.mask(blank, None), errors="raise").astype("Float64")
    return numeric(series, column)


def _hours(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.strip()
    bad = ~cleaned.str.fullmatch(r"\d{1,2}")
    if not bad.any():
        values = cleaned.astype(int)
        bad = (values < 0) | (values > 23)
        if not bad.any():
            return values.astype("Int64")
    raise DataTransformationError(
        f"{int(bad.sum())} Hour value(s) are not 0-23, e.g. {series[bad].head(3).tolist()}"
    )


def _convert(series: pd.Series, spec: ColumnSpec, csv_date_format: str | None) -> pd.Series:
    kind, source = spec.kind, spec.source
    if kind == "date":
        return parse_report_dates(series, csv_date_format)
    if kind in ("id", "id?"):
        return _ids(series, source, required=kind == "id")
    if kind == "hour":
        return _hours(series)
    if kind == "count":
        values = _numbers(series, source).fillna(0)
        if bool((values != values.round()).any()):
            raise DataTransformationError(f"{source} has fractional values")
        return values.round().astype("Int64")
    if kind == "metric":
        return _numbers(series, source).fillna(0.0)
    if kind == "number":
        return _numbers(series, source)
    values = _text(series)
    if kind == "text!" and bool(values.isna().any()):
        raise DataTransformationError(f"{int(values.isna().sum())} row(s) have no {source}")
    return values


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------


def transform_placement_report(
    raw: pd.DataFrame,
    *,
    report_id: str,
    loaded_at: datetime,
    today: date | None,
    max_age_days: int,
    csv_date_format: str | None = None,
    table_columns: Mapping[str, str] | None = None,
    load_from: date | None = None,
) -> PlacementReport:
    """Validate and convert the raw CSV into table rows.

    Args:
        today: "Today" in the calendar timezone; no row may be dated after
            today + 1 or before today - max_age_days.  None skips the check
            (``--process-file`` backfills).
        table_columns: The existing table's report columns (name -> kind), in
            table order.  None when the table does not exist yet: then every
            column of the file is kept.
        load_from: Rows dated before this are dropped (``LOAD_FROM_DATE``).

    Raises:
        CSVValidationError: A natural-key column of the table is not in the file.
        DataTransformationError: A value cannot be converted, or a row repeats.
        DateMismatchError: A row's date is outside the allowed window.
    """
    specs = map_columns(raw.columns, raw)
    date_column = DATE_COLUMN
    if table_columns is not None:
        # The table may store a report column under another name (``date``, ``spend``).
        in_file = {s.name for s in specs}
        renames: dict[str, str] = {}
        for own, aliases in COLUMN_ALIASES.items():
            if own in in_file and own not in table_columns:
                alias = next((a for a in aliases if a in table_columns and a not in in_file), None)
                if alias:
                    renames[own] = alias
        if renames:
            logger.info("Stored under the table's names: %s",
                        ", ".join(f"{a} -> {b}" for a, b in renames.items()))
            specs = [ColumnSpec(s.source, renames.get(s.name, s.name), s.kind) for s in specs]
            date_column = renames.get(DATE_COLUMN, DATE_COLUMN)
    if table_columns is not None:
        by_name = {s.name: s for s in specs}
        skipped = [s.source for s in specs if s.name not in table_columns]
        if skipped:
            logger.warning(
                "Column(s) not in the table, not stored: %s (add them to the table to keep them)",
                ", ".join(skipped),
            )
        key = key_columns(table_columns)
        missing_key = [c for c in key if c not in by_name]
        if missing_key:
            raise CSVValidationError(
                f"The report no longer has the column(s) {', '.join(missing_key)}, which identify a row "
                "in the table. Put them back in the saved report's columns (loading without them would "
                "create duplicate rows)."
            )
        absent = [c for c in table_columns if c not in by_name]
        if absent:
            logger.warning("Column(s) in the table but not in the report, stored as NULL: %s", ", ".join(absent))
        specs = [
            ColumnSpec(by_name[name].source, name, kind) if name in by_name else ColumnSpec("", name, kind)
            for name, kind in table_columns.items()
        ]
    else:
        key = key_columns(s.name for s in specs)

    if raw.empty:
        frame = pd.DataFrame(columns=[*(s.name for s in specs), *META_COLUMNS])
        return PlacementReport(frame, tuple(specs), key, (), {})

    out = pd.DataFrame(index=raw.index)
    for spec in specs:
        if spec.source:
            out[spec.name] = _convert(raw[spec.source], spec, csv_date_format)
        else:
            out[spec.name] = pd.Series([pd.NA] * len(raw), index=raw.index, dtype="object")
    for column in ("campaign_country", "country_code"):
        if column in out.columns and out[column].notna().any():
            out[column] = out[column].astype("string").str.upper()
    out["report_id"] = report_id
    out["loaded_at"] = loaded_at.replace(tzinfo=None, microsecond=0)

    dates = set(out[date_column])
    if today is not None:
        earliest, latest = today - timedelta(days=max_age_days), today + timedelta(days=1)
        outside = sorted(d for d in dates if d < earliest or d > latest)
        if outside:
            raise DateMismatchError(
                f"The report contains date(s) {', '.join(d.isoformat() for d in outside[:5])} outside "
                f"{earliest.isoformat()}..{latest.isoformat()}. Check the saved report's period, "
                "or raise MAX_REPORT_AGE_DAYS."
            )

    if load_from is not None:
        early = out[date_column] < load_from
        if bool(early.any()):
            logger.info("Skipped %d row(s) dated before LOAD_FROM_DATE %s (%s)", int(early.sum()),
                        load_from.isoformat(), ", ".join(sorted({d.isoformat() for d in out.loc[early, date_column]})))
            out = out.loc[~early]
        if out.empty:
            frame = pd.DataFrame(columns=[*(s.name for s in specs), *META_COLUMNS])
            return PlacementReport(frame, tuple(specs), key, (), {})

    out[ROW_KEY_COLUMN] = [row_key(values) for values in zip(*(out[c].tolist() for c in key))]
    data_columns = [s.name for s in specs]
    exact = out.duplicated(data_columns, keep="first")
    if bool(exact.any()):
        logger.warning("Dropped %d exact duplicate row(s) in the file (every column equal)", int(exact.sum()))
        out = out.loc[~exact]
    duplicated = out.duplicated([ROW_KEY_COLUMN], keep=False)
    if bool(duplicated.any()):
        raise DataTransformationError(_duplicate_message(out, duplicated, key, specs))

    marketplace = next((c for c in ("campaign_country", "country_code") if c in out.columns), None)
    if marketplace:
        pairs = zip(out[date_column], out[marketplace].astype("object").where(out[marketplace].notna(), "-"))
    else:
        pairs = ((d, "-") for d in out[date_column])
    slices = tuple(sorted(set(pairs)))
    text_lengths = {
        s.name: int(out[s.name].dropna().astype(str).str.len().max() or 0)
        for s in specs
        if s.kind in ("text", "text!") and s.source
    }
    frame = out.loc[:, [*(s.name for s in specs), *META_COLUMNS]].reset_index(drop=True)
    report = PlacementReport(frame, tuple(specs), key, slices, text_lengths)
    logger.info("Prepared %d row(s), key (%s): %s", len(frame), ", ".join(key), report.describe())
    return report


def _duplicate_message(
    out: pd.DataFrame, duplicated: pd.Series, key: tuple[str, ...], specs: list[ColumnSpec]
) -> str:
    """Which other columns tell the repeated rows apart -- the hint for fixing the key."""
    rows = out.loc[duplicated]
    group = rows[rows[ROW_KEY_COLUMN] == rows.iloc[0][ROW_KEY_COLUMN]]
    differing = [
        s.source or s.name
        for s in specs
        if s.name not in key and group[s.name].astype(str).nunique() > 1
    ]
    sample = {k: (None if pd.isna(v) else str(v)) for k, v in group.iloc[0][list(key)].items()}
    hint = (
        f" The repeated rows differ in: {', '.join(differing[:8])} -- if that column identifies a "
        "row, it must be part of the key (app/placement_processor.py key_columns)."
        if differing
        else ""
    )
    return (
        f"{int(duplicated.sum())} row(s) repeat the same {', '.join(key)}, e.g. {sample}.{hint} "
        "Nothing was written."
    )
