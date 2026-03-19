# How We Calculate Missing Pet Rent

A plain-English explanation of how the Pet Rent tool identifies tenants who have pets but aren't being charged pet rent — and why.

---

## The Big Picture

We're answering one question: **"Which tenants have a confirmed pet through PetScreening but aren't being charged pet rent by their property?"**

To answer this, we need two things:
1. **PetScreening data** (from Snowflake) — tells us who has a pet
2. **Property management charge data** (from Yardi or Entrata API) — tells us who's paying pet rent

We match them together. Anyone who appears in #1 but NOT in #2 is "missing pet rent."

---

## The Process, Step by Step

### Step 1: Get the charge data from the property management system

We call the Yardi or Entrata API and pull every charge line for every tenant across all properties. This gives us a big table of who is being charged what — rent, pet rent, pet fees, deposits, everything.

The user then selects which charge codes count as "pet-related" (e.g., Pet Rent, Pet Fee, Pet Deposit, Monthly Pet Rent, etc.).

### Step 2: Build the "paying" list

From the charge data, we build a list of tenants who ARE paying at least one of the selected pet charge codes. This is the **paying set**.

A tenant is considered "paying" if they have ANY of the selected charge codes on their account — even just a one-time pet deposit counts.

### Step 3: Get PetScreening profiles from Snowflake

We query our data warehouse for tenants who have completed pet screening with an active household pet. These are the tenants we KNOW have a pet.

### Step 4: Match profiles to charges

We compare the PetScreening profiles against the paying set. Anyone with an active pet profile who is NOT in the paying set gets flagged as "missing pet rent."

### Step 5: Estimate the dollar impact

For each missing tenant, we estimate how much pet rent is going uncollected by using the average pet rent that OTHER tenants at the same property are paying.

---

## Entrata vs. Yardi: Key Differences

### How We Get Charge Data

| | Yardi | Entrata |
|---|---|---|
| **API** | SOAP-based GetRentroll API | REST-based getLeases API |
| **What it returns** | Scheduled charges per tenant | Leases with customers, intervals, and scheduled charges |
| **Tenant identifier** | `tenant_code` (straightforward) | `customerId` (extracted from lease JSON) |
| **Email availability** | Not always available | Usually available per customer |
| **Date range** | Pulls 10 years of history (wide window) | Pulls all lease statuses (current, past, notice) |

### How Charges Are Structured

**Yardi:** Simple — each row is one charge for one tenant. One tenant code, one charge code, one amount.

**Entrata:** Complex — charges belong to a **lease**, not a person. A lease can have multiple customers (roommates, guarantors). Each customer on the lease sees the same charges. We handle this by:
- Emitting one row per customer × charge combination
- Using a deduplication key so we don't double-count revenue
- If any customer on a lease has a pet charge, all customers on that lease are marked as "paying"

### How We Identify Tenants

**Yardi:** Match on `tenant_code` — a unique identifier Yardi assigns to each tenant. Straightforward and reliable.

**Entrata:** Match on `customerId` first, then fall back to email. The customer ID comes from a JSON field in the lease data (`lease_source_external_id`), and we try three possible key names:
```
tenant_code → customerId → customer_id
```
If none of these exist, we fall back to matching by email address.

### Entrata-Only: The Freshness Filter

Entrata has an extra safety check that Yardi does not need.

**The problem it solves:** PetScreening profiles in Snowflake might include tenants who have already moved out but whose profiles haven't been deactivated yet. If we flag these ex-tenants as "missing pet rent," it's a false positive — they don't live there anymore.

**How it works:** After identifying profiles with pets, we check if that tenant's email appears anywhere in the current Entrata API data. If their email is NOT in the API data, we assume they're no longer an active tenant and exclude them.

**The tradeoff:** This filter is conservative. It can also exclude legitimate current tenants whose email in PetScreening doesn't match their email in Entrata (typos, different email used, etc.). When this happens, a message shows how many profiles were excluded.

**Yardi doesn't need this** because the Yardi API returns a rent roll (current occupants), which is inherently fresh. Entrata's getLeases API can return historical leases, so we need the extra check.

---

## Filters on PetScreening Profiles

We only flag a tenant as "missing pet rent" if ALL of these are true:

| Filter | What it means | Why |
|---|---|---|
| `compliance_status = 'compliant'` | The tenant completed their screening fully | Incomplete screenings might not have confirmed pets yet |
| `user_pet_type = 'household'` | The pet is classified as a household pet (not an assistance/ESA animal) | Assistance animals cannot be charged pet rent — it's a legal requirement (Fair Housing Act) |
| `user_pet_status = 'active'` | The pet profile is currently active | Archived or removed pet profiles shouldn't generate charges |
| Email is not null/blank/junk | The tenant has a real email address | We need a valid identifier to match against charge data |

### What This Means for the Numbers

These filters are **intentionally conservative**. We would rather miss a real case than flag a false positive. This means:

- Tenants who started screening but didn't finish → **not counted**
- Tenants with pending or draft pet profiles → **not counted**
- Tenants with assistance/ESA animals → **not counted** (correctly — these can't be charged)
- Tenants who never screened at all → **not counted** (we have no evidence of a pet)

The "missing pet rent" number represents the **minimum confirmed gap** — the real number could be higher, but we can only flag what we can prove.

---

## Edge Cases We Account For

### Roommates / Shared Leases

**Problem:** Two roommates share a unit. One roommate has the pet charge on their account, the other doesn't. Without handling this, the second roommate would be flagged as "not paying pet rent" even though the unit IS being charged.

**Solution:** We expand the paying set at two levels:
- **Unit-level:** If any tenant on a unit has a pet charge, all tenants on that unit are marked as paying
- **Lease-level (Entrata):** If any customer on the same lease has a pet charge, all customers on that lease are marked as paying

### Lease Renewals / Changed Tenant Codes

**Problem:** When a tenant renews their lease in Entrata, they might get a new customer ID. Their PetScreening profile has the old ID. The match fails.

**Solution:** We fall back to email matching. If the tenant code doesn't match, we try matching by email address (case-insensitive). We also propagate the paying flag — if any row for the same (property, email) combination is paying, all rows for that person are marked as paying.

### Cancelled / Applicant Leases (Entrata)

**Problem:** Entrata returns all lease intervals, including cancelled applications and denied applicants. Charges on cancelled leases shouldn't count as real revenue.

**Solution:** We filter lease intervals by status. Only `Current`, `Past`, and `Notice` intervals are included. `Cancelled`, `Applicant`, `Denied`, and `Future` intervals are dropped.

### One-Time vs. Recurring Charges

**Problem:** Some properties charge a monthly pet rent, others charge a one-time pet deposit, and some charge both. We need to estimate the right dollar amount for missing tenants.

**Solution:**
- **Entrata:** Uses the explicit `frequency` field on each charge (one-time vs. monthly)
- **Yardi:** Infers from the date range — if the charge spans more than 60 days, it's recurring; otherwise it's one-time
- Missing revenue estimation uses the property-specific average. If a property has no payers to average from, we fall back to the portfolio-wide average.

### Properties Without Charge Data

**Problem:** Some properties don't return any data from the API (connection issues, permissions, etc.). If we included their PetScreening profiles, every single profile would be flagged as "missing" — which isn't fair.

**Solution:** We scope the analysis to only properties that successfully returned charge data. Properties with no API data are excluded entirely.

### Tenants Not in API Data (Entrata Freshness)

**Problem:** A tenant might exist in PetScreening's Snowflake data but not appear in the current Entrata API pull — possibly because they moved out, or the API didn't return their lease.

**Solution:** The Entrata freshness filter checks if the tenant's email exists in the API data. If not, they're excluded (with a count shown to the user). Tenants already marked as paying are kept regardless.

### Properties with No Pet Charges At All

**Problem:** A property might have charge data but zero tenants paying any pet-related charge. Should we flag all pet owners there as "missing"?

**Solution:** Yes — but we use the portfolio-wide average fee to estimate the dollar impact, since we can't calculate a property-specific average. The label shows "(portfolio avg)" to make this clear.

---

## Suspected Undisclosed Pets (Separate Analysis)

Beyond "confirmed" missing pet rent, there's a second category: **suspected undisclosed pets**. These are tenants who show signals of having a pet but haven't completed screening.

A tenant is "suspected" if:
- They **started** a household pet profile but didn't finish it (abandoned)
- They have an **unresolved assistance/ESA request** (draft, non-responsive, declined, not recommended, returned status)
- They started an assistance profile but ended up declaring **no pet**

These are NOT counted in the main "missing pet rent" number. They appear separately when the "Show suspected undisclosed" toggle is turned on.

**Excluded from suspected:** Assistance profiles with `recommended` or `expired` status (these are legitimate ESA cases), and any pet profile with an archive reason (intentionally removed).

---

## What the Numbers Mean

| Metric | Definition |
|---|---|
| **Total Unpaid/Suspected** | Count of unique tenants with confirmed active pets who have no matching pet charge |
| **Uncollected (Current Month)** | Estimated monthly pet rent not being collected right now |
| **Total Uncollected** | Cumulative estimated uncollected rent across all months since each property's launch |
| **Properties Affected** | How many properties have at least one missing pet rent tenant |

### Why the Number Might Feel Low

The confirmed missing count is intentionally the most conservative number we can produce. It only captures tenants where we can prove BOTH:
1. They have a verified, active, compliant household pet in PetScreening
2. They have zero matching pet charge codes in their property management system

It does NOT capture:
- Tenants who never screened (biggest blind spot — we have no data on them)
- Tenants with incomplete or pending screenings
- Tenants whose email/ID doesn't match between systems (they get silently excluded)
- Properties not connected via API
- Tenants filtered out by the Entrata freshness check

Think of the confirmed number as the **floor**, not the ceiling. The actual gap is likely larger.

---

## Data Flow Diagram

```
PetScreening Profiles (Snowflake)          Property Charges (Yardi/Entrata API)
┌──────────────────────────┐               ┌───────────────────────────┐
│ Compliant tenants with   │               │ All charge lines for all  │
│ active household pets    │               │ tenants across properties │
│                          │               │                           │
│ Filters:                 │               │ User selects which charge │
│ • compliant status       │               │ codes = "pet related"     │
│ • household pet type     │               │                           │
│ • active pet status      │               │ → Builds "paying set"     │
│ • valid email            │               │   (tenant_code + email)   │
└────────────┬─────────────┘               └────────────┬──────────────┘
             │                                          │
             │              ┌───────────┐               │
             └─────────────►│   MATCH   │◄──────────────┘
                            └─────┬─────┘
                                  │
                    ┌─────────────┼──────────────┐
                    ▼                            ▼
            ┌──────────────┐            ┌───────────────┐
            │   PAYING     │            │   MISSING     │
            │ Has pet +    │            │ Has pet +     │
            │ has charge   │            │ NO charge     │
            │              │            │               │
            │ (No action   │            │ → Flag as     │
            │  needed)     │            │   uncollected │
            └──────────────┘            └───────────────┘
```

---

*Last updated: March 19, 2026*
