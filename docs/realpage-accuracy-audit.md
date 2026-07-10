# RealPage Accuracy Audit — Staging vs Live API

**Date:** 2026-07-05
**Method:** Controlled experiments against the production OneSite SOAP gateway
(`Pet_Screening.svc`) plus a side-by-side comparison of both fetch paths on
4 properties across different parent companies.

---

## Question 1: Why can't we pull historical residents from the live API?

**Answer: the API is structurally incapable of it.** Proven empirically on
Greystar — Cadence Music Factory (property_id 694, 6 SOAP calls):

| Experiment | Result |
|---|---|
| `getscheduledcharges` with `postmonth` = current, −6mo, −12mo | **Byte-identical data** (895 rows / 171 leases each time). The response echoes back whatever postmonth you send, which makes it *look* honored. It is ignored. |
| `getscheduledcharges` with `residentstatus=F` (Former) | **0 rows.** |
| Resident lists | 187 Current leases, **1,300 Former leases** |
| Former leases appearing in the charges pull | **0 of 1,300** |

So `getscheduledcharges` returns only the current snapshot of scheduled
charges for current residents. The `residentheldthestatus=3650` /
`residentwillhavestatus=365` parameters do not change this.

**Why we "still get some past residents":** two mechanisms, neither of which
is true historical coverage:
1. The live fetcher expands each current charge across its BillStart→BillEnd
   window, so long-running charges produce months of synthetic history, and
   current residents with a move-out date on file look like past residents.
2. The Snowflake staging tables accumulate daily snapshots — anyone who was
   current at any point since the EKS job started ingesting exists there.

**Ledger endpoint — CONFIRMED TO EXIST (2026-07-05 probe):** the gateway's
WSDL metadata is disabled, but probing 21 candidate operations against the
service found the contract includes more than the two operations we use:

| Operation | Status for our license key |
|---|---|
| `getscheduledcharges` | ✅ authorized (in use) |
| `getresidentlistinfo` | ✅ authorized (in use) |
| `getresidentlist` | ✅ **authorized, unused** — ONE call returns ALL residents (Current + Former + Applicants; 2,129 records incl. 1,338 Former on the test property) with balance fields (`balance`, `pendingbalance`, `estfutchgs`, `curlateamt`) |
| `getresidentledger` | 🔒 exists, but **"LicenseKey is not authorized to call the method"** |
| `getunitlist` | 🔒 exists, not authorized |
| getledger / gettransactions / getrentroll / 12 others | ❌ not on the contract |

**Action: ask RealPage partner support to authorize `getresidentledger`
(and ideally `getunitlist`) for the Pet_Screening license key.** A resident
ledger is the direct path to true per-month payment history — including
former residents — which would make RealPage lift baselines fully accurate.
Until then, the hybrid fetch below is the best available.

Secondary win available now: switching the live fetcher from 3×
`getresidentlistinfo` calls to 1× `getresidentlist` would halve SOAP calls
per property and add balance columns.

---

## Question 2: Which source is more accurate?

**Neither alone.** Side-by-side on 4 properties (2026-07-05):

| Property | Staging | Live | Verdict |
|---|---|---|---|
| 50 Rector Park (Boraie) | 60 months history (2021-06→2026-05), 151 leases incl. Past, **ingestion stopped 2026-05-31** | 22 months synthetic, 139 leases, 109 leases staging lacks | Staging wins pre-June; live wins June onward |
| 3 Thousand One Crystal Springs (180 MF) | Table has 60 post_months **but 0 usable rows** (resident join/email failures) | 108 leases, 17 paying pet, $430/mo | Live only usable source |
| The Collection Townhomes (AHC Texas) | 8 post_months in table, **0 usable rows** | 254 leases, 82 paying pet, $3,132/mo | Live only usable source |
| 156.1 Stone Ridge - Acctg | No rows | SOAP faults | Dead sub-account property — should be excluded upstream |

Revenue divergence example (50 Rector Park, pet codes):

| Month | Staging | Live |
|---|---|---|
| 2025-11 | **$475** | $100 |
| 2026-03 | $107 | $275 |
| 2026-06 | $0 (stale) | **$350** |

- Staging's Nov-2025 snapshot saw payers whose charges have since ended —
  live's BillStart expansion cannot see them (understates history).
- Staging shows $0 for June 2026 because ingestion stopped May 31
  (understates current).
- Mid-window disagreements (2026-01→05) are staging-observed snapshots vs
  live back-projection; the observed snapshot is the defensible number.

Match-type stats confirm the lease-fallback design: direct ReshID matches
were 2/241 and 0/604 on the live path — the lease-id join carries everything.

---

## Decision history

**2026-07-05 (final): current-state only.** The hybrid fetch below was
implemented and then retired the same day: staging has its own integrity
problems (stale ingestion, properties with zero usable rows) and mixing
sources muddied the story. RealPage now runs live-API-only with rows
emitted for the current month, meaning: current collected revenue +
missing/suspected reports (filtered against the live `getresidentlist`
roster), and **no lift attribution** until `getresidentledger` is
authorized. The hybrid code remains in app.py unexposed for reference.

## Superseded: hybrid fetch (retired 2026-07-05)

`fetch_realpage_hybrid()` in app.py, now the **default** RealPage mode in
the UI (sidebar radio: hybrid / live-only / staging-only):

- For each property+month, an **observed staging snapshot wins**.
- **Live fills** months staging never observed: the most recent weeks,
  future scheduled charges, and entire properties with no usable staging
  rows.
- Live rows for staging-covered months are dropped (synthetic
  back-projection, not observations).
- The fetch log reports the split per property
  (`N staging history + M live fill`).

## Remaining risks / follow-ups

1. **Staging ingestion health** is invisible from inside the app. The audit
   found per-property staleness (5 weeks) and whole properties with rows but
   zero usable charges. Worth a standing data-quality check with the data
   team: max `ingested_at` per property, and % of charge rows that fail the
   resident join.
2. **Pre-launch baselines for RealPage remain weak**: staging history begins
   when the EKS job first ingested a property (often post-launch), so lift
   for RealPage properties frequently rests on the `data_starts_after_launch`
   guard in `compute_launch_analysis` (correctly excluded from lift
   attribution). True pre-PS baselines need the ledger endpoint (see above).
3. **The missing-rent "current resident" EXISTS clause** reads the staging
   resident list, which inherits the same staleness. Acceptable for now
   (residents change slower than charges), but worth revisiting if staging
   lag grows.
