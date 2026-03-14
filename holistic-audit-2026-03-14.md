# Pet Rent App — Holistic Audit
**Date:** 2026-03-14  
**Scope:** Everything beyond the bugs, lift methodology, and charts-vs-reports already documented.  
**Goal:** Make this data trustworthy, consistent, and simple for internal teams + external clients.

---

## Executive Summary

The app is solid at its core — the matching logic, deduplication, unit/lease expansion, and Entrata handling are thoughtful and well-documented. But there are **trust-eroding inconsistencies** and **missing guardrails** that would concern a data-savvy client or internal stakeholder. Below is everything I found, organized by impact.

---

## 🔴 Trust Issues (Would Cause a Client to Question the Data)

### 1. The "Properties" Denominator Changes Without Explanation

A client looking at "32 of 36 properties" on one tab and "32 of 44 properties" on another will immediately lose trust. There's no visible explanation of what each denominator means.

**Where it happens:**
| Location | Denominator | Source |
|----------|------------|--------|
| Charts KPI — Launch Dates | Properties with charge data (`len(monthly_by_prop)`) | Only those with matching charge codes |
| Charts KPI — Properties Affected | ALL integrated properties (`len(property_ids)`) | Every property under the PMC |
| What's Next — Quick Stats | ALL integrated properties | Same |
| Report tab — Properties in scope | ALL integrated properties | Same |

**Fix:** Add a visual funnel/stepper at the top of each tab showing the cascade:
```
41 Total → 36 with API access → 33 with pet charges → 29 with adoption data → 22 comparable
```
One line. Always visible. No ambiguity.

### 2. Report Tab Tenant Count Can Be Lower Than Charts Tab (Silent Gap)

The Report tab joins against `R_MONTHLY_EXECUTIVE_SUMMARY` (Step 4-5), but Charts/What's Next don't. If a tenant exists in `petscreening__user_enriched` but hasn't been loaded into the exec summary table yet (refresh lag), they'll be counted as missing in Charts but **won't appear in the downloadable report**.

**Impact:** A PM downloads the report, counts 42 tenants. But the Charts tab said 47. "Where are the other 5?"

**Fix options:**
- **Best:** Build the report directly from the Step 2-3 profile data (drop the exec summary dependency). Add pet details via a separate join to `d_pet_profiles` or `f_user_pets`.
- **Quick:** Add a caption: _"Report shows X of Y identified tenants. Z tenants are pending data refresh and will appear in the next report."_

### 3. No Data Freshness Indicator Anywhere

Clients have no way to know:
- When the API data was fetched (it's not timestamped)
- When Snowflake data was last refreshed
- Whether the QBR adoption data is from last week or last quarter

**Fix:** Add a metadata footer to every exported report:
```
Data fetched: March 14, 2026 at 2:34 PM EST
Snowflake data: Last refreshed March 13, 2026
QBR adoption data: Through February 2026
```
Also show this in-app in the sidebar or a collapsed info block.

### 4. Avg Fee Estimation Uses Mean Instead of Median

The missing rent estimation uses `np.mean` for the per-property charge average. But pet charge distributions are often skewed — a few $300 deposits + many $35/mo rents gives a misleading mean.

**Example:** If 5 tenants pay $35/mo rent + $300 deposit, mean = $67.50. The true recurring cost per missing tenant is $35/mo. Using median or splitting by charge type (recurring only for monthly estimates) would be more accurate.

**Current state:** The code does split recurring vs one-time, which helps. But `avg_recurring_by_prop` still takes the mean of ALL recurring charge amounts across ALL tenants at the property — including amounts that may be per-charge-code rather than per-tenant.

**Fix:** Use median instead of mean for charge estimation, or compute per-tenant averages first then average across tenants.

---

## 🟡 Consistency & UX Issues (Confusing but Not "Wrong")

### 5. What's Next Tab Rebuilds Everything from Scratch

The What's Next tab doesn't reuse any computed data from the Charts tab. It re-derives `monthly_by_prop`, `launch_analysis`, `comparable`, charge classification, adoption data — all independently. This means:
- ~200 lines of duplicated aggregation logic
- Any future change to Charts that isn't mirrored in What's Next will silently diverge
- Today they match because you just fixed them, but this is fragile

**Fix:** Extract the core aggregation into a shared `@st.cache_data` function called once and stored in session state. Both tabs read from it.

### 6. HTML Report and In-App KPIs Can Show Different Numbers

The `generate_html_report()` function receives data at the time the "Generate Report" button is clicked. If the user changes the slider, overlay, or toggle **after** generating Charts but **before** generating the HTML report, the report captures the old state.

**Specific risk:** 
- User runs Charts with Unit Adoption overlay → sees Tranche 3 numbers
- Changes overlay to Resident Adoption
- Clicks "Generate Report" on What's Next → report uses resident adoption numbers
- But the Charts HTML export was generated earlier with unit adoption
- Two downloadable reports disagree

**Fix:** Either regenerate both reports simultaneously, or clearly stamp which overlay/settings were active when each report was generated.

### 7. Adoption Overlay Type Is Silently Coupled Across Tabs

The Charts tab radio button (`adoption_overlay`) controls what the What's Next tab shows for Tranche 3 and the executive summary. But there's no indication on What's Next of which adoption type is being used — just "Avg Adoption" without specifying unit vs resident.

**Fix:** Always show the label: "Avg **Unit** Adoption" or "Avg **Resident** Adoption" in What's Next, and include it in the exported reports.

### 8. Suspected Undisclosed Not Included in HTML Report Export

The Charts tab HTML report includes confirmed missing rent data (orange bars) but not suspected undisclosed (red bars). The What's Next PDF includes suspected numbers in the text. The Charts HTML export doesn't match what the user sees on screen if both toggles are on.

**Fix:** Pass `suspected_data` to `generate_html_report()` and include it in the exported report.

### 9. No Currency Formatting Consistency

Some places show `$5,000` (no decimals), others show `$5,000.00`. The export CSVs use `$5,000.00` strings which break if someone tries to sort/filter in Excel.

**Fix:** 
- In-app: always `$X,XXX` (no decimals for KPIs, two decimals for detail tables)
- CSV exports: raw numbers (no `$`, no commas) — let Excel handle formatting
- HTML reports: `$X,XXX` consistently

---

## 🟢 Missing Features That Would Build Trust

### 10. Add a "Data Validation" or "Health Check" Section

Before showing results, run quick sanity checks and surface them:
- Are there properties with $0 revenue in ALL months? (data issue or genuinely no pet charges?)
- Are there tenants appearing in multiple properties simultaneously? (data quality)
- Are there charge codes with suspiciously high counts? (duplicate ingestion?)
- Do launch dates make sense? (no dates in the future, no dates before 2018)
- Are there properties with charge data but no launch date? (common, should be flagged)

Show a ✅/⚠️/❌ health panel. Clients see "7 of 7 checks passed" and trust goes up immediately.

### 11. Add Methodology Confidence Levels to KPIs

Instead of flat numbers, show confidence:

| KPI | Value | Confidence |
|-----|-------|-----------|
| Revenue Change | +$12,400/mo | 🟢 High (22 of 24 properties with 3+ months baseline) |
| Projected at 100% | $47,000/mo | 🟡 Medium (linear extrapolation, last 35% may differ) |
| Uncollected Pet Rent | $8,200/mo | 🟡 Medium (based on avg fee estimation, not actual charges) |

This preempts the "how confident are you in this number?" question.

### 12. Add a "What Changed Since Last Report" Comparison

If someone ran this for the same PMC last month, they'd want to know:
- Revenue went up $X
- 12 new tenants started paying
- 3 properties improved adoption by >10%
- Missing tenant count dropped by 15

This is the most powerful trust-builder — showing the **delta** proves the data is alive and tracked, not a one-time snapshot.

**Implementation:** Save a JSON snapshot of key metrics per PMC per run. On the next run, load the previous snapshot and show diffs.

### 13. Export Should Include Source SQL Queries

For data-savvy clients (and internal teams), include the actual SQL used in an appendix or collapsible section of the HTML report. They can verify independently.

**Current state:** The Documentation tab has this, but it's not in the exports.

### 14. Missing "Last Updated" on Adoption Data

The QBR table (`R_QUARTERLY_BUSINESS_REVIEW_REPORTING`) has a `PERIOD_MONTH` field, but the app doesn't surface what the latest period is. If QBR data hasn't been refreshed in 2 months, the adoption % is stale but the app shows it as "current."

**Fix:** Show the max `PERIOD_MONTH` per property (or overall) and flag if it's more than 1 month old.

---

## 🔧 Technical Debt (Not Client-Facing But Important)

### 15. `_build_paying_sets` Creates Full DataFrame Copies Multiple Times

The function does `_all = charges_df.copy()` twice (for unit expansion and lease expansion), plus the main function copies the input. For a PMC with 100K+ charge rows, this is 300K+ row copies.

**Fix:** Build the unit keys and lease keys as sets of tuples directly from the DataFrame without copying. Or consolidate into a single pass.

### 16. No Test Coverage

Zero tests for any of the core logic. The `_build_paying_sets`, `_apply_paying_flag`, `compute_launch_analysis`, and revenue aggregation functions are pure logic that could be unit tested with small DataFrames.

**Risk:** Any refactor (like the file splitting suggested in the code review) could silently break matching logic.

**Fix:** Extract the pure functions and add pytest cases with known inputs/outputs. The validation work you did on HQ Asset Living is effectively a manual integration test — encode it.

### 17. Session State as Data Bus is Fragile

The app uses `st.session_state` as a data bus between tabs. Keys like `missing_rent_{hash}`, `suspected_{hash}`, `chart_data`, `pm_emails_cache`, `export_html`, `exec_html`, etc. are scattered across the code with no registry. If a key name changes in one place but not another, data silently disappears.

**Fix:** Define a `SessionKeys` class or enum with all keys. Reference by name, not string.

### 18. The `parse_date` Function Has No Logging on Failures

If a date can't be parsed, it silently returns `None`. Combined with the `bare except:` blocks in launch date extraction, bad dates just vanish.

**Fix:** At minimum, count unparseable dates and surface them in the health check (Issue 10).

---

## 📋 Recommended Priority Order

### Immediate (before next client presentation):
1. **Standardize the property denominator** — add the cascade/funnel line (#1)
2. **Add data freshness indicator** to exports (#3)
3. **Label adoption type** on What's Next and exports (#7)
4. **Add the Report tab count mismatch caption** (#2 quick fix)

### This Sprint:
5. **Extract shared aggregation function** (#5) — removes the biggest inconsistency risk
6. **Include suspected data in HTML report** (#8)
7. **Fix CSV exports** — raw numbers, no formatting (#9)
8. **Add basic health checks** (#10) — even 3-4 simple ones build trust

### Next Sprint:
9. **Build the delta comparison** (#12) — biggest trust-builder for repeat clients
10. **Switch to median for charge estimation** (#4)
11. **Add methodology confidence badges** (#11)
12. **Add unit tests for core matching logic** (#16)
13. **Extract pure functions and split the file** (from code-review doc)

### Backlog:
14. Include SQL in exported reports (#13)
15. Surface QBR data freshness (#14)
16. Optimize `_build_paying_sets` memory usage (#15)
17. Session state registry (#17)
18. Date parsing diagnostics (#18)

---

## Summary of All Issues Found Across All Audits

| # | Issue | Source Doc | Severity |
|---|-------|-----------|----------|
| 1 | Missing pet rent overcounting (property scope) | yardi-validation | 🔴 Bug (Fixed) |
| 2 | One-time deposits spread across months | yardi-validation | 🔴 Bug (Fixed) |
| 3 | Insufficient baselines show lift | yardi-validation | 🟡 UX (Fixed) |
| 4 | Date century bug | yardi-validation | 🔴 Bug (Fixed) |
| 5 | Cursor leaks in 6 functions | code-review | 🟡 Resource leak |
| 6 | SQL injection via f-strings | code-review | 🟡 Security hygiene |
| 7 | Bare except catches | code-review | 🟡 Debugging |
| 8 | Health check cursor leak | code-review | 🟡 Resource leak |
| 9 | Sequential API calls (no parallelism) | code-review | 🟡 Performance |
| 10 | No retry logic on API calls | code-review | 🟡 Reliability |
| 11 | Lambda apply → vectorized join | code-review | 🟡 Performance |
| 12 | Properties denominator inconsistency | cross-tab-audit | 🔴 Trust |
| 13 | Report ↔ Charts count mismatch | cross-tab-audit | 🔴 Trust |
| 14 | Lift aggregation duplicated in What's Next | cross-tab-audit | 🟡 Consistency |
| 15 | **Property denominator confusion (no funnel)** | **holistic-audit** | 🔴 Trust |
| 16 | **Report tab count gap (exec summary dep)** | **holistic-audit** | 🔴 Trust |
| 17 | **No data freshness indicator** | **holistic-audit** | 🔴 Trust |
| 18 | **Mean vs median for fee estimation** | **holistic-audit** | 🟡 Accuracy |
| 19 | **What's Next rebuilds from scratch** | **holistic-audit** | 🟡 Consistency |
| 20 | **HTML report captures stale state** | **holistic-audit** | 🟡 Consistency |
| 21 | **Adoption type not labeled** | **holistic-audit** | 🟡 UX |
| 22 | **Suspected data missing from HTML export** | **holistic-audit** | 🟡 Completeness |
| 23 | **Inconsistent currency formatting** | **holistic-audit** | 🟡 Polish |
| 24 | **No health check / sanity panel** | **holistic-audit** | 🟢 Enhancement |
| 25 | **No confidence levels on KPIs** | **holistic-audit** | 🟢 Enhancement |
| 26 | **No delta comparison between runs** | **holistic-audit** | 🟢 Enhancement |
| 27 | **No SQL in exports** | **holistic-audit** | 🟢 Enhancement |
| 28 | **QBR data freshness not surfaced** | **holistic-audit** | 🟢 Enhancement |
| 29 | **No tests** | **holistic-audit** | 🟡 Technical debt |
| 30 | **8,100-line single file** | code-review | 🟡 Technical debt |
| 31 | **Lift methodology (avg all post vs latest month)** | yardi-validation | ❓ Open question |
| 32 | **Organic growth not separated from PS lift** | yardi-validation | ❓ Open question |

---

## What's Actually Good (Don't Touch)

- **Unit + lease expansion logic** — handles roommates correctly at multiple levels
- **Entrata deduplication** — properly handles shared-lease charges
- **Entrata freshness filter** — prevents stale profiles from inflating counts  
- **Effective date coalescing** — charge_to → move_out → lease_to fallback is solid
- **Junk email filtering** — catches common fake emails
- **Documentation tab** — exceptionally thorough inline docs
- **Cross-lease email propagation** — multi-lease matching works correctly
- **Charge classification** — Yardi inferred, Entrata explicit, both handle well
- **Raw data export** — letting users validate independently is a trust-building feature
- **Property Data Explorer** — great debug tool for per-property drill-down

---

*This is the complete holistic audit. Combined with yardi-validation-findings.md, code-review-2026-03-14.md, and cross-tab-audit.md, this covers every dimension of the app.*
