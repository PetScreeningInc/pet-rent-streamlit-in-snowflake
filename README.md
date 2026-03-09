# 🐾 Pet Rent Analyzer

A Streamlit application that connects to Snowflake, fetches live Yardi rent roll data via the GetRentroll SOAP API, and provides:

1. **Fee Collection Visualizations** — interactive charts of pet rent / fee trends over time
2. **Missing Pet Rent Report** — identifies current tenants with household pets who are NOT paying selected pet charges, downloadable as CSV or Excel

---

## Quick Start

```bash
# Install dependencies
pip install streamlit pandas plotly requests snowflake-connector-python python-dotenv openpyxl numpy

# Run the app
streamlit run app.py --server.port 8501
```

Open **http://localhost:8501** in your browser. You'll get a Duo Push MFA prompt on the first Snowflake connection.

---

## Prerequisites

### `.env` file

Create a `.env` file in the project root with your Snowflake and Yardi credentials:

```env
SNOWFLAKE_ACCOUNT=PTNQTDQ-KDB61325
SNOWFLAKE_USER=YOUR_USERNAME
SNOWFLAKE_PASSWORD=YOUR_PASSWORD
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=DEVELOPER
YARDI_LICENSE_TOKEN=MIIB...  # Your Yardi InterfaceLicense token
```

### Python Packages

| Package | Purpose |
|---------|---------|
| `streamlit` | Web application framework |
| `pandas` | Data manipulation |
| `plotly` | Interactive charts |
| `requests` | HTTP/SOAP API calls to Yardi |
| `snowflake-connector-python` | Snowflake database connection |
| `python-dotenv` | Load credentials from `.env` |
| `openpyxl` | Excel file export |
| `numpy` | Numeric operations |

---

## How It Works — End-to-End Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SIDEBAR SELECTION                            │
│  User picks: Parent Company Name / Ancestry ID / Property ID        │
│  User sets: Lookback period (6–60 months, default 24)               │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SNOWFLAKE: d_properties + integrations → property list + creds     │
│  Shows: total properties vs. API-accessible properties              │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  YARDI API: GetRentroll SOAP call per property                      │
│  → Flat table of ALL lease charges (replaces staging table)         │
│  → User selects which charge codes = pet fees                       │
└─────────┬───────────────────────────────────────┬───────────────────┘
          │                                       │
          ▼                                       ▼
┌──────────────────────┐             ┌────────────────────────────────┐
│  📈 CHARTS TAB       │             │  📋 REPORT TAB                 │
│                      │             │                                │
│  Filter to selected  │             │  1. paying_set from live API   │
│  charge codes        │             │  2. SF → PetScreening profiles │
│  → portfolio chart   │             │  3. match → Profile_No_Rent    │
│  → stacked area      │             │  4. SF → exec summary          │
│  → current snapshot  │             │  5. final join → download      │
│  → property grid     │             │                                │
│    with launch dates │             │  📖 DOCS TAB — all SQL shown   │
└──────────────────────┘             └────────────────────────────────┘
```

---

## Step-by-Step Usage

### 1. Select Properties (Sidebar)

Three search options:

| Option | Field | Example |
|--------|-------|---------|
| **Parent Company Name** | `d_properties.parent_company_name` | `Case & Associates - Enterprise` |
| **Parent Company Ancestry ID** | `d_properties.parent_company_ancestry_id` | `61610` |
| **Property Name / ID** | `d_properties.property_id` | `94672` |

The dropdown shows the **total** property count from `d_properties` (e.g., "40 props"). Below it shows how many have active Yardi API integrations (e.g., "34 of 40 have Yardi API access").

Set the **lookback period** slider (default 24 months, up to 60).

### 2. Fetch Rent Roll Data

Click **🚀 Fetch Rent Roll Data**. The app:

1. Queries Snowflake for properties with Yardi integration credentials
2. Calls the Yardi GetRentroll SOAP API for each property
3. Parses the XML response → flat table of lease charges
4. Shows a fetch summary: ✅ success · ❌ errors · ⚠️ warnings

**Expand "📋 Fetch Results"** to see per-property status.

### 3. Select Charge Codes

All unique charge codes are listed. The app **auto-selects** codes containing "pet", "animal", "petnr", "concpet". You can add/remove codes.

The selected codes summary table shows count and total amount **per code** (fixes the old bug where newly-added codes weren't visible).

### 4. Tab: 📈 Fee Collection Charts

Click **Analyze & Visualize** to generate:

- **Portfolio aggregate** — monthly bar chart of total revenue + charge count line
- **Stacked area** — each property's contribution over time
- **Current snapshot** — horizontal bar ranking properties by latest month revenue
- **Individual property grid** — small multiples with 🚀 red dashed lines for PetScreening launch dates
- **Monthly data table** + CSV download

### 5. Tab: 📋 Missing Pet Rent Report

Click **🔍 Generate Missing Pet Rent Report**. The app:

1. **From live API data**: builds a set of `(property_id, tenant_code)` paying selected charges
2. **Snowflake query**: gets PetScreening household profiles via `petscreening__user_enriched` + `d_units` + `f_leases`
3. **Python join**: matches profiles to paying tenants → anyone with a pet and no matching charge = `Profile_No_Rent`
4. **Snowflake query**: pulls detailed pet/profile info from `R_MONTHLY_EXECUTIVE_SUMMARY`
5. **Python join**: keeps only records matching the missing tenants

**Output includes:** pet profile URL, pet name, breed, species, tenant name, email, unit address, compliance status, profile type/status.

**Download as:** CSV or Excel (multi-sheet with property breakdown).

### 6. Tab: 📖 Documentation & SQL

Full documentation with:
- Every SQL query running behind the scenes
- Python logic translated to SQL equivalents
- ASCII data flow diagram
- Table of all Snowflake tables used

---

## Snowflake Tables Used

| Table | Purpose |
|-------|---------|
| `PROD.COMMON.D_PROPERTIES` | Property master data, parent company info, ancestry IDs |
| `PROD.STAGING.STG_PETSCREENING__INTEGRATIONS` | Yardi API credentials per integration |
| `PROD.STAGING.STG_PETSCREENING__UNITS` | Property code mapping |
| `PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS` | PetScreening launch dates per property |
| `PROD.COMMON.D_UNITS` | Unit dimension (links PetScreening profiles to properties) |
| `PROD.PETSCREENING.PETSCREENING__USER_ENRICHED` | PetScreening user profiles, compliance, pet type |
| `PROD.COMMON.F_LEASES` | Lease facts (links `tenant_code` to PetScreening `user_key`) |
| `PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY` | Detailed pet/profile data for the final report |

---

## Yardi API Details

- **Endpoint:** `GetRentroll` (SOAP over HTTP POST)
- **Namespace:** `http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData`
- **Auth:** Per-property credentials stored in `STG_PETSCREENING__INTEGRATIONS.settings` (JSON)
- **License:** `YARDI_LICENSE_TOKEN` from `.env`
- **Parameters:** `YardiPropertyId`, `MoveIn`, `MoveOut`, `LeaseChgFrom`, `LeaseChgTo` (all set to the lookback date)
- **Response:** XML with nested `Property > Units > Unit > Tenants > Tenant > LeaseCharges > LeaseCharge`

---

## How the Report Replaces the dbt Pipeline

The traditional pipeline:
```
raw.yardi_getrentroll_new
    → stg_pmc_integrations_yardi__getrentroll_new  (dbt staging model)
    → ht_pmc_integrations_yardi__getrentroll_new   (dbt join model)
    → final SELECT with R_MONTHLY_EXECUTIVE_SUMMARY
```

This app replaces the first two steps:
```
LIVE Yardi API call (app.py)             ← replaces stg_ staging table
    → Python join with PS profiles       ← replaces ht_ join model
    → Snowflake: R_MONTHLY_EXECUTIVE_SUMMARY (same final query)
```

Key difference: the app uses **selected charge codes** instead of the hardcoded `charge_code ILIKE '%pet%'` in the dbt model.

---

## File Structure

```
pet-rent/
├── app.py                  # Main Streamlit application (all logic)
├── .env                    # Snowflake + Yardi credentials (gitignored)
├── README.md               # This file
└── *.md                    # Snowflake stored procedure references
    ├── SP_LOAD_YARDI_GETRENTROLL(...)   # GetRentroll SP variants
    ├── SP_YARDI_GETRENTROLL(...)        # GetRentroll SP variants
    ├── LOAD_YARDI_RESIDENTS()           # GetResidentsByDate SP
    └── LOAD_YARDI_RESIDENTS_BY_STATUS() # GetResidentsByStatus SP
```

---

## Known Behaviors

- **Duo Push MFA**: Snowflake connection triggers a Duo Push on first connect (cached afterward)
- **Property count gap**: The dropdown shows total properties from `d_properties`; the fetch may process fewer (only those with integration credentials)
- **API timeouts**: Some properties may timeout (120s limit) — they show as ❌ in the fetch log
- **No data properties**: Some property codes return "Invalid or no access" from Yardi — shown as ⚠️
- **`actually_paying_property` filter**: The report only includes properties where at least one tenant IS paying the selected charges (same as the original dbt model's `actually_paying_property_custom_field > 0` filter)
