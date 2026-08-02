-- A separate database for the `db`-marked tests.
--
-- The test fixtures refuse to run against anything not named `reviewhive_test`,
-- so a mistyped URL cannot truncate the development database. That guard is only
-- practical if the test database exists without anyone having to create it.
--
-- Postgres runs this once, when the data directory is first initialised. It will
-- not re-run against an existing volume.

CREATE DATABASE reviewhive_test OWNER reviewhive;
