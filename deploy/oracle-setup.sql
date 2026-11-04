-- Oracle test setup: run once as SYSTEM on the pluggable database, after the container is up.
--   docker exec -i cosmos-semantic-oracle-1 sqlplus -s system/cosmos@FREEPDB1 < deploy/oracle-setup.sql
--
-- In Oracle a schema is a user. The demo data lives in the COSMOS user (created by the
-- container's APP_USER); the pack tests also load a customer's copy into a SHOP schema, so
-- COSMOS gets the right to create and read tables there. Test databases only.
WHENEVER SQLERROR CONTINUE
CREATE USER shop NO AUTHENTICATION QUOTA UNLIMITED ON users;
WHENEVER SQLERROR EXIT FAILURE
GRANT CREATE ANY TABLE, DROP ANY TABLE, INSERT ANY TABLE, SELECT ANY TABLE TO cosmos;
GRANT SELECT ANY DICTIONARY TO cosmos;
EXIT
