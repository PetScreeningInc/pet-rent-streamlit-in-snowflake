# Entrata Presentation — Data Plan
**Date:** 2026-03-17

## What the boss needs vs what we can deliver

### 1. "In 2025, we increased pet rent by $X across mutual clients"
**Data source:** `stg_pmc_integrations_entrata__getleases` (scheduled charges)
**Method:** Lift analysis — same 6-and-6 methodology from the app, but scoped to Entrata-only properties with 2025 data
**Query:** See Query A below

### 2. "At a 5% cap rate, that equates to $X in asset value created"
**Formula:** `annual_pet_rent_increase / 0.05`
**Derived from:** Monthly lift × 12 / 0.05

### 3. "Enhanced integration generates XX% more pet-related revenue vs basic"
**Data source:** `stg_petscreening__integrations.settings` — need to identify which properties have enhanced (required new flow) vs basic
**Plus:** Same getleases charge data, split by integration type
**Query:** See Query B below

### 4. "Pet leak" — uncollected pet rent (active HHP not paying)
**Data source:** Same missing-pet-rent logic from the app — `petscreening__user_enriched` with `household + active + compliant` joined against getleases charges
**Query:** See Query C below
