-- itrend.search_term_placement_data: add the loader's columns to the table an
-- older loader created (it has `date`, no report_id / loaded_at / row_key).
--
-- Non-destructive: no row is changed or deleted.  The existing rows keep
-- row_key = NULL (a UNIQUE index allows any number of NULLs) -- they are not
-- unique on any key, so they cannot be keyed without losing data.  Every row
-- the loader writes from now on has a row_key and is protected by the index.
--
-- Applied 2026-11-20.  Safe to read; do not re-run (ADD COLUMN would fail).

ALTER TABLE itrend.search_term_placement_data
  ADD COLUMN report_id VARCHAR(36) NULL COMMENT 'Amazon saved report the row came from',
  ADD COLUMN loaded_at DATETIME NULL COMMENT 'UTC time of the last load that wrote the row',
  ADD COLUMN row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL
    COMMENT 'SHA-256 of the natural key (upsert key), NULL on rows loaded before 2026-11-20';

ALTER TABLE itrend.search_term_placement_data
  ADD UNIQUE KEY uk_row_key (row_key);
