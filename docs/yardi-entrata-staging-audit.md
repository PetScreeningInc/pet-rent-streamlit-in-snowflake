# Yardi & Entrata: API vs Snowflake Staging Audit

**Date:** 2026-07-05
**Method:** live API pulls compared row-by-row against the data team's
staging tables, plus global coverage/freshness queries. Companion to
[realpage-accuracy-audit.md](realpage-accuracy-audit.md).

Tables audited:
- `PROD.STAGING.STG_PMC_INTEGRATIONS_YARDI__GETRENTROLL`
- `PROD.STAGING.STG_PMC_INTEGRATIONS_ENTRATA__GETLEASES`

---

## TL;DR

| | Yardi staging | Entrata staging |
|---|---|---|
| Freshness | 🔴 **Dead since 2025-11-18** (~8 months) | 🟢 Nightly; 4,018 of 4,343 props fresh ≤ 7d |
| Property coverage | 🔴 4,829 of 11,330 integrated (43%) | 🟢 4,343 of 4,404 (98.6%) |
| Lease/tenant coverage (sampled) | 🔴 264 tenants vs 429 in API today | 🟢 Exact match (267/267, 172/172) |
| Charge content | 🔴 No charge end-date column; garbage dates | 🟡 Current lease interval only — no history |
| Verdict | **Unusable** — keep calling the API | Good for *current-state*; **cannot** support before/after lift |

---

## Yardi — the staging table is abandoned

1. **Ingestion stopped 2025-11-18.** All 4,829 properties are > 90 days
   stale (ingestion window was 2025-09-10 → 2025-11-18 — it was a one-time
   backfill, not a pipeline).
2. **Coverage was never complete**: 4,829 properties vs **11,330** active
   integrated Yardi properties in `D_PROPERTIES` (43%).
3. **Schema can't support our math**: there is `CHARGE_FROM_DATE` but **no
   charge end date** (only `LEASE_TO_DATE`). Without ToDate you can't tell
   an expired charge from an ongoing one — the revenue spreading and
   missing-rent logic both need it. The GetRentroll API returns it; the
   table dropped it.
4. **Data quality**: `CHARGE_FROM_DATE` ranges from year 2000 to **year
   6065** (unvalidated date parsing).

**Concrete example (property_id 1575, pulled live 2026-07-05):**

| | Staging (2025-09-11 snapshot) | API today |
|---|---|---|
| Charge rows | 673 | 1,434 |
| Tenants | 264 | 429 |
| Pet-paying tenants | 42 | 70 |

Of today's 70 pet-paying tenants, **32 (46%) don't exist in staging at
all** — they started paying after the snapshot. Any report built on this
table undercounts pet revenue by roughly half at this property.

**Ask for the data team:** either revive the GetRentroll pipeline with (a)
full property coverage, (b) the charge ToDate field, (c) date validation —
or confirm it's deprecated so nobody builds on it.

**Side finding:** the Yardi API answered successfully on a **Saturday**
for this property — the "weekdays 9–6 only" restriction is evidently
per-PMC/server, not universal. The batch scripts' window deferral has an
`--ignore-yardi-window` override for runs where the servers allow it.

---

## Entrata — trustworthy for "now", structurally missing history

The good news first — on 2 sampled properties (JVM Apex on Quality Hill,
GWR Memorial Fountain):

- **Lease coverage matches the API exactly** (267/267 and 172/172 distinct
  lease ids, zero drift either way).
- Pet-charge agreement is close: staging had 31 vs API 33, and 9 vs 10
  pet leases (the deltas are explained below).
- Ingestion is nightly and property coverage is 98.6%.

The structural gap: **`SCHEDULED_CHARGES_ARRAY` holds only the CURRENT
lease interval's charges, with no `endDate` on most entries** (421 of
1,776 sampled entries had one). The API returns charges for *all* lease
intervals (prior lease years + renewal).

**Concrete example (lease 14111811, Apex on Quality Hill):** family of
three, PET RENT $25/mo.

- API returns the charge on **two intervals**: 2025-06-10 → 2026-06-09
  (previous lease year) and 2026-06-10 → 2027-06-09 (renewal).
- Staging has **only** the renewal interval (`leaseIntervalId 18585401`,
  startDate 2026-06-10, no endDate).

So a revenue-history query against staging would show this family's pet
rent starting June 2026, when they've actually paid since June 2025. The
2 "API-only" pet leases per property are the same effect (pet charge lives
on a non-current interval) plus normal nightly lag.

**Implication:** Entrata staging is fine for current-state questions (who
pays pet rent today) but **cannot reconstruct pre-PetScreening baselines or
lift** — the same class of limitation as RealPage's live snapshot. For the
value report we must keep pulling lease history from the API (or the data
team would need to ingest all lease intervals, not just current).

**Ask for the data team:** confirm whether `SCHEDULED_CHARGES_ARRAY` is
intentionally current-interval-only, and whether historical intervals could
be added (the getLeases response already contains them).

---

## What this means for the app

- Keep the **live API** as the source of truth for Yardi and Entrata (the
  app already does this) — now with the per-property cache appending every
  pull to `CACHED_PET_VALUE_DATA`, we are building our own longitudinal
  record with known provenance.
- These findings mirror RealPage: every "historical" question ultimately
  needs either (a) an API that returns history (Entrata does; RealPage
  needs `getresidentledger` authorized; Yardi GetRentroll returns full
  charge date ranges), or (b) a snapshot pipeline that actually runs and
  retains intervals.
