-- Snowflake objects for running the Cosmos Semantic tests (ADR 0010).
-- Run once as ACCOUNTADMIN in a Snowsight worksheet ("Run All").
--
-- 1. Create a key pair outside the repository (never commit the private key):
--      openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out ~/.snowflake/cosmos_test_rsa_key.p8 -nocrypt
--      openssl rsa -in ~/.snowflake/cosmos_test_rsa_key.p8 -pubout
-- 2. Paste the public key body (without the BEGIN/END lines) below and run this script.
-- 3. Set SNOWFLAKE_ACCOUNT (the account_identifier this script prints), SNOWFLAKE_USER=COSMOS_TEST,
--    SNOWFLAKE_PRIVATE_KEY_FILE, SNOWFLAKE_ROLE=COSMOS_TEST_ROLE and SNOWFLAKE_WAREHOUSE=COSMOS_WH.

USE ROLE ACCOUNTADMIN;

-- Smallest warehouse; it suspends after 60 seconds idle.
CREATE WAREHOUSE IF NOT EXISTS COSMOS_WH
  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS COSMOS_DEMO;

-- A role that can only use this warehouse and owns this database.
CREATE ROLE IF NOT EXISTS COSMOS_TEST_ROLE;
GRANT USAGE ON WAREHOUSE COSMOS_WH TO ROLE COSMOS_TEST_ROLE;
GRANT OWNERSHIP ON DATABASE COSMOS_DEMO TO ROLE COSMOS_TEST_ROLE COPY CURRENT GRANTS;
-- The PUBLIC schema Snowflake creates in every database keeps its owner; hand it over too.
GRANT OWNERSHIP ON SCHEMA COSMOS_DEMO.PUBLIC TO ROLE COSMOS_TEST_ROLE COPY CURRENT GRANTS;
GRANT ROLE COSMOS_TEST_ROLE TO ROLE SYSADMIN;

-- Service user: key-pair login only, no password.
CREATE USER IF NOT EXISTS COSMOS_TEST
  TYPE = SERVICE
  DEFAULT_ROLE = COSMOS_TEST_ROLE
  DEFAULT_WAREHOUSE = COSMOS_WH
  RSA_PUBLIC_KEY = '<public key body>';
GRANT ROLE COSMOS_TEST_ROLE TO USER COSMOS_TEST;

SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS account_identifier;
