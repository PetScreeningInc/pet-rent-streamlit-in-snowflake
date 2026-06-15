-- CREATE SECRETS TO STORE API TOKENS
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_yardi_token
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER'
CREATE OR REPLACE SECRET prod.pet_rent.pet_rent_entrata_key
  TYPE = GENERIC_STRING
  SECRET_STRING = 'PLACEHOLDER';

-- CREATE NETWORK RULE

CREATE OR REPLACE NETWORK RULE pet_rent_api_egress

  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = (
    'apis.entrata.com',
    -- Add your Yardi hostnames here — check the RESIDENT_DATA_URL values
    -- in your STG_PETSCREENING__INTEGRATIONS table to find them, e.g.:
    'www.yardiaspnc7.com',
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
    'www.yardipcv.com',
    'www.yardiasp13.com',
    'www.yardiasp14.com',
    'www.yardipca.com'
  );

-- CREATE EXTERNAL ACCESS INTEGRATION

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION pet_rent_api_access
  ALLOWED_NETWORK_RULES = (pet_rent_api_egress)
  ALLOWED_AUTHENTICATION_SECRETS = (pet_rent_yardi_token, pet_rent_entrata_key)
  ENABLED = TRUE;

-- GRANT USAGE ON OBJECTS TO STREAMLIT APP CREATOR ROLE

GRANT READ ON SECRET prod.pet_rent.pet_rent_entrata_key TO ROLE sysadmin;
GRANT READ ON SECRET prod.pet_rent.pet_rent_yardi_token TO ROLE sysadmin;
GRANT USAGE ON INTEGRATION pet_rent_api_access TO ROLE sysadmin;

-- GRANT APP ACCESS TO BOTH EXTERNAL INTEGRATION RULES
  ALTER STREAMLIT prod.pet_rent.ME1HXAJNZ6MDQ1AK
  SET EXTERNAL_ACCESS_INTEGRATIONS = (pet_rent_api_access, pypi_access_integration)
  SECRETS = (
    'YARDI_LICENSE_TOKEN' = prod.pet_rent.pet_rent_yardi_token,
    'ENTRATA_API_KEY'     = prod.pet_rent.pet_rent_entrata_key
  );
-- REFRESH GIT REPO
ALTER GIT REPOSITORY PROD.PET_RENT."pet-rent-streamlit-in-snowflake" FETCH;

-- ALLOW STREAMLIT APP TO DOWNLOAD PACKAGES:

USE ROLE ACCOUNTADMIN;

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION pypi_access_integration
  ALLOWED_NETWORK_RULES = (snowflake.external_access.pypi_rule)
  ENABLED = TRUE;

GRANT USAGE ON INTEGRATION pypi_access_integration TO ROLE SYSADMIN;
