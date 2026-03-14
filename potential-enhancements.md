# Pet Rent App — Potential Enhancements
**Last Updated:** 2026-03-14

---

## 🔴 Priority 1 — Data Trust & Consistency

### 1. Standardize Property Count Across All Tabs
**Problem:** The "X of Y properties" metric uses different denominators depending on the tab (Charts vs What's Next vs Report). A client sees "32 of 36" in one place and "32 of 44" in another with no explanation.

**Enhancement:** Add a visible property cascade/funnel at the top of each tab:
```
41 Total → 36 with API access → 33 with pet charges → 29 with adoption data → 22 comparable
```
One line, always visible, no ambiguity about what each number means.

---

### 2. Fix Report Tab Tenant Count Gap
**Problem:** The Missing Pet Rent Report tab joins against `R_MONTHLY_EXECUTIVE_SUMMARY`, which can lag behind `petscreening__user_enriched`. Tenants identified in Charts/What's Next may not appear in the downloadable report.

**Enhancement (option A):** Build the report directly from profile data (Steps 2-3) instead of depending on the exec summary table. Add pet details via a join to `d_pet_profiles` / `f_user_pets`.

**Enhancement (option B):** Add a caption: _"Report shows X of Y identified tenants. Z tenants are pending data refresh."_

---

### 3. Add Data Freshness Indicators
**Problem:** No timestamp on when API data was fetched, when Snowflake data was last refreshed, or whether QBR adoption data is current or months old.

**Enhancement:** Add a metadata footer to every exported report and a collapsed info block in-app:
```
API data fetched: March 14, 2026 at 2:34 PM EST
Snowflake profiles: Last refreshed March 13, 2026
QBR adoption data: Through February 2026
```

---

### 4. Surface QBR Adoption Data Staleness
**Problem:** The QBR table (`R_QUARTERLY_BUSINESS_REVIEW_REPORTING`) has a `PERIOD_MONTH` field, but the app doesn't show when that data was last updated. If QBR hasn't refreshed in 2 months, adoption % is stale but displayed as "current."

**Enhancement:** Show the max `PERIOD_MONTH` per property or overall and flag if it's > 1 month old.

---

### 5. Use Median Instead of Mean for Fee Estimation
**Problem:** Missing rent estimation uses `mean` for per-property charge averages. Pet charge distributions are often skewed — a few $300 deposits + many $35/mo rents gives a misleading mean.

**Enhancement:** Switch to median for charge estimation, or compute per-tenant averages first then average across tenants.

---

## 🟡 Priority 2 — Consistency & Completeness

### 6. Extract Shared Aggregation Function (Charts ↔ What's Next)
**Problem:** The What's Next tab independently rebuilds ~200 lines of aggregation logic (monthly_by_prop, launch_analysis, charge classification, etc.) instead of reusing Charts tab data. Any future change to one that isn't mirrored in the other will silently diverge.

**Enhancement:** Extract core aggregation into a shared `@st.cache_data` function called once, stored in session state, and read by both tabs.

---

### 7. Include Suspected Undisclosed Data in HTML Report Export
**Problem:** The Charts tab HTML report includes confirmed missing rent (orange bars) but not suspected undisclosed (red bars). If both toggles are on, the exported report doesn't match what the user sees on screen.

**Enhancement:** Pass `suspected_data` to `generate_html_report()` and include it.

---

### 8. Label Adoption Type on What's Next Tab and Exports
**Problem:** What's Next shows "Avg Adoption" without specifying unit vs resident. The adoption type is silently coupled to the Charts tab radio button.

**Enhancement:** Always display the full label: "Avg **Unit** Adoption" or "Avg **Resident** Adoption" on What's Next and in all exported reports.

---

### 9. Prevent Stale Report Generation
**Problem:** HTML report captures data at the moment "Generate Report" is clicked. If the user changes overlay, slider, or toggles between generating Charts and What's Next reports, the two exports can disagree.

**Enhancement:** Either regenerate both reports simultaneously, or stamp each export with the settings that were active when it was generated.

---

### 10. Fix CSV Export Formatting
**Problem:** CSV exports contain `$` signs and commas in numeric columns (e.g. `$5,000.00`), which breaks Excel sorting and filtering.

**Enhancement:**
- CSV exports: raw numbers (no `$`, no commas) — let Excel handle formatting
- In-app/HTML: `$X,XXX` consistently (no decimals for KPIs, two decimals for detail tables)

---

## 🟢 Priority 3 — Trust-Building Features

### 11. Add a Data Health Check Panel
**Problem:** No automated sanity checks before presenting results. A data-savvy client or internal reviewer has no quick way to assess data quality.

**Enhancement:** Before showing results, run quick checks and surface a ✅/⚠️/❌ health panel:
- Properties with $0 revenue in ALL months?
- Tenants appearing in multiple properties simultaneously?
- Charge codes with suspiciously high duplicate counts?
- Launch dates in the future or before 2018?
- Properties with charge data but no launch date?

Clients see "7 of 7 checks passed" and trust goes up immediately.

---

### 12. Add Confidence Levels to KPIs
**Problem:** Flat numbers without context invite the question "how confident are you?"

**Enhancement:** Show confidence alongside each KPI:

| KPI | Value | Confidence |
|-----|-------|-----------|
| Revenue Change | +$12,400/mo | 🟢 High (22 of 24 properties with 3+ month baseline) |
| Projected at 100% | $47,000/mo | 🟡 Medium (linear extrapolation) |
| Uncollected Pet Rent | $8,200/mo | 🟡 Medium (based on avg fee estimation) |

---

### 13. Add "What Changed Since Last Report" Comparison
**Problem:** Each report is a static snapshot. There's no way to show progress over time.

**Enhancement:** Save a JSON snapshot of key metrics per PMC per run. On the next run, load the previous snapshot and show deltas:
- Revenue went up $X
- 12 new tenants started paying
- 3 properties improved adoption by >10%
- Missing tenant count dropped by 15

This is the most powerful trust-builder for repeat clients — proves the data is alive and tracked.

---

### 14. Include SQL Queries in Exported Reports
**Problem:** Data-savvy clients and internal teams can't independently verify numbers from the export alone.

**Enhancement:** Add a collapsible appendix to HTML reports with the actual Snowflake SQL used for profile identification, adoption data, and paying set logic. The Documentation tab already has this — just pipe it into exports.

---

### 15. Separate Organic Growth from PetScreening-Driven Lift
**Problem:** The lift calculation attributes ALL post-launch revenue increases to PetScreening. If pet rent was already trending up before launch, the number is inflated.

**Enhancement options:**
- Calculate pre-launch trend slope and project it forward — subtract the projected organic growth from the actual post-launch growth
- Show a "trend-adjusted lift" alongside the raw lift
- At minimum, add a disclaimer when pre-launch trend was positive

---

## 🔧 Priority 4 — Technical Debt

### 16. Split the 8,100-Line Single File
**Problem:** Everything lives in one `app.py`. Finding anything requires grep. Testing is impossible.

**Enhancement:** Split into modules:
- `snowflake_queries.py` — all Snowflake query functions
- `api_clients.py` — Yardi SOAP + Entrata REST
- `charts.py` — Plotly chart builders
- `reports.py` — HTML/PDF report generation
- `analysis.py` — lift calculations, paying set logic, missing rent
- `app.py` — Streamlit UI orchestration only

---

### 17. Add Unit Tests for Core Matching Logic
**Problem:** Zero test coverage. The `_build_paying_sets`, `_apply_paying_flag`, `compute_launch_analysis`, and revenue aggregation functions are pure logic that could break silently during refactoring.

**Enhancement:** Extract pure functions and add pytest cases with known inputs/outputs. The HQ Asset Living validation work is effectively a manual integration test — encode it as automated tests.

---

### 18. Close Cursor Leaks
**Problem:** 6 functions open Snowflake cursors without closing them. `generate_missing_pet_rent_report` runs on demand and leaks on every call.

**Enhancement:** Use `with conn.cursor() as cur:` context manager everywhere.

---

### 19. Parameterize SQL Queries
**Problem:** Multiple functions build SQL via f-string interpolation of values that could theoretically be user-controlled.

**Enhancement:** Use parameterized queries (`cur.execute(sql, params)`) or validate against allowlists.

---

### 20. Replace Bare `except:` Catches
**Problem:** Lines 1474, 1578, 1723, 4982 have `except:` blocks that catch everything including `KeyboardInterrupt` and `SystemExit`, silently skipping bad data with no logging.

**Enhancement:** Change to `except (ValueError, TypeError, AttributeError):` or at minimum `except Exception:` with logging.

---

### 21. Vectorize Lambda Apply Operations
**Problem:** `.apply(lambda, axis=1)` is used on large DataFrames for paying flag checks. This is a Python-level row loop — very slow on 10K+ rows.

**Enhancement:** Use vectorized merge/join:
```python
exec_df['_lookup_key'] = list(zip(
    exec_df['USER_EMAIL'].str.lower().str.strip(),
    exec_df['PROPERTY_ID'].astype(str)
))
report_df = exec_df[exec_df['_lookup_key'].isin(missing_lookup)]
```

---

### 22. Add API Retry Logic
**Problem:** If a Yardi/Entrata API call times out or returns a transient error, it's logged and skipped with no retry.

**Enhancement:** Add 1-2 retries with exponential backoff for timeout/5xx errors.

---

### 23. Parallelize Yardi API Calls
**Problem:** `fetch_rentroll_for_properties()` calls each property's SOAP endpoint sequentially. For a PMC with 50+ properties, this is slow.

**Enhancement:** Use `ThreadPoolExecutor` (already imported) with 3-5 workers. SOAP endpoints are independent.

---

### 24. Define Session State Key Registry
**Problem:** Session state keys (`missing_rent_{hash}`, `suspected_{hash}`, `chart_data`, `pm_emails_cache`, etc.) are scattered as magic strings. If a key name changes in one place but not another, data silently disappears.

**Enhancement:** Define a `SessionKeys` class/enum with all keys. Reference by name, not string.

---

## Open Questions (Need Team Input)

### A. How Should We Calculate Monthly Lift?
| Approach | Post Comparison | Pros | Cons |
|----------|----------------|------|------|
| Current | Avg of ALL post months | Smooths volatility | Early ramp-up drags down the number |
| Alternative | Most recent completed month | Shows current state | Single month can be noisy |

### B. How to Handle Organic Rent Increases?
If pet rent was already trending up before PetScreening launched, the current method attributes all growth to PS. Options:
- Trend-adjusted baseline (project pre-launch slope forward)
- Disclaimer only
- Show both raw and adjusted lift

---

*Source: holistic-audit-2026-03-14.md, code-review-2026-03-14.md, cross-tab-audit.md, yardi-validation-findings.md*
