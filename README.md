# PetScreening Value Report App

This app helps build value reports from PMC rent-roll/charge data. It is meant for both:

- **Non-technical users:** fetch data, pick pet-fee charge codes, review trends/opportunity, and download a polished report.
- **Technical users:** inspect the Snowflake/API logic, verify matching assumptions, and run batch exports from the command line.

## What the app does

1. **Fee Collection Charts**
   - Shows selected pet rent / pet fee revenue over time.
   - Breaks revenue down by portfolio and by individual property.
   - Marks each property's PetScreening launch month.
   - Shows current/latest-month lift against the property's observed pre-PetScreening baseline.

2. **Summary / Value Report**
   - Creates a branded stakeholder-ready report.
   - Download options are intentionally simple:
     - **Enhanced PDF** — primary shareable report.
     - **Download HTML** — browser-friendly version.
   - The old duplicate “Original PDF” download is no longer shown.

3. **Missing Pet Rent Report**
   - Finds active household pet profiles that appear current but are not paying the selected pet charge codes.
   - Exports CSV/Excel from the app.
   - Can also be run in batch from `batch_csv.py`.

4. **Suspected Undisclosed Pets**
   - Identifies unresolved household pet profiles and unresolved assistance-animal requests that may represent additional opportunity.
   - Excludes profiles already paying selected pet charges.

5. **Documentation & SQL tab**
   - Explains the data flow, matching rules, launch/baseline methodology, and PMC-specific behavior inside the app.

## Supported PMC systems

- **Yardi** — live `GetRentroll` SOAP API calls per property.
- **Entrata** — live `getLeases` API data, including scheduled charges and AR transactions where available.
- **RealPage / OneSite** — Snowflake staging tables populated by the daily RealPage EKS job.

The app auto-detects the property source system from Snowflake property metadata.

## Quick start

```bash
pip install streamlit pandas plotly requests snowflake-connector-python python-dotenv openpyxl numpy
streamlit run app.py --server.port 8501
```

Then open:

```text
http://localhost:8501
```

Snowflake may trigger Duo/MFA on first connection. Connections are cached after login.

## Required `.env`

Create a `.env` file in the project root. Do not commit it.

```env
SNOWFLAKE_ACCOUNT=[REDACTED]
SNOWFLAKE_USER=[REDACTED]
SNOWFLAKE_PASSWORD=[REDACTED]
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=DEVELOPER
YARDI_LICENSE_TOKEN=[REDACTED]
```

RealPage credentials are not property-level `.env` values in this app; RealPage data is read from Snowflake staging.

## Normal app workflow

### 1. Select properties

Use the sidebar to select one of:

- Parent company name
- Parent company ancestry ID
- Property name / property ID

The app shows how many properties exist in Snowflake and how many have usable source-system data/integrations.

### 2. Fetch rent-roll / charge data

Click **Fetch Rent Roll Data**.

What happens:

- The app queries Snowflake for selected properties and source-system metadata.
- Yardi/Entrata properties are fetched through their APIs.
- RealPage properties are loaded from Snowflake staging.
- The app normalizes the charge data into one shared format.
- A fetch log shows successes, warnings, and errors by property.

### 3. Select charge codes

The app lists charge codes found in the data and auto-selects likely pet-related codes containing terms like:

- `pet`
- `animal`
- `petnr`
- `concpet`

Review the selected codes before running reports. The selected charge codes drive the revenue charts, missing pet rent matching, and exports.

### 4. Review Fee Collection Charts

Click **Analyze & Visualize**.

You will see:

- Portfolio monthly revenue and charge counts.
- Property contribution over time.
- Latest-month property snapshot.
- Individual property charts with launch lines and lift badges.
- Monthly table with CSV download.

### 5. Generate the Summary report

In the **Summary** tab:

1. Review the value metrics.
2. Optionally include property-manager emails if available.
3. Leave the default lift methodology unless the portfolio is highly seasonal/student housing.
4. Click **Generate Report**.
5. Download either:
   - **Enhanced PDF**
   - **Download HTML**

## Baseline and lift methodology

This is the important part for interpreting the charts.

- **Launch month:** counted as post-PetScreening.
- **Pre-PS baseline:** average of up to 6 **observed** pre-launch months for that property.
- **Missing months:** not averaged as `$0`. If a property has only one real pre-PS bar, that one bar is the baseline.
- **Default Monthly Lift:** latest/current visible month revenue minus the pre-PS baseline.
- **Individual property lift badge:** uses the same default methodology property-by-property.
- **Average Monthly Lift toggle:** optional view for seasonal/student-housing portfolios; it uses completed post-launch average lift instead of latest-month lift.
- **Low-data baselines:** fewer than 3 observed pre-launch months are flagged as low-data, but the observed baseline is still shown instead of being diluted by blank months.

Example:

```text
One observed pre-PS month: $50
Latest month:              $50
Displayed baseline:        $50/mo (1 observed pre month)
Displayed lift:            $0/mo
```

## Missing Pet Rent logic

A tenant is considered missing pet rent when:

- They have an active/compliant household pet profile in PetScreening.
- They are current enough to be matched to the source-system tenant/lease data.
- Nobody on the matching tenant/unit/lease path is paying one of the selected pet charge codes.

Matching uses the best available identifiers by system:

- **Yardi:** tenant code, email, and unit-level propagation.
- **Entrata:** email and lease-level propagation because tenant codes can be missing/ineffective.
- **RealPage:** lease-based matching through Snowflake. Direct resident matching includes `lease_id` so charges do not attach to unrelated residents with the same member ID.

## RealPage notes

RealPage behaves differently from Yardi/Entrata:

- Data comes from Snowflake staging, not live interactive SOAP calls.
- Scheduled charge history is limited by the staging/EKS job and OneSite endpoint behavior.
- The app avoids showing fake lift where pre-PS history is not actually available.
- Future scheduled pet rent can still clear a resident from missing-rent status if the charge is already set up to begin in a future month.

If a resident is known to pay but no scheduled charge appears, the data may live in a ledger/transaction endpoint instead of the scheduled-charge data currently used here.

## Batch CSV export

Use `batch_csv.py` when you want app-style CSV outputs without opening Streamlit.

Examples:

```bash
# Missing + suspected reports for specific property IDs
python batch_csv.py --ids 94672,12345 --type property --report both

# Missing Pet Rent only for parent company ancestry IDs
python batch_csv.py --ids 61610,98765 --type parent --report missing --workers 8

# Suspected Undisclosed Pets from a file of IDs
python batch_csv.py --file ids.txt --type parent --report suspected --output ./batch_csvs

# Force charge codes instead of auto-detecting likely pet codes
python batch_csv.py --ids 94672 --type property --charge-codes PETRENT,PETFEE

# Force a source system if auto-detection is not desired
python batch_csv.py --ids 94672 --type property --pmc real_page
```

Outputs are saved under:

```text
batch_csvs/<pmc_system>/
```

A top-level run log is also created:

```text
batch_csvs/batch_csv_log_YYYYMMDD_HHMMSS.csv
```

## Key files

```text
app.py                         Main Streamlit app
batch_csv.py                   Batch Missing/Suspected CSV exporter
batch_pdf.py                   Batch value-report PDF generator
realpage_live_api.py           RealPage live/API helper logic
tests/test_app_ui_regressions.py Focused regression tests for UI/lift behavior
sql/                           Reference SQL and analysis queries
README.md                      This documentation
.env                           Local credentials; gitignored; do not commit
```

## Validation commands

```bash
python -m pytest tests/test_app_ui_regressions.py -q
python -m py_compile app.py batch_csv.py realpage_live_api.py batch_pdf.py
```

## Known behaviors / caveats

- Snowflake login can trigger Duo/MFA.
- Some selected properties may be skipped if they lack usable integration/source data.
- API-backed systems can timeout or return no-access responses for individual properties.
- Selected charge codes matter: the reports only treat those codes as pet rent / pet fees.
- RealPage scheduled-charge data may not contain every historical/ledger payment scenario.
