# external-integrations Pipeline Review — API vs Stored-Procedure Discrepancies

**Date:** 2026-07-09
**Scope:** read-only review of `PetScreeningInc/external-integrations`
(jobs + `sql/snowflake` stored procedures) against this repo's live-API
logic (the validated reference). Companion to
[yardi-entrata-staging-audit.md](yardi-entrata-staging-audit.md) and
[realpage-accuracy-audit.md](realpage-accuracy-audit.md), which documented
the *data-level* symptoms; this review pinpoints the *code-level* causes.
No changes were made to external-integrations.

All pipeline paths below are relative to the external-integrations repo.

---

## TL;DR

| System | Root cause | Fix size |
|---|---|---|
| **Entrata** | Request sends `includeLeaseHistory: 0` — the API is told not to return prior/renewal lease intervals, so `SCHEDULED_CHARGES_ARRAY` can only ever hold current-interval charges | **One-line flag flip** in `jobs/entrata/getleases/request.py` |
| **Yardi** | SOAP request sets all four date params to the same single date (4 years ago) — the charge window collapses to one day; plus no scheduler, a `_TEST_2` target table, and no charge `ToDate` column downstream | Small request fix + ops/schema work |
| **RealPage** | Envelope matches ours; problems are a fabricated 60-month "history" (the API ignores `postmonth`), silent SOAP-fault swallowing on charges, and a non-deterministic fabricated `lease_id` on residents | Three small targeted fixes; true history stays blocked on `getresidentledger` |

---

## Entrata (`jobs/entrata/getleases/`)

The pipeline stores each lease object **verbatim** as `RAW_JSON` (VARIANT):
`request.py` keeps the whole lease dict, `s3_writer.py` serializes it, and
`sp_load_entrata_getleases.sql:90` does a plain `PARSE_JSON` — no field
selection anywhere. So the missing scheduled charges are **not** lost in
parsing or SQL. They are never requested.

### E1 — CRITICAL: `includeLeaseHistory: 0` (request.py:179)

| Param | Pipeline (`request.py:170-183`) | pet-rent (`app.py:1390-1404`) |
|---|---|---|
| `includeLeaseHistory` | **0** | **1** |
| `includeScheduledCharges` | absent | 1 |
| `includeArTransactions` | 0 | 1 |
| `leaseStatusTypeIds` | `"1,2,3,4,5,6"` (env-overridable) | `"1,2,3,4,5,6"` hardcoded |

With `includeLeaseHistory: 0` Entrata returns only the current lease
interval and its charges. This exactly reproduces the audited case (lease
14111811: staging had only renewal interval 18585401; the API shows the
$25/mo PET RENT on the 2025–26 interval too).

**Fix:** in `jobs/entrata/getleases/request.py` set
`"includeLeaseHistory": 1` and add `"includeScheduledCharges": 1`. Because
the rest of the pipeline is payload-agnostic, the staging table picks up
full interval history with **no other change**.

### E2 — HIGH: missing `endDate` is the same defect

Only current-interval charges (open-ended, no `endDate`) are returned;
the historical charges that carry real `endDate`s never arrive. E1's fix
resolves this too — nothing in parsing/SQL strips the field.

### E3 — HIGH: AR transactions never ingested

`includeArTransactions: 0` means scheduled-vs-actual analysis is
impossible from staging. If in scope: flip the flag and add a landing
column/table (`entrata_tables.sql` has none). Reference parser:
`app.py` `_extract_ar_charges_from_entrata_lease` (~line 1317).

### E4 — MEDIUM (latent): `ENTRATA_LEASE_STATUS_TYPE_IDS` env override

`config.py:8` defaults to all six statuses but is env-overridable — if
prod sets it narrower, lease-level history is silently filtered. Verify
the deployed value.

*(Pagination is fine — the pipeline fully paginates via `meta`, though it
trusts Entrata's meta fields which we found unreliable; our short-page
heuristic is more defensive.)*

---

## Yardi (`jobs/yardi/getrentroll/`)

Also a raw-JSON-dump pipeline: the whole `<Property>` node lands in
`RAW_JSON`; no charge fields are promoted to columns in this repo.

### Y1 — CRITICAL: degenerate date window (request.py:19-22, 41-44)

The request computes ONE value, `four_years_ago`, and uses it for **all
four** date params: `MoveIn`, `MoveOut`, `LeaseChgFrom`, **and**
`LeaseChgTo`. So the charge window is `From == To ==` a single day four
years in the past, and move dates exclude anyone outside that day.

Reference (`parallel_fetch.py:247-249`, same in `app.py:2478-2480`):
charges from `2000-01-01` → today, move dates 10 years back. This is the
direct code explanation for property 1575 having ~half the live
charge/tenant counts in staging.

**Fix:** `LeaseChgFrom = 2000-01-01`, `LeaseChgTo = today`,
`MoveIn`/`MoveOut = today − 10y`. (Endpoint, namespace, license handling
are identical to ours — only the dates differ.)

### Y2 — HIGH: charge `ToDate` never surfaced

The raw blob *does* contain `ToDate` (generic XML→JSON conversion keeps
it), but no layer promotes it: `yardi_tables.sql` has no charge columns
and the downstream flatten model (not in this repo) selects `FromDate`
only. Without `ToDate`, expired charges are indistinguishable from
ongoing ones. Fix belongs in the downstream flatten (select
`LeaseCharge:ToDate`, sibling of `FromDate`).

### Y3 — HIGH: no scheduler, `_TEST_2` table

`config.py:3` targets `YARDI_GETRENTROLL_TEST_2`; no cron/EventBridge/
workflow schedule exists anywhere in the repo (the GitHub workflow only
deploys the container). This matches "one-time backfill Sep–Nov 2025,
then dead." The 43% coverage is operational (partial manual runs +
silent failures), not a query filter — the driving SQL selects all
integrated Yardi properties.

### Y4 — MEDIUM: dates never validated (year-6065)

No `TRY_TO_DATE`/range check at any layer; Yardi's far-future sentinel
dates land verbatim. Add validation at the downstream cast.

### Y5 — MEDIUM: failures look like empty properties

Single attempt, 30s timeout, no retry (`request.py:60-67`); any parse or
HTTP error becomes an empty `raw_json` "success" row — indistinguishable
from a property with no charges. (The sibling `getresidentsbydate` job
has `_post_with_retries`; getrentroll doesn't.) Also, a run aborted
midway still COPYs a partial snapshot with no completeness guard.

---

## RealPage (`jobs/realpage/getscheduledcharges/`, `getresidentlistinfo/`)

Good news: SOAP envelopes, auth, and params match `realpage_live_api.py`
almost exactly, and both parsers keep the full raw objects — nothing we
extract is dropped at the raw layer. The issues are behavioral:

### R1 — HIGH: fabricated 60-month history

`getscheduledcharges/job_ingestion.py` loops `postmonth` over 60 months —
but the API **ignores** `postmonth` (verified in
realpage-accuracy-audit.md): every month returns the identical current
snapshot, stored under distinct `POST_MONTH` labels. Any trend built on
`POST_MONTH` shows today's charges as 60 months of flat "history" — the
exact fake-history trap this app retired. **Fix:** query once per run,
label with the run month. Real history stays blocked on
`getresidentledger` (do not synthesize).

### R2 — HIGH: charges path swallows SOAP faults

`getscheduledcharges/parser.py` just does `findall(".//ChargeData")` — a
fault body yields `[]` and the property loads as legitimately
charge-free. The residents parser DOES check `Fault`/error messages;
charges needs the same, plus a tracking table (residents has one,
charges doesn't).

### R3 — HIGH: non-deterministic fabricated `lease_id`

`getresidentlistinfo/job_ingestion.py:91-96`: residents without a
`leaseid` get `lease_{property_id}_{hash(str(record))}` — Python's
`hash()` is salted per process, so the same resident gets a different
LEASE_ID every run (breaks dedup and can never join to a charge). We
leave missing lease_id empty so it cleanly falls to no-match. **Fix:**
stable/empty fallback.

### R4 — MEDIUM: charges table isn't joinable to residents

The charges table stores the whole month's ChargeData array as a
**VARCHAR** `RAW_JSON` (never `PARSE_JSON`d, unlike residents) with no
`property_id`, and no extracted `LeaID`/`ReshID`/`UnitID` columns —
while residents are keyed by internal `PROPERTY_ID` (no pmc/site id).
The two tables share **no common column**. Our member-id + lease-id +
unit join (anti-misattribution) needs either extracted key columns +
`property_id` on charges, or a downstream FLATTEN view bridging
pmc/site→property_id via `D_PROPERTIES`.

### R5 — notes for downstream consumers (not pipeline bugs)

Raw layer keeps things we filter at emit time; any consumer must
replicate: `CatCode = 'C'` only (drop payments), drop lease statuses
Future/Applicant/Applicant-Lease Signed/Pending, junk-email/test-name
exclusion. Also: pipeline still uses 3× `getresidentlistinfo` (C/P/F)
where we moved to 1× `getresidentlist` (fewer calls + balance fields);
ids are silently truncated to 100 chars.

---

## Suggested asks for the data team (in priority order)

1. **Entrata:** flip `includeLeaseHistory` to 1 (+ add
   `includeScheduledCharges: 1`) in `jobs/entrata/getleases/request.py`.
   One flag; staging immediately gains full interval history + endDates.
2. **Yardi:** fix the four date params in
   `jobs/yardi/getrentroll/request.py` (charges 2000-01-01→today, moves
   −10y); add charge `ToDate` to the flatten; schedule the job off the
   `_TEST_2` table or declare it deprecated.
3. **RealPage:** stop the 60-month `postmonth` loop; add fault detection
   + tracking to the charges job; make the missing-lease_id fallback
   deterministic; extract join keys (`LeaID`, `ReshID`, `UnitID`,
   `property_id`) on the charges side.
