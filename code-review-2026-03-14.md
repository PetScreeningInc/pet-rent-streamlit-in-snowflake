# Pet Rent App — Code Review
**Date:** 2026-03-14

---

## 🔴 Bugs / Should Fix

### 1. Cursor Leaks (6 functions never close their cursor)

These functions open a Snowflake `DictCursor` but never call `cur.close()`:

| Line | Function | Impact |
|------|----------|--------|
| 794 | `load_parent_companies()` | Low (cached, runs rarely) |
| 820 | `load_all_properties()` | Low (cached) |
| 836 | `load_properties_for_selection()` | Low (cached) |
| 888 | `load_entrata_parent_companies()` | Low (cached) |
| 917 | `load_entrata_all_properties()` | Low (cached) |
| 933 | `load_entrata_properties_for_selection()` | Low (cached) |
| 3534 | `generate_missing_pet_rent_report()` | **Medium** — runs on demand, opens cursor, runs 2 queries, returns without closing |

The cached functions are lower risk since they run infrequently and the cursor eventually gets garbage collected. But `generate_missing_pet_rent_report` runs every time someone downloads the report — worth fixing.

**Fix:** Add `cur.close()` before each return, or better, use `with conn.cursor() as cur:` context manager.

---

### 2. SQL Injection via f-strings

Multiple functions build SQL queries with f-string interpolation of user-controlled values:

```python
# Line 843
where_clause += f" AND p.parent_company_name = '{parent_company_name}'"
# Line 845
where_clause += f" AND p.property_id = {property_id}"
# Line 847
where_clause += f" AND p.parent_company_ancestry_id = '{ancestry_id}'"
```

Same pattern at lines 945-949 (Entrata version).

**Risk:** Low in practice — values come from Snowflake dropdown results, not raw user input. But if anyone ever passes a crafted parent company name, it's injectable.

**Fix:** Use parameterized queries (`cur.execute(sql, params)`) or at minimum `snowflake.connector.paramstyle` binding.

---

### 3. Bare `except:` Catches (Swallowing All Errors)

Lines 1474, 1578, 1723, 4982 have bare `except:` blocks that catch everything including `KeyboardInterrupt` and `SystemExit`:

```python
except:
    continue  # silently swallows errors
```

These are in date parsing and launch date extraction — they'll silently skip bad data with no logging.

**Fix:** Change to `except (ValueError, TypeError, AttributeError):` or at minimum `except Exception:`.

---

### 4. `get_snowflake_connection()` Creates a New Connection Every Call

```python
def get_snowflake_connection():
    conn = _create_snowflake_connection()  # this IS cached via @st.cache_resource
    try:
        conn.cursor().execute("SELECT 1")  # health check cursor never closed
    except Exception:
        _create_snowflake_connection.clear()
        conn = _create_snowflake_connection()
    return conn
```

The health check `conn.cursor().execute("SELECT 1")` creates a cursor that's never closed. This runs every time any function calls `get_snowflake_connection()`. Minor leak but adds up.

**Fix:** Use `with conn.cursor() as cur: cur.execute("SELECT 1")` for the health check.

---

## 🟡 Concerns / Worth Improving

### 5. Single 7,800-Line File

Everything lives in one `app.py`. Functions, API calls, data processing, chart building, HTML report generation, Snowflake queries, SOAP/XML parsing — all in one file.

**Impact:** Hard to test, hard to review, high risk of unintended side effects when editing. Finding anything requires grep.

**Suggestion:** When there's time, split into modules:
- `snowflake_queries.py` — all Snowflake query functions
- `api_clients.py` — Yardi SOAP + Entrata REST
- `charts.py` — Plotly chart builders
- `reports.py` — HTML report generation
- `analysis.py` — lift calculations, paying set logic, missing rent
- `app.py` — Streamlit UI orchestration only

---

### 6. API Credentials Fetched from Snowflake and Used in Plain Text

`load_properties_for_selection()` fetches API passwords from Snowflake:
```sql
PARSE_JSON(i.settings):"password"::STRING AS password
```

These passwords travel through the Streamlit session state as plain dict values. Not a vulnerability per se (it's server-side), but if session state ever gets serialized/logged, credentials leak.

**Suggestion:** Fetch credentials only when making the API call, not during property selection. Don't store them in broader data structures.

---

### 7. Sequential API Calls (No Parallelism for Yardi)

`fetch_rentroll_for_properties()` calls each property's SOAP endpoint sequentially in a `for` loop. For a PMC with 50+ properties, this is slow.

Entrata uses `_fetch_entrata_leases_for_property` which is also sequential.

**Suggestion:** Use `ThreadPoolExecutor` (already imported but unused for Yardi) with 3-5 workers. The SOAP endpoints are independent — easy parallelism.

---

### 8. No Retry Logic on API Calls

If a Yardi SOAP call times out or returns a transient error, it's logged and skipped. No retry.

```python
except requests.exceptions.Timeout:
    results_log.append({...status: "Error: Timeout"...})
```

**Suggestion:** Add 1-2 retries with exponential backoff for timeout/5xx errors. `requests` + `urllib3` Retry adapter, or simple manual retry loop.

---

### 9. `pmc_system` Interpolated into SQL Strings

```python
pmc_system = st.session_state.get("pmc_system", "yardi")
# ...
AND du.unit_source = '{pmc_system}'
```

This is used in multiple Snowflake queries. While `pmc_system` is only ever "yardi" or "entrata" (set by the app), it's still string interpolation into SQL.

**Fix:** Use parameterized queries or validate against an allowlist: `assert pmc_system in ('yardi', 'entrata')`.

---

### 10. Missing Pet Rent Report — `lambda` Apply on Large DataFrames

```python
report_df = exec_df[
    exec_df.apply(
        lambda r: (
            str(r['USER_EMAIL']).lower().strip(),
            str(r['PROPERTY_ID']),
        ) in missing_lookup,
        axis=1,
    )
]
```

`.apply(lambda, axis=1)` is very slow on large DataFrames — it's a Python-level row loop. For 10K+ rows this is noticeable.

**Fix:** Use a vectorized merge/join instead:
```python
exec_df['_lookup_key'] = list(zip(
    exec_df['USER_EMAIL'].str.lower().str.strip(),
    exec_df['PROPERTY_ID'].astype(str)
))
report_df = exec_df[exec_df['_lookup_key'].isin(missing_lookup)]
```

Same pattern at lines 3606, 3847, 4180, 4414 (`_in_api` functions).

---

## 🟢 Things That Are Well Done

- **Error handling on API calls** — good logging of which properties failed and why
- **Effective date coalescing** — the `charge_to_date → move_out → lease_to` fallback logic is solid and well-commented
- **Unit-level expansion** for roommate handling — thorough implementation
- **Cross-lease email propagation** — handles the multi-lease matching correctly
- **Entrata charge deduplication** — properly handles shared-lease charge duplication
- **Junk email filtering** — good list of known fake emails
- **Inline documentation** — the SQL queries and analysis functions have clear comments explaining the "why"

---

## Priority Order for Fixes

1. **Cursor leaks in `generate_missing_pet_rent_report`** — quick fix, real leak
2. **Health check cursor leak** — quick fix
3. **Bare `except:` → `except Exception:`** — quick fix, better debugging
4. **Lambda apply → vectorized join** — performance improvement for large PMCs
5. **SQL parameterization** — security hygiene (low urgency given internal use)
6. **File splitting** — big refactor, do when there's breathing room
