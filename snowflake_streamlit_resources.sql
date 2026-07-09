-- ═══════════════════════════════════════════════════════════════════
--  Snowflake resources for the Pet Rent Streamlit-in-Snowflake app
--  Idempotent; re-run the relevant section after adding hosts/secrets.
--  The secret NAMES in the final ALTER STREAMLIT mapping are what the
--  app code reads (snowflake_auth.get_app_secret) — do not rename them.
-- ═══════════════════════════════════════════════════════════════════

-- CREATE SECRETS TO STORE API CREDENTIALS
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_yardi_token
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER';
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_entrata_key
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER';
-- RealPage OneSite (live RealPage provider)
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_onesite_username
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER';
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_onesite_password
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER';
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_onesite_license_key
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER';

-- CREATE NETWORK RULE (egress allow-list for the live PMS APIs)
CREATE OR REPLACE NETWORK RULE pet_rent_api_egress
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = (
    -- Entrata REST
    'apis.entrata.com',
    -- RealPage OneSite SOAP gateway
    'gateway.rpx.realpage.com',
    -- Yardi SOAP hosts — per-PMC servers; check RESIDENT_DATA_URL values
    -- in STG_PETSCREENING__INTEGRATIONS and add any new ones here:
    'www.yardiasp.com',
    'www.yardiasp13.com',
    'www.yardiasp14.com',
    'www.yardiaspcn6.com',
    'www.yardiaspla2.com',
    'www.yardiaspla5.com',
    'www.yardiaspnc7.com',
    'www.yardiaspnc8.com',
    'www.yardiasptx10.com',
    'www.yardiasptx11.com',
    'www.yardipca.com',
    'www.yardipcf.com',
    'www.yardipco.com',
    'www.yardipcu.com',
    'www.yardipcv.com'
  );

-- CREATE EXTERNAL ACCESS INTEGRATION
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION pet_rent_api_access
  ALLOWED_NETWORK_RULES = (pet_rent_api_egress)
  ALLOWED_AUTHENTICATION_SECRETS = (
    pet_rent_yardi_token,
    pet_rent_entrata_key,
    pet_rent_onesite_username,
    pet_rent_onesite_password,
    pet_rent_onesite_license_key
  )
  ENABLED = TRUE;

-- GRANT USAGE TO THE STREAMLIT APP CREATOR ROLE
GRANT READ ON SECRET prod.pet_rent.pet_rent_yardi_token TO ROLE sysadmin;
GRANT READ ON SECRET prod.pet_rent.pet_rent_entrata_key TO ROLE sysadmin;
GRANT READ ON SECRET prod.pet_rent.pet_rent_onesite_username TO ROLE sysadmin;
GRANT READ ON SECRET prod.pet_rent.pet_rent_onesite_password TO ROLE sysadmin;
GRANT READ ON SECRET prod.pet_rent.pet_rent_onesite_license_key TO ROLE sysadmin;
GRANT USAGE ON INTEGRATION pet_rent_api_access TO ROLE sysadmin;

-- ATTACH INTEGRATIONS + SECRETS TO THE STREAMLIT APP
ALTER STREAMLIT prod.pet_rent.ME1HXAJNZ6MDQ1AK
  SET EXTERNAL_ACCESS_INTEGRATIONS = (pet_rent_api_access, pypi_access_integration)
  SECRETS = (
    'YARDI_LICENSE_TOKEN'  = prod.pet_rent.pet_rent_yardi_token,
    'ENTRATA_API_KEY'      = prod.pet_rent.pet_rent_entrata_key,
    'ONESITE_USERNAME'     = prod.pet_rent.pet_rent_onesite_username,
    'ONESITE_PASSWORD'     = prod.pet_rent.pet_rent_onesite_password,
    'ONESITE_LICENSE_KEY'  = prod.pet_rent.pet_rent_onesite_license_key
  );

-- REFRESH THE GIT REPO (run after every merge to master — or use the
-- "Fetch" button on the repository object in Snowsight)
ALTER GIT REPOSITORY PROD.PET_RENT."pet-rent-streamlit-in-snowflake" FETCH;

-- ALLOW STREAMLIT APP TO DOWNLOAD PACKAGES (one-time, ACCOUNTADMIN):
USE ROLE ACCOUNTADMIN;
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION pypi_access_integration
  ALLOWED_NETWORK_RULES = (snowflake.external_access.pypi_rule)
  ENABLED = TRUE;
GRANT USAGE ON INTEGRATION pypi_access_integration TO ROLE SYSADMIN;
