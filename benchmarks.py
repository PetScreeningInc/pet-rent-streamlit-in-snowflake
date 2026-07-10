"""
benchmarks.py — pet-rent pricing summary built from the portfolio's OWN data.

Earlier versions compared a portfolio against PetScreening-network pools
(state-level fee medians from the Entrata staging table, plus an
assistance-animal share benchmark). Both were removed in July 2026:

- The assistance-animal ratio is gone entirely (cut from the PDF/Summary).
- The fee comparison no longer uses external "market" data — provenance was
  unclear and multi-state parents made a single state figure misleading.

What remains is a defensible internal view: the distribution of recurring
pet rent the portfolio itself charges, computed from the same live per-
property fee estimates the rest of the report already uses
(`_estimate_property_fees` in app.py). A richer benchmark (own-book
percentiles by state/zip from the RAW.MISC snapshot tables) is planned as a
separate "Pet Rent Pricing Benchmark" table — see git history for the old
pool queries when that work starts.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

FEE_MIN, FEE_MAX = 5, 200          # plausible monthly pet rent range


def build_pet_rent_pricing(fee_by_prop: Dict[str, float],
                           paying_count: int = 0) -> Optional[Dict[str, Any]]:
    """Summarize what THIS portfolio charges for recurring pet rent.

    fee_by_prop : {property_name: typical monthly recurring pet fee} for the
                  selection (use _estimate_property_fees output).
    paying_count: tenants currently paying recurring pet charges (context).
    Returns None when no property has a plausible recurring fee.
    """
    import statistics

    fees = sorted(v for v in fee_by_prop.values() if FEE_MIN <= v <= FEE_MAX)
    if not fees:
        return None

    n = len(fees)
    out: Dict[str, Any] = {
        "n_props": n,
        "median": float(statistics.median(fees)),
        "min": float(fees[0]),
        "max": float(fees[-1]),
        "paying_count": int(paying_count or 0),
    }
    if n >= 4:
        q = statistics.quantiles(fees, n=4)
        out["p25"], out["p75"] = float(q[0]), float(q[2])
    else:
        out["p25"], out["p75"] = out["min"], out["max"]
    return out
