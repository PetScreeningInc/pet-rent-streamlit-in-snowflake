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

## Architecture

The app follows the modular layout this repo introduced (the monolithic
`app.py` from the pet-rent repo is split along the same seams as the
original modularization — every relocated function is AST-identical to its
pet-rent original):

```
app.py                        Streamlit UI orchestration only (tabs, sidebar, session state)
config.py                     Shared constants (JUNK_EMAILS)
services/
  snowflake_io.py             Connection (SiS active-session OR local keypair) + secrets
  yardi.py                    Yardi property/parent queries + GetRentroll SOAP fetch
  entrata.py                  Entrata property/parent queries + getLeases REST fetch
  realpage.py                 RealPage staging queries + staging/live hybrid fetch
  appfolio.py                 AppFolio staging-backed provider
analytics/
  launch_analysis.py          Before/after launch analysis, health checks, QBR adoption
  missing_pet_rent.py         Missing Pet Rent engine (household-active only)
  suspected_undisclosed.py    Suspected Undisclosed engine (incl. assistance non-responsive)
components/
  ui_helpers.py               Brand CSS, logo, password gate, table/funnel widgets
  charts.py                   Plotly chart builders
reports/
  pdf_report.py               Branded executive PDF (fpdf2)
  html_report.py              Branded HTML report + exec summary HTML
snowflake_auth.py             Back-compat shim → services.snowflake_io (used by batch tools)
realpage_live_api.py          OneSite live SOAP client (used by services.realpage)
fetch_cache.py                Append-only Snowflake fetch cache
snapshot_tables.py            RAW.MISC PET_VALUE_* auto-append
batch_pdf.py / batch_csv.py   Local batch tools (import from app's namespace)
```

Source-level regression tests assert against the ordered concatenation of
these files (`tests/app_source.py`), so "the app contains X" keeps meaning
what it meant in the monolith.

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
