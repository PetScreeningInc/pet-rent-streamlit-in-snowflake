# Entrata Deep Dive: Hillpointe - Enterprise (Ancestry ID: 63451)

**Generated:** 2026-03-03 12:24

## Step 0: Properties & Launch Dates

**43 properties** found for Hillpointe - Enterprise

- With launch date: **43**
- Without launch date: **0**

| Property | Launch Date | Corp ID | Entrata PID |
|----------|------------|---------|-------------|
| Hillpointe - Pointe Grand Augusta | 2025-04-17 | hillpointe | 100070619 |
| Hillpointe - Pointe Grand Augusta Reservation Way | 2025-05-20 | hillpointe | 100070638 |
| Hillpointe - Pointe Grand Beaufort | 2025-09-18 | hillpointe | 100070605 |
| Hillpointe - Pointe Grand Bergen Woods | 2025-05-20 | hillpointe | 100070615 |
| Hillpointe - Pointe Grand Brick City | 2025-05-21 | hillpointe | 100070639 |
| Hillpointe - Pointe Grand Brunswick | 2025-04-17 | hillpointe | 100070620 |
| Hillpointe - Pointe Grand Brunswick Island View | 2025-01-08 | hillpointe | 100070592 |
| Hillpointe - Pointe Grand Byron | 2025-05-20 | hillpointe | 100070621 |
| Hillpointe - Pointe Grand Cartersville | 2026-01-12 | hillpointe | 100139785 |
| Hillpointe - Pointe Grand Champions Village | 2025-09-18 | hillpointe | 100070602 |
| Hillpointe - Pointe Grand Columbia | 2025-04-24 | hillpointe | 100070616 |
| Hillpointe - Pointe Grand Covington | 2025-10-20 | hillpointe | 100070613 |
| Hillpointe - Pointe Grand Davenport | 2024-04-04 | hillpointe | 100070636 |
| Hillpointe - Pointe Grand Dawsonville | 2025-05-20 | hillpointe | 100070622 |
| Hillpointe - Pointe Grand Daytona | 2025-04-17 | hillpointe | 100070618 |
| Hillpointe - Pointe Grand DeLand | 2025-04-08 | hillpointe | 100070595 |
| Hillpointe - Pointe Grand Fort Pierce | 2026-01-12 | hillpointe | 100070601 |
| Hillpointe - Pointe Grand Jacksonville West | 2025-03-04 | hillpointe | 100070626 |
| Hillpointe - Pointe Grand Lakeland | 2025-10-20 | hillpointe | 100070600 |
| Hillpointe - Pointe Grand Macon | 2025-04-17 | hillpointe | 100070627 |
| Hillpointe - Pointe Grand New Berlin | 2025-04-23 | hillpointe | 100070596 |
| Hillpointe - Pointe Grand North Athens | 2025-10-22 | hillpointe | 100070607 |
| Hillpointe - Pointe Grand Ocala | 2023-07-01 | hillpointe | 100070625 |
| Hillpointe - Pointe Grand Palm Coast | 2025-04-23 | hillpointe | 100070624 |
| Hillpointe - Pointe Grand Panama City | 2025-09-18 | hillpointe | 100070597 |
| Hillpointe - Pointe Grand Pendergrass | 2023-12-06 | hillpointe | 100070630 |
| Hillpointe - Pointe Grand Pensacola | 2025-10-20 | hillpointe | 100070612 |
| Hillpointe - Pointe Grand Plant City | 2024-06-06 | hillpointe | 100070631 |
| Hillpointe - Pointe Grand Port Wentworth | 2025-09-18 | hillpointe | 100070598 |
| Hillpointe - Pointe Grand Southlake | 2023-07-01 | hillpointe | 100070632 |
| Hillpointe - Pointe Grand Spartanburg | 2025-05-20 | hillpointe | 100070623 |
| Hillpointe - Pointe Grand Spring Hill | 2024-07-18 | hillpointe | 100070633 |
| Hillpointe - Pointe Grand Statesboro | 2025-10-20 | hillpointe | 100070604 |
| Hillpointe - Pointe Grand Thomaston | 2025-03-21 | hillpointe | 100070593 |
| Hillpointe - Pointe Grand Timber Ridge | 2025-05-20 | hillpointe | 100070637 |
| Hillpointe - Pointe Grand Town Center | 2026-01-12 | hillpointe | 100070603 |
| Hillpointe - Pointe Grand Warner Robins | 2025-04-17 | hillpointe | 100070634 |
| Hillpointe - Pointe Grand Willowbrook | 2025-12-10 | hillpointe | 100070608 |
| Hillpointe - Pointe Grand Yulee | 2025-10-20 | hillpointe | 100070599 |
| Hillpointe - Pointe Grand at Heath Brook | 2025-05-20 | hillpointe | 100070617 |
| Hillpointe - Pointe Grand at Oakleaf | 2025-03-04 | hillpointe | 100070629 |
| Hillpointe - Pointe Grand on Main | 2023-09-29 | hillpointe | 100070628 |
| Hillpointe - Pointe Villas | 2025-04-17 | hillpointe | 100070635 |

> **Note:** The Entrata `getLeases` API caps at 100 leases per page. The pagination in both this script and `app.py` breaks after page 1 if the API doesn't return `meta.currentPage`/`meta.lastPage`. This means we may only see the first 100 leases per property.

## Charge Code Discovery

Pet-related charge codes found:

| Code | Count |
|------|-------|
| Pet Rent | 2,359 |
| Pet Fee | 283 |

**Using:** `['Pet Rent', 'Pet Fee']`

Top 20 charge codes overall:

| Code | Count |
|------|-------|
| Tech Package | 8,818 |
| Washer/Dryer Rental Charges | 8,818 |
| Valet Trash Fee | 8,765 |
| Rent | 8,691 |
| One-Time Concession | 3,750 |
| Amenity Rent | 3,645 |
| Pet Rent | 2,359 |
| Amenity Fob | 2,129 |
| Risk Waiver Fee | 1,671 |
| Garage Rent | 1,429 |
| Hassle-Free Living | 1,163 |
| Month-to-Month Rent | 970 |
| Month To Month Fee | 892 |
| Administrative Fee | 536 |
| Garage/Storage Unit Concession | 421 |
| Pet Fee | 283 |
| Internet Concession | 198 |
| Washer/Dryer Concession | 193 |
| Renewal Rent | 129 |
| Trash Fine | 91 |

---
## Investigation 1: Pre-PetScreening Data Availability

**Question:** Do we get pet charge data from before PetScreening went live, or does the API only return data from around launch?

### Interval status distribution (all pet charges)

| Status | Count | Is Valid |
|--------|-------|----------|
|  | 2,642 | False |

> **Note:** No charges matched 'current/past/notice' interval filter. Using ALL pet charges for analysis.

**2 properties with ZERO pet charges:**
- Hillpointe - Pointe Grand Cartersville
- Hillpointe - Pointe Grand Town Center

### Per-Property Breakdown

| Property | Launch | Earliest Charge | Pre Months | Post Months | Pre Charges | Post Charges | Months Before Launch |
|----------|--------|-----------------|-----------|------------|-------------|-------------|---------------------|
| Hillpointe - Pointe Grand North Athens | 2025-10-22 | 2026-01-02 | 0 | 1 | 0 | 2 | -3 |
| Hillpointe - Pointe Grand Port Wentworth | 2025-09-18 | 2025-10-22 | 0 | 4 | 0 | 11 | -2 |
| Hillpointe - Pointe Grand Plant City | 2024-06-06 | 2024-08-16 | 0 | 20 | 0 | 63 | -3 |
| Hillpointe - Pointe Grand Pensacola | 2025-10-20 | 2026-02-02 | 0 | 2 | 0 | 4 | -4 |
| Hillpointe - Pointe Grand Pendergrass | 2023-12-06 | 2024-02-10 | 0 | 24 | 0 | 81 | -3 |
| Hillpointe - Pointe Grand Statesboro | 2025-10-20 | 2025-11-01 | 0 | 4 | 0 | 8 | -1 |
| Hillpointe - Pointe Grand Ocala | 2023-07-01 | 2024-01-30 | 0 | 25 | 0 | 52 | -8 |
| Hillpointe - Pointe Grand on Main | 2023-09-29 | 2024-05-07 | 0 | 11 | 0 | 24 | -8 |
| Hillpointe - Pointe Grand New Berlin | 2025-04-23 | 2025-09-26 | 0 | 1 | 0 | 2 | -6 |
| Hillpointe - Pointe Grand Thomaston | 2025-03-21 | 2025-05-01 | 0 | 1 | 0 | 12 | -2 |
| Hillpointe - Pointe Grand Lakeland | 2025-10-20 | 2026-04-24 | 0 | 3 | 0 | 10 | -7 |
| Hillpointe - Pointe Grand Fort Pierce | 2026-01-12 | 2026-02-27 | 0 | 2 | 0 | 5 | -2 |
| Hillpointe - Pointe Grand Southlake | 2023-07-01 | 2024-02-12 | 0 | 14 | 0 | 24 | -8 |
| Hillpointe - Pointe Grand DeLand | 2025-04-08 | 2025-06-01 | 0 | 2 | 0 | 10 | -2 |
| Hillpointe - Pointe Grand Willowbrook | 2025-12-10 | 2026-02-26 | 0 | 2 | 0 | 2 | -3 |
| Hillpointe - Pointe Grand Davenport | 2024-04-04 | 2024-11-05 | 0 | 8 | 0 | 23 | -8 |
| Hillpointe - Pointe Grand Covington | 2025-10-20 | 2026-01-12 | 0 | 2 | 0 | 4 | -3 |
| Hillpointe - Pointe Grand Yulee | 2025-10-20 | 2025-12-31 | 0 | 5 | 0 | 13 | -3 |
| Hillpointe - Pointe Grand Champions Village | 2025-09-18 | 2026-04-24 | 0 | 2 | 0 | 4 | -8 |
| Hillpointe - Pointe Grand Brunswick Island View | 2025-01-08 | 2025-05-27 | 0 | 4 | 0 | 12 | -5 |
| Hillpointe - Pointe Grand Bergen Woods | 2025-05-20 | 2025-09-30 | 0 | 5 | 0 | 17 | -5 |
| Hillpointe - Pointe Grand Beaufort | 2025-09-18 | 2026-01-02 | 0 | 3 | 0 | 8 | -4 |
| Hillpointe - Pointe Grand Spring Hill | 2024-07-18 | 2024-10-01 | 0 | 14 | 0 | 53 | -3 |
| Hillpointe - Pointe Grand Panama City | 2025-09-18 | 2025-08-25 | 2 | 2 | 8 | 6 | 0 |
| Hillpointe - Pointe Grand Brick City | 2025-05-21 | 2025-02-28 | 3 | 5 | 5 | 26 | 2 |
| Hillpointe - Pointe Grand Augusta Reservation Way | 2025-05-20 | 2025-02-14 | 4 | 6 | 13 | 16 | 3 |
| Hillpointe - Pointe Grand Timber Ridge | 2025-05-20 | 2024-12-16 | 4 | 4 | 10 | 7 | 5 |
| Hillpointe - Pointe Grand at Oakleaf | 2025-03-04 | 2024-10-14 | 6 | 8 | 21 | 13 | 4 |
| Hillpointe - Pointe Grand Jacksonville West | 2025-03-04 | 2024-02-15 | 7 | 8 | 12 | 20 | 12 |
| Hillpointe - Pointe Grand at Heath Brook | 2025-05-20 | 2024-05-10 | 11 | 7 | 29 | 13 | 12 |
| Hillpointe - Pointe Grand Augusta | 2025-04-17 | 2024-04-30 | 11 | 11 | 22 | 26 | 11 |
| Hillpointe - Pointe Grand Columbia | 2025-04-24 | 2024-03-30 | 11 | 10 | 26 | 27 | 13 |
| Hillpointe - Pointe Grand Byron | 2025-05-20 | 2024-04-15 | 11 | 8 | 22 | 19 | 13 |
| Hillpointe - Pointe Grand Brunswick | 2025-04-17 | 2024-02-23 | 11 | 11 | 22 | 28 | 13 |
| Hillpointe - Pointe Grand Palm Coast | 2025-04-23 | 2024-02-01 | 12 | 9 | 30 | 23 | 14 |
| Hillpointe - Pointe Grand Daytona | 2025-04-17 | 2024-01-08 | 12 | 9 | 18 | 20 | 15 |
| Hillpointe - Pointe Grand Warner Robins | 2025-04-17 | 2023-12-14 | 12 | 12 | 25 | 24 | 16 |
| Hillpointe - Pointe Grand Dawsonville | 2025-05-20 | 2024-03-28 | 12 | 9 | 32 | 22 | 13 |
| Hillpointe - Pointe Grand Spartanburg | 2025-05-20 | 2024-04-01 | 12 | 11 | 27 | 21 | 13 |
| Hillpointe - Pointe Grand Macon | 2025-04-17 | 2024-01-02 | 13 | 8 | 26 | 15 | 15 |
| Hillpointe - Pointe Villas | 2025-04-17 | 2024-02-02 | 13 | 11 | 33 | 40 | 14 |

### Verdict

- Properties with **< 3 pre-launch months** (unreliable baseline): **24**
- Properties with **3+ pre-launch months** (usable baseline): **17**
- Properties with **zero pet charges**: **2**

> **Finding:** The majority of properties lack sufficient pre-launch charge data. This strongly suggests the Entrata `getLeases` API only returns scheduled charges from around the time the PetScreening integration was configured -- NOT the full historical charge ledger. The 'Revenue Change Since PetScreening' metric is **unreliable** for this PMC.
>
> Properties with some pre-launch data may have been piloting or had their integration enabled before the official launch date.

---
## Investigation 2: Missing Pet Rent Deduplication

**Question:** If two residents share a lease and one is paying, do we correctly exclude the other from 'missing'? Are we overcounting?

- Total leases with pet charges: **701**
- Shared leases (2+ customers) with pet charges: **572** (81.6%)

### Paying set expansion

- Direct paying tenant_codes: **1511**
- After unit expansion: **2003** (+492 co-tenants added)
- Direct paying emails: **1357**
- After unit expansion: **1752** (+395 added)

### Profile matching results

- Total active household profiles: **1460** unique emails
- Matched as **paying**: **191**
- Flagged as **missing** (not paying pet rent): **1269**

### Match method breakdown (paying profiles)

- Direct tenant_code: 0
- Direct email: 185
- Unit expansion (TC): 0
- Unit expansion (email): 191

### Missing tenants by property

| Property | Missing Tenants |
|----------|----------------|
| Hillpointe - Pointe Grand Dawsonville | 88 |
| Hillpointe - Pointe Grand Palm Coast | 85 |
| Hillpointe - Pointe Grand Pendergrass | 68 |
| Hillpointe - Pointe Grand Spring Hill | 65 |
| Hillpointe - Pointe Grand Augusta | 61 |
| Hillpointe - Pointe Grand Columbia | 55 |
| Hillpointe - Pointe Grand Plant City | 53 |
| Hillpointe - Pointe Grand at Oakleaf | 51 |
| Hillpointe - Pointe Grand Macon | 48 |
| Hillpointe - Pointe Grand Spartanburg | 47 |
| Hillpointe - Pointe Grand Ocala | 47 |
| Hillpointe - Pointe Grand Byron | 47 |
| Hillpointe - Pointe Grand Davenport | 46 |
| Hillpointe - Pointe Grand Brunswick | 44 |
| Hillpointe - Pointe Grand on Main | 43 |
| Hillpointe - Pointe Grand Warner Robins | 43 |
| Hillpointe - Pointe Grand Brunswick Island View | 33 |
| Hillpointe - Pointe Grand Daytona | 31 |
| Hillpointe - Pointe Grand Jacksonville West | 31 |
| Hillpointe - Pointe Villas | 30 |
| Hillpointe - Pointe Grand DeLand | 29 |
| Hillpointe - Pointe Grand Brick City | 29 |
| Hillpointe - Pointe Grand Thomaston | 29 |
| Hillpointe - Pointe Grand Panama City | 27 |
| Hillpointe - Pointe Grand Timber Ridge | 22 |
| Hillpointe - Pointe Grand at Heath Brook | 20 |
| Hillpointe - Pointe Grand Southlake | 18 |
| Hillpointe - Pointe Grand Augusta Reservation Way | 16 |
| Hillpointe - Pointe Grand Bergen Woods | 13 |
| Hillpointe - Pointe Grand New Berlin | 13 |
| Hillpointe - Pointe Grand Port Wentworth | 11 |
| Hillpointe - Pointe Grand Statesboro | 10 |
| Hillpointe - Pointe Grand Beaufort | 9 |
| Hillpointe - Pointe Grand Fort Pierce | 5 |
| Hillpointe - Pointe Grand Yulee | 2 |

### Spot check: Sample 'missing' profiles

Checking whether these tenants actually appear in the API data and what charges they have:

- **Eion Madden** (eionmadden@gmail.com)
  - Property: Hillpointe - Pointe Grand Macon, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Michelle Sullivan** (futbol37@hotmail.com)
  - Property: Hillpointe - Pointe Grand Spartanburg, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Arianna Ciszczon** (ariannaciszczon@gmail.com)
  - Property: Hillpointe - Pointe Grand on Main, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Joshua Guerrero** (joshguerrero20132014@gmail.com)
  - Property: Hillpointe - Pointe Grand Brunswick, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Alan Valentin** (aceltic17@gmail.com)
  - Property: Hillpointe - Pointe Grand Plant City, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Marissa Monn** (marissa.monn11@gmail.com)
  - Property: Hillpointe - Pointe Grand Palm Coast, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Katharine Cutia** (katharinecutia@gmail.com)
  - Property: Hillpointe - Pointe Grand Brunswick Island View, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Toree Davis** (toreed19@gmail.com)
  - Property: Hillpointe - Pointe Grand Augusta Reservation Way, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Autumn Martin** (fallwolfmartin@gmail.com)
  - Property: Hillpointe - Pointe Grand Spring Hill, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Renee Pologe** (reneepologe@gmail.com)
  - Property: Hillpointe - Pointe Grand Augusta, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Lisa Robitaille** (robitaillelisam@gmail.com)
  - Property: Hillpointe - Pointe Grand Davenport, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Ashley Anderson** (wilsonelizabeth608@gmail.com)
  - Property: Hillpointe - Pointe Grand Dawsonville, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **angel boulware** (angelshoes8@icloud.com)
  - Property: Hillpointe - Pointe Grand Southlake, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Dorthalia Newberry** (drthl_smith@yahoo.com)
  - Property: Hillpointe - Pointe Grand Brunswick Island View, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Tryston Mckenna** (tryston.mckenna@gmail.com)
  - Property: Hillpointe - Pointe Grand Jacksonville West, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)

### Verdict

Of the **1269** 'missing' tenants:
- **1226** are not in the Entrata API data at all (may be past tenants or TC mismatch)
- **0** actually DO have pet charges in the API (matching bug -- tenant_code/email mismatch between Snowflake and API)
- **43** are genuinely in the API without pet charges (truly missing)

> **Stale profiles:** 1226 tenants have active PetScreening profiles but don't appear in the current Entrata API data. They may have moved out but their PetScreening profile wasn't deactivated, or the tenant_code in Snowflake doesn't match any customer_id in the API.

---
## Investigation 3: Scheduled vs Actual Charges

**Question:** `getLeases` returns `scheduledCharges`. How reliable are these as a proxy for actual revenue?

- Valid pet charges (deduped): **1191**
- Future-dated (scheduled, not yet billed): **98**
- Zero-amount (waived/placeholder): **15**
- On invalid intervals (excluded by app): **1191**

### Frequency distribution

| Frequency | Count |
|-----------|-------|
| Monthly | 1,067 |
| One-Time | 124 |

### Amount distribution by frequency

- **Monthly:** 1067 charges, $0-$140, mean=$26.33, median=$20.00
- **One-Time:** 124 charges, $0-$1550, mean=$396.77, median=$350.00

### Zero-amount charges detail

| Property | Code | Frequency | Start | Interval Status |
|----------|------|-----------|-------|----------------|
| Hillpointe - Pointe Grand Augusta | Pet Rent | Monthly | 2025-05-06 |  |
| Hillpointe - Pointe Grand Brick City | Pet Fee | One-Time | 2025-08-01 |  |
| Hillpointe - Pointe Grand Daytona | Pet Rent | Monthly | 2025-09-16 |  |
| Hillpointe - Pointe Grand Daytona | Pet Rent | Monthly | 2025-07-14 |  |
| Hillpointe - Pointe Grand DeLand | Pet Rent | Monthly | 2025-06-06 |  |
| Hillpointe - Pointe Grand Jacksonville West | Pet Rent | Monthly | 2026-01-16 |  |
| Hillpointe - Pointe Grand Jacksonville West | Pet Rent | Monthly | 2026-01-16 |  |
| Hillpointe - Pointe Grand Ocala | Pet Rent | Monthly | 2025-07-21 |  |
| Hillpointe - Pointe Grand Palm Coast | Pet Rent | Monthly | 2026-01-29 |  |
| Hillpointe - Pointe Grand Pendergrass | Pet Rent | Monthly | 2025-11-14 |  |
| Hillpointe - Pointe Grand Spring Hill | Pet Rent | Monthly | 2025-11-29 |  |
| Hillpointe - Pointe Grand Spring Hill | Pet Rent | Monthly | 2026-02-07 |  |
| Hillpointe - Pointe Grand Timber Ridge | Pet Rent | Monthly | 2025-06-13 |  |
| Hillpointe - Pointe Grand Warner Robins | Pet Rent | Monthly | 2025-04-06 |  |
| Hillpointe - Pointe Grand Warner Robins | Pet Rent | Monthly | 2025-06-10 |  |

### AR Transactions test

Testing `includeArTransactions=1` on **Hillpointe - Pointe Grand Augusta**:

- **AR transactions present:** 130 transactions on first lease
- Fields: `['id', 'transactionTypeId', 'chargeCodeId', 'chargeCodeName', 'leaseIntervalId', 'description', 'transactionDate', 'postDate', 'postMonth', 'balanceDue', 'amount', 'amountPaid']`
- Sample:
```json
{
  "id": 21201440,
  "transactionTypeId": "2",
  "chargeCodeId": 25839,
  "chargeCodeName": "Rent",
  "leaseIntervalId": 68320,
  "description": "Posted from 03/01/2026 to 03/31/2026",
  "transactionDate": "02/25/2026",
  "postDate": "03/01/2026",
  "postMonth": "03/01/2026",
  "balanceDue": 1455,
  "amount": 1455,
  "amountPaid": 0
}
```

---
## Critical Finding: Pagination Truncation

**37 out of 43 properties returned exactly 100 leases** (the per-page cap). This almost certainly means the API has more leases that we are not fetching. The pagination logic in both this script and `app.py` breaks after page 1 because the Entrata API does not return `meta.currentPage`/`meta.lastPage` in the response.

**Impact:** We are working with an incomplete dataset. Properties with more than 100 leases (likely most of them) are missing data. This affects:
- Revenue calculations (undercounting charges)
- Missing pet rent detection (may miss tenants who ARE paying on leases 101+)
- Before/after comparisons (skewed by which 100 leases the API returns first)

---
## Critical Finding: Empty Interval Status

**100% of scheduled charges** from this PMC have an empty `leaseIntervalId` status. The app's `interval_is_valid` filter expects 'current', 'past', or 'notice'. Since all statuses are empty, this filter would exclude ALL Entrata charges for this PMC. The investigation fell back to using all charges, but `app.py` may be silently discarding valid charge data.

---
## Critical Finding: Tenant Code Matching is Broken

**0 out of 1,460 profiles matched via tenant_code.** Every Snowflake profile has `tenant_code = None`, meaning the `f_leases.lease_source_external_id:tenant_code` extraction returns NULL for Entrata. All 191 paying matches came through **email matching only**. This is why only 13% of profiles matched as paying -- the primary matching path is completely non-functional for this PMC.

---
## Critical Finding: AR Transactions ARE Available

The `includeArTransactions=1` parameter **does return actual posted transactions** with fields like `transactionDate`, `postDate`, `amount`, `amountPaid`, and `balanceDue`. This means we can compare scheduled charges against actual posted/collected charges. The app currently only uses `scheduledCharges` but could use `arTransactions` for more accurate revenue data.

---
## Summary & Recommendations

### 1. Pre-PS Data Availability

**Status: Unreliable baseline for most properties.**

24/41 properties have insufficient pre-launch data. The Entrata API likely only returns scheduled charges from when the integration was set up, not full historical charge data.

**Recommendations:**
- Don't rely on before/after revenue comparison for Entrata PMCs
- Consider only showing post-launch trends, or marking the before/after comparison as 'insufficient data'
- Investigate whether Entrata has a separate historical charges endpoint

### 2. Missing Pet Rent Accuracy

**Status: Potential overcounting (1269 flagged as missing).**

Key issues found:
- **1226 stale profiles**: Tenants with active PetScreening profiles who are no longer in the API (possibly moved out).

**Recommendations:**
- Strengthen tenant matching: add email-based matching as primary (not just fallback) for Entrata
- Add lease_id-level dedup: if ANY customer on a lease has a pet charge, ALL customers on that lease should be excluded
- Consider adding a freshness check: exclude profiles where the tenant doesn't appear in current API data

### 3. Scheduled vs Actual Charges

**Status: Scheduled charges are the only data currently used, but AR transactions are available.**

- 15 zero-amount charges could inflate tenant counts if not filtered
- 98 future-dated charges may be included in current revenue
- `includeArTransactions=1` DOES return actual posted charges with amounts and payment status

**Recommendations:**
- Filter out $0-amount charges from revenue calculations
- Add a note in the UI that Entrata data reflects *scheduled* charges, not actual collections
- Consider capping charge dates to today to exclude future-scheduled charges from current revenue
- **Explore using AR transactions** (`includeArTransactions=1`) for actual revenue data instead of / in addition to scheduled charges

### 4. Pagination (CRITICAL)

**Status: Most properties are truncated at 100 leases.**

37/43 properties hit the 100-lease cap. The API likely has more data that we are not fetching.

**Recommendations:**
- Fix pagination: inspect the raw API response to determine the correct pagination fields
- If Entrata doesn't support pagination on `getLeases`, consider using a different endpoint or requesting larger page sizes
- At minimum, flag properties where `lease_count == per_page` as potentially truncated in the UI

### 5. Tenant Code Matching (CRITICAL)

**Status: Completely broken for Entrata. 0% match rate via tenant_code.**

All 191 paying matches came through email only. The `f_leases.lease_source_external_id:tenant_code` path returns NULL for all Entrata tenants.

**Recommendations:**
- Audit how `lease_source_external_id` is populated for Entrata in the data pipeline
- Make email the primary matching method for Entrata (not just a fallback)
- Consider matching by unit number as an additional signal
- The 87% "missing" rate (1269/1460) is almost certainly an overcount driven by the 100-lease pagination cap combined with broken TC matching

### 6. Interval Status Filter (CRITICAL)

**Status: All Entrata charges have empty interval status, causing the `interval_is_valid` filter to reject everything.**

**Recommendations:**
- For Entrata, skip the interval status filter or treat empty status as valid
- Alternatively, use the lease-level status (which may be populated) instead of the interval-level status