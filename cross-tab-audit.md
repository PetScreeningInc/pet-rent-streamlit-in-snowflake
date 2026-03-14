# Cross-Tab Consistency Audit
**Date:** 2026-03-14

---

## ❌ Issue 1: "Properties" Denominator Differs Across Tabs

The "X of Y properties" metric uses a different Y depending on where you look:

| Location | "Y" (total properties) | What it means |
|----------|----------------------|---------------|
| **Charts tab** — Launch Dates KPI | `len(monthly_by_prop)` | Properties with at least one charge in selected pet codes |
| **Charts tab** — Properties Affected (missing rent) | `len(st.session_state.property_ids)` | ALL properties with integrations under the parent company |
| **What's Next tab** — Quick Stats | `len(_prop_ids)` | ALL properties with integrations |
| **What's Next tab** — Properties with Charge Data | `len(_monthly_by_prop)` vs `len(_prop_ids)` | Both shown — this one is actually fine |
| **HTML Report** — Properties with Launch Date | `n_props_total = len(monthly_by_prop)` | Properties with pet charge data |
| **Report tab** — Properties in scope (info box) | `len(st.session_state.property_ids)` | ALL properties with integrations |

**Problem:** A user sees "32 of 36 properties" on one tab and "32 of 44 properties" on another. Different denominators = confusion.

**Fix:** Pick one definition and use it everywhere. Recommendation: use `len(st.session_state.property_ids)` (total integrated properties) as the universal denominator, but ALSO show `len(monthly_by_prop)` as "Properties with charge data" so the gap is visible.

---

## ⚠️ Issue 2: Missing Tenant Count Could Differ Between Report Tab and Charts/What's Next

**Charts + What's Next tabs:** Count comes from `fetch_missing_pet_rent_by_property()` → `USER_EMAIL.nunique()` per property, then summed.

**Report tab:** Count comes from `generate_missing_pet_rent_report()` → profiles matched to `R_MONTHLY_EXECUTIVE_SUMMARY`, then `USER_EMAIL.nunique()`.

The profile identification (Step 2-3) is identical — same SQL, same `_apply_paying_flag`. But the Report tab has an extra Step 4-5 where it joins against `R_MONTHLY_EXECUTIVE_SUMMARY`:

```python
# Step 5: Keep only exec summary records whose (email, property) is missing
report_df = exec_df[exec_df.apply(lambda r: (email, property_id) in missing_lookup)]
```

If a tenant's email exists in `petscreening__user_enriched` (Step 2) but NOT in `R_MONTHLY_EXECUTIVE_SUMMARY` (Step 4), they'll be counted in Charts/What's Next but **missing from the Report**. The Report count would be lower.

**When this happens:** `R_MONTHLY_EXECUTIVE_SUMMARY` is a reporting table that may be refreshed on a different cadence than `petscreening__user_enriched`. New profiles could show up in one but not the other.

**Fix:** Either:
a) Remove the exec summary dependency — build the report directly from the Step 2-3 profiles (add pet details via a separate join), OR
b) Accept the potential discrepancy and add a note explaining it, OR
c) Use the same source for both

---

## ⚠️ Issue 3: Lift Aggregates Recalculated Independently in What's Next

The What's Next tab doesn't reuse `launch_analysis` from the Charts tab. It rebuilds `_monthly_by_prop`, `_launch_analysis`, and `_comparable` from scratch using `parsed_charges` in session state.

Before the fixes today, the Charts tab and What's Next tab could compute different window ranges if the slider value changed between tab views (slider drove Charts, hardcoded value for What's Next used `lookback_months`). 

**After today's fix:** Both tabs now derive the window from the earliest charge date, so they SHOULD match. But the code is still duplicated — any future change to one tab's aggregation that isn't mirrored in the other will cause a mismatch.

**Fix:** Extract the aggregation into a shared function called once, stored in session state, and read by both tabs.

---

## ⚠️ Issue 4: One-Time Classification Not Applied to What's Next Revenue

**Status: FIXED in today's commit** — both tabs now classify charges as recurring vs one-time. But worth noting: this was a live inconsistency before today where Charts would show one number and What's Next would show a different one for the same metric.

---

## ✅ Things That Are Consistent

| Metric | Source | Consistent? |
|--------|--------|-------------|
| Missing tenant count (Charts ↔ What's Next) | Same `_missing_rent_data` from session state cache | ✅ Same data |
| Suspected count (Charts ↔ What's Next) | Same `_suspected_data` from session state cache | ✅ Same data |
| Selected charge codes | Global `selected_codes` | ✅ Same everywhere |
| Profile identification (compliant + household + active) | Same SQL filters in both functions | ✅ Identical |
| Paying set logic | Same `_build_paying_sets` + `_apply_paying_flag` | ✅ Shared code |
| Junk email list | Same `JUNK_EMAILS` constant | ✅ Shared constant |
| Property scoping (charge data filter) | Now applied to all 3 functions | ✅ Fixed today |

---

## Recommended Priority

1. **Standardize property denominator** — pick one, use everywhere (quick)
2. **Extract aggregation into shared function** — removes the Charts ↔ What's Next duplication risk (medium)
3. **Investigate Report ↔ Charts count mismatch** — check if `R_MONTHLY_EXECUTIVE_SUMMARY` actually causes a gap (investigate first, then fix)
