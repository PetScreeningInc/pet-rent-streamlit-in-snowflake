# Pet Rent — Streamlit in Snowflake

The PetScreening **Pet Value Report** app, deployed inside Snowflake
(Streamlit-in-Snowflake). This repo mirrors the actively developed app in
[e-salvador13/pet-rent](https://github.com/e-salvador13/pet-rent) — the full
current version (live Yardi/Entrata/RealPage fetching, Snowflake fetch
cache, missing-pet-rent + suspected reports, branded PDFs/CSVs, snapshot
tables, Pet Rent Pricing) plus a small compatibility layer for running
inside Snowflake.

## How updates reach Snowflake

Snowflake has a GIT REPOSITORY object
(`PROD.PET_RENT."pet-rent-streamlit-in-snowflake"`) pointing at this repo.

1. Merge changes into `master` here.
2. In Snowsight, open the repository object and click **Fetch**
   (or run `ALTER GIT REPOSITORY PROD.PET_RENT."pet-rent-streamlit-in-snowflake" FETCH;`).
3. Reopen the Streamlit app — it serves the fetched code.

`snowflake_streamlit_resources.sql` provisions everything the app needs:
secrets (Yardi/Entrata/OneSite), the egress network rule for the live PMS
APIs, the external access integration, and the `ALTER STREAMLIT ... SET
SECRETS` mapping. Re-run the relevant section when adding a new Yardi host
or credential.

## How the app adapts to Streamlit-in-Snowflake

All handled in `snowflake_auth.py` — the rest of the codebase is identical
to the local version:

- **Connection** — inside Snowflake, `get_snowflake_connection()` returns
  the active session's connection (wrapped so the app's `conn.close()`
  calls are no-ops); locally it uses keypair/password auth from `.env`.
- **API credentials** — `get_app_secret()` reads Snowflake secrets attached
  to the STREAMLIT object (via the `_snowflake` module) and falls back to
  environment variables locally.
- **Filesystem** — crash-safe CSV auto-exports fall back to the temp dir
  (the app stage is read-only); durable state lives in Snowflake anyway
  (fetch cache `RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA`,
  snapshot tables `RAW.MISC.PET_VALUE_*`).

## Running locally

```bash
python3.12 -m streamlit run app.py --server.port 8517 --server.headless true
```

Requires a `.env` with `SNOWFLAKE_*` keypair auth plus
`YARDI_LICENSE_TOKEN`, `ENTRATA_API_KEY`, `ONESITE_*` (see
`snowflake_auth.py` docstring). Install deps from `requirements.txt`
(local) — `environment.yml` is what Streamlit-in-Snowflake installs from
the Snowflake Anaconda channel.

## Tests

```bash
python3.12 -m pytest tests/ -q
```

## Notes / limitations inside Snowflake

- Yardi SOAP hosts must be listed in the `pet_rent_api_egress` network
  rule — a PMC on an unlisted `yardi*.com` server will fail to fetch until
  the host is added and the integration re-created.
- The batch scripts (`batch_pdf.py`, `batch_csv.py`, etc.) are for local /
  server use; they don't run inside Streamlit-in-Snowflake.
- RealPage is current-snapshot only until `getresidentledger` is
  license-authorized (same as everywhere else).
