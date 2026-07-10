# Property Manager / Ownership-Change Signals from PMS APIs

**Date:** 2026-07-05
**Context:** Properties get bought and sold constantly and PetScreening
often doesn't know who currently operates a property (PM, regional manager)
— internal CRM data isn't trusted. Question: what do the PMS APIs we
already have access to expose? All findings below verified empirically
against production endpoints with our existing credentials.

## What works TODAY (no new access needed)

> **Run it:** `python pm_roster.py --ids 1575,1576` (or `--parent` /
> `--ancestry`) produces a CSV of every contact field below per property.

### Yardi — `Export_PropertyAttributeData` ✅ **NAMED MANAGERS BY ROLE** (verified live)

The WSDL on our licensed `ItfResidentData` interface exposes 37 operations
(we only used GetRentroll). `Export_PropertyAttributeData` returns each
property's attribute set — which clients use for **staff assignments**.
Real example (NRES Management — Sovereign at Overland Park, 2026-07-05):

| Field | Value |
|---|---|
| Regional Manager | Danielle Allen |
| Director of Operations | Missy Singer |
| Accounting | Victoria |
| Region | Midwest |

`GetContactRoles` returns the client's role taxonomy — including
**Property Manager, Regional Manager, VP Operations, VP/SVP Asset Mgmt,
Director of Operations, Owner, CFO** — exactly the role levels we need to
segment outreach. Caveats: attributes are client-configured (coverage
varies by client; some populate none), and values are **names, not
emails** — pair with the office-email sources below or HubSpot enrichment.
`GetContacts` turned out to be resident-scoped (tenant contacts), not
staff.

### Yardi — `GetPropertyConfigurations` ✅ (verified live)

Same `ItfResidentData` interface and license we already use for
GetRentroll. One call per client database returns the **complete list of
properties that client currently operates**: property code, marketing
name, full address, and active AR/AP accounting periods.

No manager names — but this is the **bought/sold detection primitive**:

- Snapshot every client's property list weekly (cheap: one call per
  client database, no per-property calls).
- A property that **disappears** from a client's database → they no longer
  manage it (sold / management change).
- A property (matched by address) that **appears** in another client's
  database → that's the new operator.
- Diff against `D_PROPERTIES` to flag PetScreening records pointing at
  stale operators.

### Entrata — `getProperties` ✅ (verified live)

Works with our existing API key per corp. Returns per property: marketing
name, full address, **the property's current office email**
(e.g. `apexinfo@jvmrealty.com`), website, hours. The email is maintained
by whoever operates the property *today* — a reliable outreach address
that self-heals across management changes. Same disappearance/appearance
diffing works per corp as with Yardi.

## Sweep results (2026-07-05, live sample)

**Yardi** — 47 properties across 25 distinct client databases (of 427 total):

- **47/47 responded** (on a Saturday — the weekday-window restriction is
  clearly per-PMC, not universal).
- **11/47 (23%) have role attributes populated** — client-configured, so
  coverage is all-or-nothing per client: when a client uses them, every
  property has them; many clients configure none.
- Most common fields: Regional Manager (7), Accounting Manager, Asset
  Manager, District Manager, VP / Senior VP / Executive VP, Ownership Name.
- Real examples: Thrive Communities ("Regional Manager: Lindsey Grippo"),
  Avenue5 ("Asset Manager: Ed Hansen"), Towbes ("Manager: Karen Mims").
- **0/47 had emails in attribute values** — Yardi gives names + roles only.

**Entrata** — 36 properties across 12 corps (of 204):

- **29/36 (81%) return an office email** from `getProperties`; ~10% are
  personal-format (e.g. `willow.e@whproperty-leasing.com`), the rest are
  property inboxes (`wynstone@advenirliving.com`) — still current-operator
  addresses that self-heal across management changes.

**The unlock — Yardi names × PetScreening CRM emails (verified):**

Yardi tells us WHO manages the property *today*; our own
`stg_petscreening__property_manager_permissions_only` + `user_enriched`
tables have manager EMAILS (of unknown freshness). A name match between
the two means the CRM contact is confirmed current by the operator's own
system. Live demo, property 1575:

> Yardi: "Regional Manager: **Danielle Allen**" →
> PS CRM: **dallen@nolanliving.com / dallen@nolanred.com** ✅ name-verified

`pm_roster.py` performs this join automatically and emits
"Regional Manager — email (PS CRM, name-verified)" rows. The inverse is
also an insight: a Yardi-named manager *absent* from PS CRM (e.g. Missy
Singer, Director of Operations) is a current decision-maker we have no
relationship with — a prospecting list, for free.

## Gated — concrete asks

| System | Endpoint | Status | Ask |
|---|---|---|---|
| Entrata | `getPhoneNumber` | exists; "application is not allowed to make this call" | ask Entrata to enable for our integration |
| Entrata | staff/agent-level methods | not under the method names probed; Entrata's API catalog has per-method permissions | ask Entrata which property-staff methods exist and to enable them |
| RealPage | `getunitlist` | exists; license key not authorized | bundle with the `getresidentledger` authorization request |
| RealPage | property/site info ops | not on our `Pet_Screening.svc` contract | ask RealPage what property-metadata operations can be added |
| Yardi | manager/agent endpoints | ItfResidentData doesn't carry staff data; other Yardi interfaces (e.g. property mgmt config) need their own license | ask clients'/Yardi channel about an interface that exposes property staff |

## Recommendation (standalone mini-project)

A weekly "operator roster" job:
1. `GetPropertyConfigurations` per Yardi client DB + `getProperties` per
   Entrata corp (both already authorized; ~one call per client).
2. Land snapshots in Snowflake (same append-only pattern as
   `CACHED_PET_VALUE_DATA`).
3. Diff week-over-week: departures, arrivals, address matches across
   clients → a "management change" feed with the new operator's office
   email (Entrata) or client identity (Yardi).

That feed directly answers "who do we reach out to now" for two of the
three major PMS ecosystems, with zero new permissions.
