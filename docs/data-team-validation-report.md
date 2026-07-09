# PMS staging tables vs the live APIs — what I found

Eduardo Salvador — July 6, 2026

For the pet value report I've been pulling Yardi, Entrata, and RealPage
data straight from the PMS APIs and comparing it against our staging
tables. This is the write-up of what matched, what didn't, and the
specific rows behind each claim. Every finding has a repro query — please
check my work.

Short version: **Entrata is in good shape and I'd like us to certify it.
Yardi and RealPage staging are effectively dead and anything built on them
is reading September 2025.**

---

## Entrata — `STG_PMC_INTEGRATIONS_ENTRATA__GETLEASES`

This one is close to trustworthy, and it's the pool I'd like to promote to
"certified for current-state" once the two gaps below are addressed.

**What checked out.** I pulled two properties live from the getLeases API
and diffed against staging: lease coverage matched exactly — 267/267
distinct lease ids at JVM's Apex on Quality Hill (property 1576) and
172/172 at GWR's Memorial Fountain (2537), zero drift in either direction.
Pet scheduled charges agreed within nightly lag. Ingestion is running
nightly (4,018 of 4,343 staged properties touched in the last 7 days).

**Gap 1 — only the current lease interval is kept.** The API returns
scheduled charges for every lease interval; `SCHEDULED_CHARGES_ARRAY`
keeps just the current one, and most entries have no `endDate` (421 of
1,776 sampled entries had one). Concrete example: lease `14111798` at
property 1576 — the API shows PET RENT $25/mo on two intervals
(2025-06-10 → 2026-06-09, then the renewal 2026-06-10 → 2027-06-09);
staging has only the renewal (`leaseIntervalId 18585401`). Anyone
computing revenue history from this table will think this family started
paying pet rent a year later than they did. If the ingestion could keep
all intervals from the same response we already fetch, this table would
support before/after analysis, not just current state.

**Gap 2 — 235 active, integration-enabled properties have no rows at
all.** Examples: `25042` Greystar – Hornet Commons, `5996` Brookview
Columbus, and what looks like the whole Broadhouse portfolio (`25078`,
`25079`, `25081`, `25082`, `25084`, `25089`, ...). Repro:

```sql
WITH stg AS (SELECT DISTINCT PROPERTY_ID
             FROM PROD.STAGING.STG_PMC_INTEGRATIONS_ENTRATA__GETLEASES)
SELECT p.property_id, p.property_name, p.parent_company_name
FROM PROD.COMMON.D_PROPERTIES p
LEFT JOIN stg ON stg.PROPERTY_ID = p.property_id
WHERE p.property_source_name = 'entrata'
  AND p.property_status = 'active' AND p.integration_status = 'enabled'
  AND stg.PROPERTY_ID IS NULL;
```

---

## Yardi — `STG_PMC_INTEGRATIONS_YARDI__GETRENTROLL`

I don't think anyone should query this table without knowing the
following.

**1. It was two backfill bursts, not a pipeline.** All 3.7M rows were
ingested in exactly two windows: September 2025 (3.64M rows, 3,974
properties) and November 2025 (58K rows, 1,251 properties). Nothing
since.

```sql
SELECT DATE_TRUNC('month', INGESTED_AT)::DATE, COUNT(*),
       COUNT(DISTINCT PROPERTY_ID_METADATA)
FROM PROD.STAGING.STG_PMC_INTEGRATIONS_YARDI__GETRENTROLL
GROUP BY 1 ORDER BY 1;
-- 2025-09: 3,643,742 rows, 3,974 props
-- 2025-11:    57,558 rows, 1,251 props
```

**2. Coverage was never close.** 4,829 properties ever staged vs 11,330
active integration-enabled Yardi properties in `D_PROPERTIES` (43%).

**3. What that staleness means in practice.** Property 1575 (NRES —
Sovereign at Overland Park): staging knows 42 pet-rent payers; a live
GetRentroll call today returns 70. 32 of today's payers — 46% — don't
exist in the table.

**4. The schema drops the charge end date.** There's `CHARGE_FROM_DATE`
but no charge-level ToDate (only `LEASE_TO_DATE`), so an expired charge
and an ongoing one are indistinguishable. The API returns ToDate; it's
being dropped at ingestion.

**5. Dates aren't validated.** 1,566 rows have `CHARGE_FROM_DATE` before
2005 or after 2030, including the year 6065 (Alta Oceanside, tenant
`t3117314`, code `parking`) and 5025 (Elea Apartments, `t0909351`,
`psub`). These are Yardi fat-finger entries that a plausibility check
should quarantine.

---

## RealPage — `STG_PMC_INTEGRATIONS_REALPAGE__GETSCHEDULEDCHARGES`

**The EKS job has effectively stopped.** 6,580 of 6,599 (pmc_id, site_id)
pairs — 99.7% — were last ingested more than three weeks ago, and the
bulk of them on **2025-09-10**. A handful still update (max last
ingestion 2026-06-28), so the job isn't fully dead, which is arguably
worse: it looks alive from a distance.

```sql
WITH per_prop AS (
    SELECT pmc_id, site_id, MAX(ingested_at)::DATE AS last_ing
    FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETSCHEDULEDCHARGES
    GROUP BY 1, 2
)
SELECT SUM(IFF(last_ing < DATEADD('day', -21, CURRENT_DATE), 1, 0)),
       COUNT(*), MIN(last_ing), MAX(last_ing)
FROM per_prop;   -- 6,580 stale / 6,599 total, min 2025-09-10
```

Two things we proved about the API itself that the job design should
account for (full detail in `docs/realpage-accuracy-audit.md`):

- `getscheduledcharges` **ignores its postmonth parameter** — five
  different values, including a nonsense string, returned byte-identical
  responses. The `MONTHS_BACK` setting in the job does nothing except
  create duplicate snapshots.
- Former residents are unreachable through this endpoint
  (`residentstatus=F` returns zero rows). The ledger operation that would
  fix this (`getresidentledger`) exists on our gateway contract but the
  license key isn't authorized — we've asked RealPage.

There are also properties with rows that produce zero usable output
because every charge fails the resident join or has no usable email —
e.g. 180 Multi Family's 3 Thousand One Crystal Springs (60 post_months in
the table, 0 joinable rows) and AHC Texas's The Collection Townhomes.

---

## What the app does in the meantime

The value report now calls all three APIs live and doesn't read the Yardi
or RealPage staging tables at all. Every pull is appended to
`RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA` (one row per charge,
plus fetch metadata), so we're accumulating our own dated record of what
the APIs actually returned — that table is open for anyone to query and
might be useful as a reference while the pipelines get sorted.

## Asks

1. **Entrata** (small): keep all lease intervals from the getLeases
   response, and chase the 235 missing properties (Broadhouse looks
   systematic). With those two fixed I'd call this table certified for
   current-state use.
2. **Yardi**: decide whether the GetRentroll pipeline is alive or
   deprecated. If it's revived: full property coverage, keep the charge
   ToDate, add a date plausibility check.
3. **RealPage**: the job needs a restart and an alert on ingestion lag;
   drop the `MONTHS_BACK` looping (proven no-op); and when RealPage
   authorizes `getresidentledger`, that's the endpoint worth ingesting.
4. **Cross-cutting**: a freshness monitor on all three tables (max
   ingested_at per property, alert past N days). Both dead pipelines
   failed silently for months.
