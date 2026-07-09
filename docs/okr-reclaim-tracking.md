# Tracking the reclaim-rate OKR

Context: solutions org H2 OKR — *"Increase PMC pet revenue reclaim rate on
abandoned ESA requests from [baseline]% to [X]% by EoQ4"*, plus the broader
"reclaim lost pet revenue from the obvious cases."

Answer to "how difficult is it to track": **it's now built.** The value
report flags individual tenants (name, email, property, reason, estimated
fee); as of July 6 every report run persists that list as a dated cohort.
Reclaim rate is then a straight cohort comparison.

## The KPI

```
net reclaim rate = reclaimed / (baseline cohort − moved out)
```

Per baseline-flagged tenant, the next measurement classifies them as:

| Class | Meaning |
|---|---|
| `reclaimed` | no longer flagged AND a pet charge now exists for them |
| `still_flagged` | flagged again in the latest run |
| `moved_out` | absent from the latest charge data (excluded from denominator) |
| `unclear` | present but no pet-charge match — verify manually (small) |

Reasons are tracked per tenant, so the ESA-specific OKR reads directly
from its own bucket, separate from `missing_pet_rent` (profile-no-rent) and
"Abandoned household profile":

- **"Unresolved assistance request"** — the suspected-report bucket covering
  assistance profiles in draft/non-responsive/declined/not-recommended/
  returned status. This is the AA-team attribution signal.
- **`assistance_non_responsive`** — historical only. A July 2026 experiment
  briefly flagged non-responsive ESA inside the missing-pet-rent report under
  this reason before being reverted; cohorts saved during that window still
  carry it, so the report keeps grouping it.

Estimated $/mo reclaimed comes from the fee attached at flag time.

## Mechanics

- **Cohorts**: every *Generate Report* (app) or `batch_pdf.py` run writes
  the flagged-tenant list to `CACHED_PET_VALUE_DATA`
  (`PMC_SYSTEM='_flags'`, one `flag` row per tenant + a `flag_run` marker
  listing the properties covered).
- **Measurement**: `python reclaim_report.py` compares the earliest and
  latest cohort per label (`--baseline` / `--as-of` to pin quarter
  boundaries) against the newest cached charge rows, prints the KPI table
  per reason and overall, and `--csv` emits the per-tenant classification
  (which doubles as the follow-up work list).
- **Cadence**: the quarterly batch (`quarterly_run.sh`) establishes and
  refreshes cohorts for every parent automatically. Measuring quarterly
  matches the OKR cycle; monthly runs give an early-warning trend.

## Setting the baseline

Run the quarterly batch across the target parents **once, now** — that
snapshot is the H2 baseline. Q4's run then produces
`reclaim_report.py --baseline 2026-07-01 --as-of 2026-12-31`.

Suggested KPI hygiene when the number is reported:
- Quote the **net** rate (moved-outs excluded) and show gross alongside.
- Report ESA ("Unresolved assistance request") separately — that's the AA
  workstream's number and its cohort is cleaner.
- The `unclear` bucket should stay small (<5%); if it grows, charge-code
  selection drifted between runs.

## Caveats (worth stating up front to Strait)

1. **Attribution**: reclaim measures outcome, not cause — a tenant may
   start paying because of the PMC's own audit. That's fine for an
   outcome OKR; it's the same number the client cares about.
2. **RealPage** cohorts are current-state accurate (live roster filtering)
   but a flagged tenant who moves out mid-quarter lands in `moved_out`,
   not failure — the denominator handles it.
3. **Coverage**: a property only enters measurement when a report is run
   against it. The quarterly batch makes coverage systematic.

## Usage tracking (the "is this being used" half)

Every fetch and report already lands in the same table, so usage is one
query away — no new instrumentation:

```sql
-- Report generations per week, by selection label
SELECT DATE_TRUNC('week', FETCHED_AT)::DATE AS week,
       PROPERTY_ID AS label_slug, COUNT(*) AS reports
FROM RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA
WHERE PMC_SYSTEM = '_report' AND RECORD_TYPE = 'snapshot'
GROUP BY 1, 2 ORDER BY 1 DESC;

-- Properties fetched per week, by source system
SELECT DATE_TRUNC('week', FETCHED_AT)::DATE AS week, PMC_SYSTEM,
       COUNT(DISTINCT PROPERTY_ID) AS properties, COUNT(*) AS fetches
FROM RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA
WHERE RECORD_TYPE = 'meta'
GROUP BY 1, 2 ORDER BY 1 DESC;

-- Cohort sizes over time (the OKR input)
SELECT FETCHED_AT::DATE AS run_date, DATA:label::STRING AS label,
       DATA:n_flags::INT AS flagged,
       DATA:reason_counts AS by_reason
FROM RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA
WHERE PMC_SYSTEM = '_flags' AND RECORD_TYPE = 'flag_run'
ORDER BY 1 DESC;
```

These three queries are dashboard-ready (Sigma/Tableau/Hex) if the
solutions org wants the KPI on a wall.
