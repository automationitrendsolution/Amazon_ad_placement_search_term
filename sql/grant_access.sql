-- Optional: only needed if the itrend login in .env cannot create / write the table.
-- Run as a MySQL admin. A dedicated login that can only read and upsert rows in
-- this one table. Replace the password, then put the same user/password in .env.

CREATE USER IF NOT EXISTS 'placement_loader'@'%' IDENTIFIED BY 'change-me';

-- Hourly load: INSERT ... ON DUPLICATE KEY UPDATE (upsert) needs INSERT + UPDATE.
-- Nothing is ever deleted, so no DELETE grant.
GRANT SELECT, INSERT, UPDATE ON itrend.search_term_placement_data TO 'placement_loader'@'%';

-- The first run creates the table from the report's columns (DB_AUTO_CREATE_TABLE=true).
-- That needs CREATE once; you can revoke it after the table exists.
GRANT CREATE ON itrend.* TO 'placement_loader'@'%';
-- REVOKE CREATE ON itrend.* FROM 'placement_loader'@'%';
