"""MySQL persistence for ``itrend.search_term_placement_data`` -- and nothing else.

Rows are matched on ``row_key`` (SHA-256 of the natural key: date, hour,
marketplace, campaign, ad group, target, search term, placement), which has a
UNIQUE index, so the table can never hold the same row twice.

``upsert`` (default, ``DB_LOAD_MODE`` / ``--load-mode``), per chunk::

      INSERT ... ON DUPLICATE KEY UPDATE <every column but row_key>
      SELECT COUNT(*) ... WHERE row_key IN (chunk)  -- must equal the chunk

New rows are added; rows already in the table get the report's latest values
(each hourly run refreshes the day's clicks, spend, sales ...).  Nothing is
deleted.

``insert`` adds new rows only; rows already in the table are left unchanged.

With ``DB_COMMIT_EVERY_ROWS`` = 0 a file is one transaction; otherwise every
that many rows are committed on their own, so a big file survives a dropped
connection and a re-run continues where it stopped.

The table is created from the first report's columns (automatically on the
first load when ``DB_AUTO_CREATE_TABLE=true``, or with ``run.py --create-table
FILE``); the DDL used is saved to ``sql/create_search_term_placement_data.sql``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    create_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateIndex, CreateTable

from .config import ALLOWED_TABLES, KEYED_LOAD_MODES, LOAD_MODES, PROJECT_ROOT, ROW_KEY_COLUMN, TARGET_TABLE, DatabaseConfig
from .exceptions import DatabaseError
from .logging_config import get_logger
from .placement_processor import KNOWN_COLUMNS, META_COLUMNS, PlacementReport

logger = get_logger("database")

DDL_FILE = PROJECT_ROOT / "sql" / "create_search_term_placement_data.sql"

#: MySQL errors a second attempt can plausibly get past.
TRANSIENT_MYSQL_ERRNOS = frozenset({1205, 1213, 2003, 2006, 2013, 2055})

#: Database column -> kind, for the columns the processor knows by name.
_KNOWN_KINDS: dict[str, str] = {name: kind for name, kind in KNOWN_COLUMNS.values()}
#: Text columns that only ever hold short codes / statuses.
_SHORT_TEXT = frozenset({
    "campaign_country", "country_code", "budget_currency", "campaign_status", "campaign_delivery_status",
    "target_status", "target_type", "targeting_match_type", "match_type", "campaign_type", "ad_product",
    "campaign_budget_type",
})
_VARCHAR_BYTES_LIMIT = 48_000  # stay well under MySQL's 65,535-byte row limit (utf8mb4 = 4 bytes/char)


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    error = DatabaseError(message)
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    error.transient = bool(getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS)
    return error


@dataclass(frozen=True)
class WriteResult:
    table: str
    slices: tuple[tuple[date, str], ...]
    inserted: int
    mode: str = "upsert"
    #: upsert: rows already in the table, overwritten with the file's values.
    updated: int = 0
    #: insert: rows already in the table, left unchanged.
    skipped: int = 0

    def describe(self) -> str:
        if self.mode == "upsert":
            return f"upsert: {self.inserted} inserted, {self.updated} updated"
        return f"insert: {self.inserted} inserted, {self.skipped} already present (unchanged)"


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Pooled engine.  The URL is never logged with its password."""
    try:
        return create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not create engine for {config.safe_url}: {exc}") from exc


# ---------------------------------------------------------------------------
# table definition from a report
# ---------------------------------------------------------------------------


def _sql_type(name: str, kind: str, longest: int, *, use_text: bool):
    if kind == "date":
        return Date()
    if kind == "hour":
        return SmallInteger()
    if kind in ("id", "id?"):
        return BigInteger()
    if kind == "count":
        return BigInteger()
    if kind in ("metric", "number"):
        return Numeric(20, 6)
    if name in _SHORT_TEXT and longest <= 64:
        return String(64)
    if use_text:
        return Text()
    return String(512)


def table_for_report(report: PlacementReport, metadata: MetaData | None = None, name: str = TARGET_TABLE) -> Table:
    """The table a report's columns need: id, the report's columns, report_id, loaded_at, row_key."""
    metadata = metadata or MetaData()
    key = set(report.key)
    # Long free-text columns become TEXT so the row fits MySQL's row-size limit.
    text_specs = [s for s in report.columns if s.kind in ("text", "text!")]
    budget = sum(4 * 512 for s in text_specs if not (s.name in _SHORT_TEXT and report.text_lengths.get(s.name, 0) <= 64))
    as_text: set[str] = set()
    for spec in sorted(text_specs, key=lambda s: -report.text_lengths.get(s.name, 0)):
        longest = report.text_lengths.get(spec.name, 0)
        if spec.name in key or (spec.name in _SHORT_TEXT and longest <= 64):
            continue
        if longest > 400 or budget > _VARCHAR_BYTES_LIMIT:
            as_text.add(spec.name)
            budget -= 4 * 512
    if any(report.text_lengths.get(c, 0) > 512 for c in key):
        raise DatabaseError("A natural-key text value is longer than 512 characters; cannot size the table")

    id_type = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql").with_variant(Integer(), "sqlite")
    columns: list[object] = [Column("id", id_type, primary_key=True, autoincrement=True)]
    for spec in report.columns:
        columns.append(Column(
            spec.name,
            _sql_type(spec.name, spec.kind, report.text_lengths.get(spec.name, 0), use_text=spec.name in as_text),
            nullable=spec.name != "report_date",
            comment=f"Amazon column: {spec.source}" if spec.source else None,
        ))
    columns += [
        Column("report_id", String(36), nullable=True, comment="Amazon saved report the row came from"),
        Column("loaded_at", DateTime(), nullable=False, comment="UTC time of the last load that wrote the row"),
        Column(
            ROW_KEY_COLUMN,
            String(64).with_variant(mysql.CHAR(64, charset="ascii", collation="ascii_bin"), "mysql"),
            nullable=False,
            comment=f"SHA-256 of {'|'.join(report.key)} (upsert key)"[:1024],
        ),
        Index(f"uk_{ROW_KEY_COLUMN}", ROW_KEY_COLUMN, unique=True),
        Index("ix_report_date", "report_date"),
    ]
    names = {s.name for s in report.columns}
    if "campaign_id" in names:
        columns.append(Index("ix_campaign_date", "campaign_id", "report_date"))
    if "search_term" in names:
        columns.append(Index("ix_search_term", "search_term", mysql_length={"search_term": 100}))
    return Table(
        name, metadata, *columns,
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_unicode_ci",
        comment="Amazon Ads placement search term report (placment_search_term_Automation), loaded hourly",
    )


def mysql_ddl(table: Table) -> str:
    """CREATE TABLE + CREATE INDEX statements, as MySQL will run them."""
    dialect = mysql.dialect()
    statements = [str(CreateTable(table).compile(dialect=dialect)).strip()]
    statements += [str(CreateIndex(index).compile(dialect=dialect)).strip() for index in table.indexes]
    return ";\n\n".join(statements) + ";\n"


def _kind_from_sql(name: str, column_type) -> str:
    if name in _KNOWN_KINDS:
        return _KNOWN_KINDS[name]
    if isinstance(column_type, Date):
        return "date"
    if isinstance(column_type, (Integer, BigInteger, SmallInteger)):
        return "id?" if name.endswith("_id") else "count"
    if isinstance(column_type, Numeric):
        return "number"
    return "text"


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


class PlacementDatabase:
    """Schema checks, creation and keyed writes for the one allowed table."""

    def __init__(
        self, engine: Engine, table_name: str = TARGET_TABLE, *, chunk_size: int = 2000, commit_every: int = 0
    ) -> None:
        if table_name not in ALLOWED_TABLES:
            raise DatabaseError(
                f"Refusing to use table {table_name!r}: this project only writes {', '.join(sorted(ALLOWED_TABLES))}"
            )
        self.engine = engine
        self.table_name = table_name
        self.chunk_size = chunk_size
        #: Rows per committed batch (0 = one transaction).
        self.commit_every = commit_every
        self._table: Table | None = None

    # -- schema -------------------------------------------------------------

    def check_connection(self) -> None:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Database connection verified")

    def exists(self) -> bool:
        try:
            return inspect(self.engine).has_table(self.table_name)
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not inspect the database: {exc}", exc) from exc

    def reflect(self) -> Table:
        if not self.exists():
            raise DatabaseError(
                f"Table {self.table_name!r} does not exist. Set DB_AUTO_CREATE_TABLE=true, or create it "
                "with: python run.py --create-table <downloaded report.csv>"
            )
        try:
            self._table = Table(self.table_name, MetaData(), autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not reflect {self.table_name!r}: {exc}", exc) from exc
        logger.info("Reflected %s with %d column(s)", self.table_name, len(self._table.columns))
        return self._table

    @property
    def table(self) -> Table:
        return self._table if self._table is not None else self.reflect()

    def has_unique_row_key(self) -> bool:
        """True when ``row_key`` has a UNIQUE index or constraint of its own."""
        try:
            inspector = inspect(self.engine)
            unique = [i["column_names"] for i in inspector.get_indexes(self.table_name) if i.get("unique")]
            try:
                unique += [c["column_names"] for c in inspector.get_unique_constraints(self.table_name)]
            except NotImplementedError:
                pass
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not inspect indexes of {self.table_name}: {exc}", exc) from exc
        return any(list(columns) == [ROW_KEY_COLUMN] for columns in unique)

    def validate_table(self) -> None:
        """The table must have id, the meta columns and a UNIQUE row_key.

        Raises:
            DatabaseError: Otherwise -- loading would risk duplicates.
        """
        live = {c.name for c in self.table.columns}
        missing = [c for c in ("id", *META_COLUMNS) if c not in live]
        if missing:
            raise DatabaseError(f"{self.table_name} is missing column(s) {', '.join(missing)}")
        if not self.has_unique_row_key():
            raise DatabaseError(
                f"{self.table_name}.{ROW_KEY_COLUMN} has no UNIQUE index; without it duplicates are possible. "
                f"Add it: ALTER TABLE {self.table_name} ADD UNIQUE KEY uk_row_key (row_key);"
            )
        logger.info("Table %s validated (%d report column(s), unique row_key)", self.table_name,
                    len(self.report_columns()))

    def report_columns(self) -> dict[str, str]:
        """The table's report columns (not id / meta), name -> kind, in table order."""
        return {
            c.name: _kind_from_sql(c.name, c.type)
            for c in self.table.columns
            if c.name != "id" and c.name not in META_COLUMNS
        }

    def create_table(self, report: PlacementReport, ddl_file: Path | None = None) -> bool:
        """Create the table for this report's columns.  Returns False if it already existed.

        The DDL used is saved to ``ddl_file`` (default: sql/create_search_term_placement_data.sql).
        """
        if self.exists():
            logger.info("%s already exists; not changed", self.table_name)
            return False
        ddl_file = ddl_file or DDL_FILE
        table = table_for_report(report, name=self.table_name)
        if ddl_file is not None:
            try:
                ddl_file.parent.mkdir(parents=True, exist_ok=True)
                ddl_file.write_text(
                    f"-- itrend.{self.table_name}: generated from the report's columns by run.py.\n"
                    f"-- Natural key (row_key): {', '.join(report.key)}\n\n{mysql_ddl(table)}",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("Could not save the DDL to %s: %s", ddl_file, exc)
        try:
            table.metadata.create_all(self.engine, tables=[table], checkfirst=True)
        except SQLAlchemyError as exc:
            raise _database_error(f"CREATE TABLE {self.table_name} failed: {exc}", exc) from exc
        self._table = None
        logger.info("Created table %s (%d report column(s); key: %s)", self.table_name, len(report.columns),
                    ", ".join(report.key))
        return True

    # -- records --------------------------------------------------------------

    def fit_text(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Shorten text longer than its VARCHAR (MySQL strict mode would refuse the row).

        row_key was computed from the full value, so this never merges rows.
        """
        for column in self.table.columns:
            length = getattr(column.type, "length", None)
            if column.name not in frame.columns or not isinstance(column.type, String) or not length:
                continue
            if column.name == ROW_KEY_COLUMN:
                continue
            values = frame[column.name]
            if values.dtype == object or str(values.dtype).startswith("string"):
                too_long = values.astype("string").str.len() > length
                if bool(too_long.fillna(False).any()):
                    logger.warning("%d %s value(s) longer than %d characters were shortened",
                                   int(too_long.fillna(False).sum()), column.name, length)
                    frame = frame.copy()
                    frame[column.name] = values.astype("string").str.slice(0, length)
        return frame

    def to_records(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        """Rows as driver-ready dicts: pandas NA -> NULL, numpy scalars -> Python."""
        records: list[dict[str, object]] = []
        integer_columns = {
            c.name for c in self.table.columns if isinstance(c.type, (Integer, BigInteger, SmallInteger))
        }
        for row in frame.to_dict(orient="records"):
            record: dict[str, object] = {}
            for key, value in row.items():
                if value is None or value is pd.NA or value is pd.NaT:
                    value = None
                elif isinstance(value, (float, np.floating)):
                    value = None if np.isnan(value) else float(value)
                    if value is not None and key in integer_columns:
                        value = int(round(value))
                elif isinstance(value, np.integer):
                    value = int(value)
                elif isinstance(value, pd.Timestamp):
                    value = value.to_pydatetime()
                record[key] = value
            records.append(record)
        return records

    def lazy_records(self, frame: pd.DataFrame) -> "FrameRecords":
        """Like :meth:`to_records`, but converted one chunk at a time while writing."""
        return FrameRecords(self, self.fit_text(frame).reset_index(drop=True))

    # -- writes -------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT on one connection, ROLLBACK on any error."""
        connection = self.engine.connect()
        try:
            trans = connection.begin()
            try:
                yield connection
            except Exception as exc:
                try:
                    trans.rollback()
                except SQLAlchemyError as rollback_exc:
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.error("ROLLBACK after error: %s: %s", type(exc).__name__, exc)
                raise
            trans.commit()
        finally:
            connection.close()

    def write(
        self, mode: str, slices: Sequence[tuple[date, str]], records: Sequence[dict[str, object]]
    ) -> WriteResult:
        """Write one validated report with the given load mode."""
        if mode not in KEYED_LOAD_MODES:
            raise ValueError(f"Unknown load mode {mode!r}; use one of {', '.join(LOAD_MODES)}")
        return self.merge_rows(slices, records, update_existing=mode == "upsert")

    def _merge_statement(self, columns: Sequence[str], *, update_existing: bool):
        """INSERT that updates (upsert) or keeps (insert) a row whose row_key already exists."""
        table = self.table
        dialect = self.engine.dialect.name
        updates = [c for c in columns if c != ROW_KEY_COLUMN]
        if dialect in ("mysql", "mariadb"):
            stmt = mysql.insert(table)
            if update_existing:
                return stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in updates})
            return stmt.on_duplicate_key_update({"id": table.c.id})
        if dialect == "sqlite":  # the test suite
            stmt = sqlite.insert(table)
            if update_existing:
                return stmt.on_conflict_do_update(
                    index_elements=[ROW_KEY_COLUMN], set_={c: stmt.excluded[c] for c in updates}
                )
            return stmt.on_conflict_do_nothing(index_elements=[ROW_KEY_COLUMN])
        raise DatabaseError(f"Upsert / insert loads are not supported on {dialect}")

    def merge_rows(
        self,
        slices: Sequence[tuple[date, str]],
        records: Sequence[dict[str, object]],
        *,
        update_existing: bool,
    ) -> WriteResult:
        """Upsert (``update_existing``) or insert-new-only, matched on row_key.

        After every chunk the number of rows with the chunk's keys must equal
        the chunk size -- one row per key, never two.

        Raises:
            ValueError: No records, a record without row_key, or a repeated row_key.
            DatabaseError: Any database failure -- the open batch is rolled back.
        """
        mode = "upsert" if update_existing else "insert"
        if not records:
            raise ValueError(f"{self.table_name}: zero records")
        keys = _column(records, ROW_KEY_COLUMN)
        if not all(keys):
            raise ValueError(f"{self.table_name}: {mode} needs {ROW_KEY_COLUMN} on every record")
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.table_name}: the records repeat a {ROW_KEY_COLUMN}")

        table = self.table
        key_column = table.c[ROW_KEY_COLUMN]
        statement = self._merge_statement(list(records[0]), update_existing=update_existing)
        label = _label(slices)
        total = len(records)
        if self.commit_every:
            batch = max(self.chunk_size, self.commit_every - self.commit_every % self.chunk_size)
        else:
            batch = total
        inserted = existing = committed = 0
        try:
            for batch_start in range(0, total, batch):
                batch_end = min(batch_start + batch, total)
                batch_inserted = batch_existing = 0
                with self.transaction() as connection:
                    for start in range(batch_start, batch_end, self.chunk_size):
                        stop = min(start + self.chunk_size, batch_end)
                        count = select(func.count()).select_from(table).where(key_column.in_(keys[start:stop]))
                        already = int(connection.execute(count).scalar_one())
                        if not (already == stop - start and not update_existing):
                            connection.execute(statement, list(records[start:stop]))
                            present = int(connection.execute(count).scalar_one())
                            if present != stop - start:
                                raise DatabaseError(
                                    f"After {mode} {present} row(s) exist for {stop - start} key(s); rolling back"
                                )
                        batch_existing += already
                        batch_inserted += stop - start - already
                committed = batch_end
                inserted += batch_inserted
                existing += batch_existing
                if batch < total:
                    logger.info("%s progress: %d / %d row(s) committed (%d%%)",
                                mode, committed, total, committed * 100 // total)
        except SQLAlchemyError as exc:
            kept = (
                f"; {committed} of {total} row(s) were already committed and stay -- run again to continue"
                if committed else " and was rolled back"
            )
            raise _database_error(f"{mode.capitalize()} for {label} failed{kept}: {exc}", exc) from exc
        except DatabaseError as exc:
            if committed:
                raise DatabaseError(f"{exc} ({committed} of {total} row(s) were already committed)") from exc
            raise
        result = WriteResult(
            self.table_name, tuple(slices), inserted, mode,
            updated=existing if update_existing else 0,
            skipped=0 if update_existing else existing,
        )
        logger.info("Committed %s: %s (%s)", self.table_name, result.describe(), label)
        return result

    def duplicate_keys(self) -> int:
        """Row keys stored more than once (always 0 with the UNIQUE index)."""
        table = self.table
        inner = select(table.c[ROW_KEY_COLUMN]).group_by(table.c[ROW_KEY_COLUMN]).having(func.count() > 1)
        try:
            with self.engine.connect() as connection:
                return int(connection.execute(select(func.count()).select_from(inner.subquery())).scalar_one())
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not check for duplicates: {exc}", exc) from exc

    def dispose(self) -> None:
        self.engine.dispose()


def _label(slices: Sequence[tuple[date, str]]) -> str:
    if not slices:
        return "-"
    return ", ".join(d.isoformat() + ("" if c == "-" else f"/{c}") for d, c in slices)


class FrameRecords(Sequence):
    """A prepared DataFrame seen as a sequence of driver-ready records.

    Slicing converts only that slice, so a write holds one chunk of dicts in
    memory at a time.
    """

    def __init__(self, database: PlacementDatabase, frame: pd.DataFrame) -> None:
        self.database = database
        self.frame = frame

    def __len__(self) -> int:
        return len(self.frame)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self.database.to_records(self.frame.iloc[index])
        if index < 0:
            index += len(self.frame)
        if not 0 <= index < len(self.frame):
            raise IndexError(index)
        return self.database.to_records(self.frame.iloc[index : index + 1])[0]

    def column(self, name: str) -> list[object]:
        if name not in self.frame.columns:
            return [None] * len(self.frame)
        return [None if v is pd.NA else v for v in self.frame[name].tolist()]


def _column(records: Sequence[dict[str, object]], name: str) -> list[object]:
    """One field of every record, without converting a FrameRecords row by row."""
    if isinstance(records, FrameRecords):
        return records.column(name)
    return [r.get(name) for r in records]


def build_database(
    config: DatabaseConfig,
    *,
    chunk_size: int = 2000,
    commit_every: int = 0,
    echo: bool = False,
) -> PlacementDatabase:
    """Connect, and check the table when it exists -- before anything is downloaded."""
    engine = create_db_engine(config, echo=echo)
    try:
        database = PlacementDatabase(engine, chunk_size=chunk_size, commit_every=commit_every)
        database.check_connection()
        if database.exists():
            database.validate_table()
        else:
            logger.warning("%s does not exist yet", database.table_name)
    except Exception:
        engine.dispose()
        raise
    return database
