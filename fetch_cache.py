"""
fetch_cache.py
==============

Persistent cache for per-property charge fetches, shared by the Streamlit
app and the batch scripts.

Why: every fetch re-hits the Yardi/Entrata/RealPage APIs even when the same
parent company was pulled days earlier — slow, and it burns rate-limit
headroom shared with the dev team's pipelines. Charge data changes on a
monthly cadence, so re-pulling within a TTL (default 7 days) adds nothing.

Backends (PET_VALUE_CACHE_BACKEND env var):
  - ``snowflake`` (default) — appends to a shared, append-only Snowflake
    table (PET_VALUE_CACHE_TABLE env var, default
    ``RAW.MISC.CACHED_PET_VALUE_DATA``). One row per charge record plus a
    ``meta`` row per property-fetch. Nothing is ever deleted, so over time
    the table doubles as OUR OWN trustworthy history of what the PMS APIs
    returned on each date — independent of the EKS/Airbyte staging jobs.
    Reads take the latest fetch per property within the TTL. Works from any
    machine with Snowflake access.
    NOTE: the DEVELOPER role currently lacks CREATE TABLE on RAW.MISC, so
    the working config (.env) points at RAW.PMC_EXTERNAL_INTEGRATIONS —
    flip PET_VALUE_CACHE_TABLE back to RAW.MISC once an admin grants it.
  - ``local`` — gzip pickles under ``fetch_cache/`` (offline fallback,
    used by the test suite).

Rules (both backends):
  - Property-level granularity: a pull for one parent warms the cache for
    any overlapping selection, including combined multi-system pulls.
  - Error results are NEVER cached; empty-but-successful fetches are.
  - ``mode`` disambiguates RealPage hybrid/live/staging payloads.

Public API:
    fetch_with_cache(pmc_system, properties, fetch_fn, ...) -> 5-tuple
    get_backend() / LocalCacheBackend / SnowflakeCacheBackend
    cache_stats() -> dict
    clear_cache(pmc_system=None) -> int   (local backend only)
"""
from __future__ import annotations

import gzip
import json
import os
import pickle
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Load .env from this module's directory so standalone scripts (e.g.
# reclaim_report.py) pick up PET_VALUE_CACHE_TABLE without importing app.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

CACHE_DIR = Path(__file__).resolve().parent / "fetch_cache"
DEFAULT_TABLE = "RAW.MISC.CACHED_PET_VALUE_DATA"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_TABLE_SAFE = re.compile(r"^[A-Za-z0-9_.\"]+$")

_RECORD_TYPES = (
    ("charges", "charge"),
    ("ar_charges", "ar_charge"),
    ("raw_lease_arrays", "raw_lease"),
)


# ─── Local backend (files) ──────────────────────────────────────────────

def _entry_path(pmc_system: str, property_id: Any, mode: str = "") -> Path:
    system_dir = CACHE_DIR / _SAFE.sub("_", str(pmc_system) or "unknown")
    suffix = f"__{_SAFE.sub('_', mode)}" if mode else ""
    return system_dir / f"{_SAFE.sub('_', str(property_id))}{suffix}.pkl.gz"


def load_property(pmc_system: str, property_id: Any, ttl_days: float,
                  mode: str = "") -> Optional[Dict[str, Any]]:
    """Return the locally cached payload if younger than ttl_days."""
    path = _entry_path(pmc_system, property_id, mode)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rb") as f:
            payload = pickle.load(f)
        fetched_at = payload.get("fetched_at")
        if not isinstance(fetched_at, datetime):
            return None
        if datetime.now() - fetched_at > timedelta(days=float(ttl_days)):
            return None
        return payload
    except Exception:  # noqa: BLE001 — a corrupt entry is just a miss
        return None


def store_property(pmc_system: str, property_id: Any, payload: Dict[str, Any],
                   mode: str = "") -> None:
    path = _entry_path(pmc_system, property_id, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


class LocalCacheBackend:
    name = "local"
    location = str(CACHE_DIR)

    def load_many(self, pmc_system: str, mode: str, prop_ids: List[str],
                  ttl_days: float) -> Dict[str, Dict[str, Any]]:
        out = {}
        for pid in prop_ids:
            payload = load_property(pmc_system, pid, ttl_days, mode)
            if payload is not None:
                out[pid] = payload
        return out

    def store_many(self, pmc_system: str, mode: str,
                   entries: Dict[str, Dict[str, Any]]) -> None:
        for pid, payload in entries.items():
            try:
                store_property(pmc_system, pid, payload, mode)
            except Exception:  # noqa: BLE001 — cache failure must not kill a fetch
                pass

    def stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {"backend": "local", "location": str(CACHE_DIR),
                                 "systems": {}, "total_entries": 0, "total_bytes": 0}
        if not CACHE_DIR.exists():
            return stats
        for system_dir in sorted(p for p in CACHE_DIR.iterdir() if p.is_dir()):
            files = list(system_dir.glob("*.pkl.gz"))
            size = sum(f.stat().st_size for f in files)
            stats["systems"][system_dir.name] = {"entries": len(files), "bytes": size}
            stats["total_entries"] += len(files)
            stats["total_bytes"] += size
        return stats


# ─── Snowflake backend (append-only shared table) ───────────────────────

class SnowflakeCacheBackend:
    name = "snowflake"

    def __init__(self, table: Optional[str] = None, connection_factory=None):
        self.table = table or os.getenv("PET_VALUE_CACHE_TABLE", DEFAULT_TABLE)
        if not _TABLE_SAFE.match(self.table):
            raise ValueError(f"Unsafe cache table name: {self.table!r}")
        self.location = self.table
        self._connection_factory = connection_factory
        self._conn = None
        self._table_ready = False

    def _connection(self):
        if self._conn is None:
            if self._connection_factory is not None:
                self._conn = self._connection_factory()
            else:
                from snowflake_auth import get_snowflake_connection
                self._conn = get_snowflake_connection()
        return self._conn

    def _ensure_table(self) -> None:
        if self._table_ready:
            return
        cur = self._connection().cursor()
        try:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    FETCH_ID    VARCHAR(36)   NOT NULL,
                    PMC_SYSTEM  VARCHAR(32)   NOT NULL,
                    FETCH_MODE  VARCHAR(32),
                    PROPERTY_ID VARCHAR(64)   NOT NULL,
                    FETCHED_AT  TIMESTAMP_NTZ NOT NULL,
                    RECORD_TYPE VARCHAR(16)   NOT NULL,
                    RECORD_SEQ  NUMBER,
                    DATA        VARIANT
                )
            """)
            self._table_ready = True
        finally:
            cur.close()

    def load_many(self, pmc_system: str, mode: str, prop_ids: List[str],
                  ttl_days: float) -> Dict[str, Dict[str, Any]]:
        if not prop_ids:
            return {}
        self._ensure_table()
        cutoff = (datetime.now() - timedelta(days=float(ttl_days))).strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ", ".join(["%s"] * len(prop_ids))
        sql = f"""
            SELECT PROPERTY_ID, RECORD_TYPE, RECORD_SEQ, DATA, FETCHED_AT
            FROM {self.table}
            WHERE PMC_SYSTEM = %s
              AND COALESCE(FETCH_MODE, '') = %s
              AND PROPERTY_ID IN ({placeholders})
              AND FETCHED_AT >= TO_TIMESTAMP_NTZ(%s)
            QUALIFY FETCH_ID = FIRST_VALUE(FETCH_ID) OVER (
                PARTITION BY PROPERTY_ID ORDER BY FETCHED_AT DESC, FETCH_ID
            )
            ORDER BY PROPERTY_ID, RECORD_TYPE, RECORD_SEQ
        """
        cur = self._connection().cursor()
        try:
            cur.execute(sql, [pmc_system, mode or ""] + [str(p) for p in prop_ids] + [cutoff])
            rows = cur.fetchall()
        finally:
            cur.close()

        _type_to_key = {rt: key for key, rt in _RECORD_TYPES}
        out: Dict[str, Dict[str, Any]] = {}
        for pid, record_type, _seq, data, fetched_at in rows:
            payload = out.setdefault(str(pid), {
                "fetched_at": fetched_at,
                "charges": [], "ar_charges": [], "raw_lease_arrays": [],
                "log_row": None, "_has_meta": False,
            })
            try:
                obj = json.loads(data) if isinstance(data, str) else data
            except (TypeError, ValueError):
                continue
            if record_type == "meta":
                payload["_has_meta"] = True
                payload["log_row"] = (obj or {}).get("log_row")
                payload["extras"] = (obj or {}).get("extras")
            else:
                key = _type_to_key.get(record_type)
                if key:
                    payload[key].append(obj)

        # Only fetches that wrote their meta row count as complete.
        complete = {}
        for pid, payload in out.items():
            if payload.pop("_has_meta", False):
                complete[pid] = payload
        return complete

    def store_many(self, pmc_system: str, mode: str,
                   entries: Dict[str, Dict[str, Any]]) -> None:
        if not entries:
            return
        self._ensure_table()
        rows: List[Tuple] = []
        for pid, payload in entries.items():
            fetch_id = str(uuid.uuid4())
            ts = payload.get("fetched_at") or datetime.now()
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            meta = {
                "log_row": payload.get("log_row"),
                "counts": {key: len(payload.get(key) or []) for key, _ in _RECORD_TYPES},
                # Provider extras (e.g. the RealPage live resident roster) —
                # persisted so cache-hit runs filter reports identically to
                # fresh runs.
                "extras": payload.get("extras"),
            }
            rows.append((fetch_id, pmc_system, mode or "", str(pid), ts_str,
                         "meta", 0, json.dumps(meta, default=str)))
            for key, record_type in _RECORD_TYPES:
                for i, rec in enumerate(payload.get(key) or []):
                    rows.append((fetch_id, pmc_system, mode or "", str(pid), ts_str,
                                 record_type, i, json.dumps(rec, default=str)))

        insert_prefix = (
            f"INSERT INTO {self.table} "
            "(FETCH_ID, PMC_SYSTEM, FETCH_MODE, PROPERTY_ID, FETCHED_AT, RECORD_TYPE, RECORD_SEQ, DATA) "
            "SELECT column1, column2, column3, column4, TO_TIMESTAMP_NTZ(column5), "
            "column6, column7, PARSE_JSON(column8) FROM VALUES "
        )
        chunk_size = 1000
        cur = self._connection().cursor()
        try:
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                values_sql = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
                params: List[Any] = []
                for r in chunk:
                    params.extend(r)
                cur.execute(insert_prefix + values_sql, params)
        finally:
            cur.close()

    def stats(self) -> Dict[str, Any]:
        self._ensure_table()
        cur = self._connection().cursor()
        try:
            cur.execute(f"""
                SELECT PMC_SYSTEM,
                       COUNT(DISTINCT PROPERTY_ID) AS properties,
                       COUNT(DISTINCT FETCH_ID)    AS fetches,
                       COUNT(*)                    AS records,
                       MAX(FETCHED_AT)             AS last_fetch
                FROM {self.table}
                GROUP BY 1 ORDER BY 1
            """)
            rows = cur.fetchall()
        finally:
            cur.close()
        stats: Dict[str, Any] = {"backend": "snowflake", "location": self.table,
                                 "systems": {}, "total_entries": 0, "total_records": 0}
        for system, props, fetches, records, last_fetch in rows:
            stats["systems"][system] = {
                "properties": props, "fetches": fetches,
                "records": records, "last_fetch": last_fetch,
            }
            stats["total_entries"] += props
            stats["total_records"] += records
        return stats


# ─── Backend resolution ─────────────────────────────────────────────────

_backend_singleton: Optional[Any] = None


def get_backend():
    """Resolve the active cache backend (PET_VALUE_CACHE_BACKEND env var)."""
    global _backend_singleton
    choice = os.getenv("PET_VALUE_CACHE_BACKEND", "snowflake").strip().lower()
    if choice == "local":
        return LocalCacheBackend()
    if _backend_singleton is None or not isinstance(_backend_singleton, SnowflakeCacheBackend):
        _backend_singleton = SnowflakeCacheBackend()
    return _backend_singleton


# ─── Shared helpers ─────────────────────────────────────────────────────

def _prop_id(prop: Dict[str, Any]) -> str:
    return str(prop.get("PROPERTY_ID") or prop.get("property_id") or "")


def _prop_name(prop: Dict[str, Any]) -> str:
    return str(prop.get("PROPERTY_NAME") or prop.get("property_name") or "")


def _age_label(fetched_at: datetime) -> str:
    age = datetime.now() - fetched_at
    if age < timedelta(hours=1):
        return f"{int(age.total_seconds() // 60)}m ago"
    if age < timedelta(days=1):
        return f"{int(age.total_seconds() // 3600)}h ago"
    return f"{age.days}d ago"


def fetch_with_cache(
    pmc_system: str,
    properties: List[Dict[str, Any]],
    fetch_fn: Callable[[List[Dict[str, Any]]], Tuple],
    ttl_days: float = 7.0,
    force_refresh: bool = False,
    mode: str = "",
    backend: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]],
           List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch charges for ``properties``, reusing cached per-property results.

    ``fetch_fn(props_subset)`` must return either
    ``(charges, log)`` or ``(charges, log, ar_charges, raw_lease_arrays)``
    — the same shapes the app's fetchers already produce.

    Returns ``(charges, log, ar_charges, raw_lease_arrays, cache_info)``
    where cache_info = {"hits": int, "misses": int, "oldest": datetime|None,
    "backend": str}.
    """
    if backend is None:
        backend = get_backend()

    pid_list = [p for p in (_prop_id(prop) for prop in properties) if p]
    payloads: Dict[str, Dict[str, Any]] = {}
    if not force_refresh and pid_list:
        try:
            payloads = backend.load_many(pmc_system, mode, pid_list, ttl_days)
        except Exception as exc:  # noqa: BLE001 — cache trouble = treat all as misses
            print(f"[fetch_cache] {backend.name} cache read failed "
                  f"({str(exc)[:200]}) — fetching everything fresh.")
            payloads = {}

    misses = [prop for prop in properties if _prop_id(prop) not in payloads]

    fresh_log_by_name: Dict[str, Dict[str, Any]] = {}
    if misses:
        result = fetch_fn(misses)
        extras_by_pid = {}
        if len(result) == 2:
            charges, log = result
            ar_charges, raw_arrays = [], []
        elif len(result) == 4:
            charges, log, ar_charges, raw_arrays = result
        else:
            charges, log, ar_charges, raw_arrays, extras_by_pid = result

        fresh_log_by_name = {str(r.get("property", "")): r for r in log}

        charges_by_pid: Dict[str, List] = {}
        for r in charges:
            charges_by_pid.setdefault(str(r.get("property_id", "")), []).append(r)
        ar_by_pid: Dict[str, List] = {}
        for r in ar_charges:
            ar_by_pid.setdefault(str(r.get("property_id", "")), []).append(r)
        raw_by_name: Dict[str, List] = {}
        for r in raw_arrays:
            raw_by_name.setdefault(str(r.get("property_name", "")), []).append(r)

        now = datetime.now()
        to_store: Dict[str, Dict[str, Any]] = {}
        for prop in misses:
            pid = _prop_id(prop)
            name = _prop_name(prop)
            log_row = fresh_log_by_name.get(name)
            payload = {
                "fetched_at": now,
                "charges": charges_by_pid.get(pid, []),
                "ar_charges": ar_by_pid.get(pid, []),
                "raw_lease_arrays": raw_by_name.get(name, []),
                "log_row": log_row or {
                    "property": name,
                    "code": str(prop.get("PROPERTY_CODE") or prop.get("property_code") or ""),
                    "status": "Warning: no log entry from fetcher",
                    "charges": len(charges_by_pid.get(pid, [])),
                },
                "extras": (extras_by_pid or {}).get(pid),
            }
            payloads[pid] = payload
            # Never cache errors — retry them next run.
            status = str(payload["log_row"].get("status", ""))
            if pid and not status.startswith("Error"):
                to_store[pid] = payload
        if to_store:
            try:
                backend.store_many(pmc_system, mode, to_store)
            except Exception as exc:  # noqa: BLE001 — cache failure must not kill a fetch
                print(f"[fetch_cache] {backend.name} cache write failed "
                      f"({str(exc)[:200]}) — results NOT cached this run.")

    all_charges: List[Dict[str, Any]] = []
    all_ar: List[Dict[str, Any]] = []
    all_raw: List[Dict[str, Any]] = []
    merged_log: List[Dict[str, Any]] = []
    hits = 0
    oldest: Optional[datetime] = None

    miss_pids = {_prop_id(p) for p in misses}
    for prop in properties:
        pid = _prop_id(prop)
        payload = payloads.get(pid)
        if payload is None:
            continue  # errored property: its log row is added below from fresh log
        all_charges.extend(payload.get("charges") or [])
        all_ar.extend(payload.get("ar_charges") or [])
        all_raw.extend(payload.get("raw_lease_arrays") or [])
        log_row = dict(payload.get("log_row") or {})
        if pid not in miss_pids:
            hits += 1
            fetched_at = payload.get("fetched_at")
            if isinstance(fetched_at, datetime):
                if oldest is None or fetched_at < oldest:
                    oldest = fetched_at
                log_row["status"] = f"Cached ({_age_label(fetched_at)}) — " + str(log_row.get("status", ""))
        merged_log.append(log_row)

    # Log rows for errored (uncached, unpayloaded) properties
    for prop in misses:
        if _prop_id(prop) in payloads:
            continue
        row = fresh_log_by_name.get(_prop_name(prop))
        if row:
            merged_log.append(row)

    cache_info = {"hits": hits, "misses": len(misses), "oldest": oldest,
                  "backend": getattr(backend, "name", "?"),
                  "extras_by_pid": {
                      pid: p.get("extras") for pid, p in payloads.items()
                      if p.get("extras") is not None
                  }}
    return all_charges, merged_log, all_ar, all_raw, cache_info


# ─── Maintenance ────────────────────────────────────────────────────────

def clear_cache(pmc_system: Optional[str] = None) -> int:
    """Delete LOCAL cache entries. The Snowflake table is append-only by
    design (it doubles as a fetch-history record) — to bypass it, turn off
    'Reuse recent fetches' / pass --no-cache instead."""
    removed = 0
    if not CACHE_DIR.exists():
        return 0
    roots = (
        [CACHE_DIR / _SAFE.sub("_", pmc_system)] if pmc_system else
        [p for p in CACHE_DIR.iterdir() if p.is_dir()]
    )
    for root in roots:
        if not root.exists():
            continue
        for f in root.glob("*.pkl.gz"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def cache_stats() -> Dict[str, Any]:
    """Stats for the active backend (falls back to local stats on error)."""
    try:
        return get_backend().stats()
    except Exception as exc:  # noqa: BLE001
        stats = LocalCacheBackend().stats()
        stats["error"] = str(exc)[:200]
        return stats


# ─── Report snapshots ("what changed since last report") ────────────────

def _slugify_label(label: str) -> str:
    return (_SAFE.sub("_", str(label))[:60] or "report").strip("_")


def save_report_snapshot(label: str, metrics: Dict[str, Any]) -> None:
    """Append a report-metrics snapshot keyed by selection label. Powers the
    'since last report' deltas in the Summary tab. Snowflake backend writes a
    RECORD_TYPE='snapshot' row into the cache table; local backend writes a
    JSON file."""
    backend = get_backend()
    now = datetime.now()
    if isinstance(backend, SnowflakeCacheBackend):
        backend._ensure_table()
        cur = backend._connection().cursor()
        try:
            cur.execute(
                f"INSERT INTO {backend.table} "
                "(FETCH_ID, PMC_SYSTEM, FETCH_MODE, PROPERTY_ID, FETCHED_AT, RECORD_TYPE, RECORD_SEQ, DATA) "
                "SELECT column1, column2, column3, column4, TO_TIMESTAMP_NTZ(column5), "
                "column6, column7, PARSE_JSON(column8) FROM VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [str(uuid.uuid4()), "_report", "", _slugify_label(label),
                 now.strftime("%Y-%m-%d %H:%M:%S"), "snapshot", 0,
                 json.dumps(metrics, default=str)],
            )
        finally:
            cur.close()
        return
    d = CACHE_DIR / "_report_snapshots"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{_slugify_label(label)}_{now.strftime('%Y%m%d_%H%M%S')}.json",
              "w", encoding="utf-8") as f:
        json.dump({"_saved_at": now.isoformat(), **metrics}, f, default=str)


def load_last_report_snapshot(label: str):
    """(metrics, saved_at) of the most recent snapshot for label, or (None, None)."""
    backend = get_backend()
    try:
        if isinstance(backend, SnowflakeCacheBackend):
            backend._ensure_table()
            cur = backend._connection().cursor()
            try:
                cur.execute(
                    f"SELECT DATA, FETCHED_AT FROM {backend.table} "
                    "WHERE PMC_SYSTEM = '_report' AND PROPERTY_ID = %s "
                    "AND RECORD_TYPE = 'snapshot' ORDER BY FETCHED_AT DESC LIMIT 1",
                    [_slugify_label(label)],
                )
                row = cur.fetchone()
            finally:
                cur.close()
            if not row:
                return None, None
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            return data, row[1]
        d = CACHE_DIR / "_report_snapshots"
        files = sorted(d.glob(f"{_slugify_label(label)}_*.json")) if d.exists() else []
        if not files:
            return None, None
        with open(files[-1], encoding="utf-8") as f:
            obj = json.load(f)
        return obj, obj.pop("_saved_at", None)
    except Exception:  # noqa: BLE001 — deltas are a bonus, never break a report
        return None, None


# ─── Flagged-tenant cohorts (reclaim-rate OKR tracking) ─────────────────

def save_flagged_tenants(label: str, pmc_system: str,
                         property_ids: List[Any],
                         flags: List[Dict[str, Any]],
                         as_of: Optional[datetime] = None) -> None:
    """Persist the individual tenants a report flagged, so a later run can
    measure the RECLAIM RATE: of the tenants flagged at baseline, who is
    paying now? This is the cohort store behind the solutions-org OKR
    ("increase PMC pet revenue reclaim rate on the obvious cases").

    One 'flag_run' marker row per run (which properties were covered, so
    zero-flag properties still count) + one 'flag' row per tenant with the
    reason ('missing_pet_rent', 'Unresolved assistance request', ...).
    Rows live in the same cache table under PMC_SYSTEM = '_flags'.
    """
    backend = get_backend()
    now = as_of or datetime.now()  # as_of backdates cohorts from old exports
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    run_id = str(uuid.uuid4())

    reason_counts: Dict[str, int] = {}
    for f in flags:
        r = str(f.get("reason") or "unknown")
        reason_counts[r] = reason_counts.get(r, 0) + 1

    marker = {
        "label": label,
        "source_system": pmc_system,
        "property_ids": [str(p) for p in property_ids],
        "n_flags": len(flags),
        "reason_counts": reason_counts,
    }

    if isinstance(backend, SnowflakeCacheBackend):
        backend._ensure_table()
        rows: List[Tuple] = [(run_id, "_flags", pmc_system or "", _slugify_label(label),
                              ts, "flag_run", 0, json.dumps(marker, default=str))]
        for i, f in enumerate(flags):
            rows.append((run_id, "_flags", pmc_system or "",
                         str(f.get("property_id") or ""), ts, "flag", i + 1,
                         json.dumps({**f, "label": label}, default=str)))
        insert_prefix = (
            f"INSERT INTO {backend.table} "
            "(FETCH_ID, PMC_SYSTEM, FETCH_MODE, PROPERTY_ID, FETCHED_AT, RECORD_TYPE, RECORD_SEQ, DATA) "
            "SELECT column1, column2, column3, column4, TO_TIMESTAMP_NTZ(column5), "
            "column6, column7, PARSE_JSON(column8) FROM VALUES "
        )
        cur = backend._connection().cursor()
        try:
            for start in range(0, len(rows), 1000):
                chunk = rows[start:start + 1000]
                values_sql = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
                params: List[Any] = []
                for r in chunk:
                    params.extend(r)
                cur.execute(insert_prefix + values_sql, params)
        finally:
            cur.close()
        return

    d = CACHE_DIR / "_flag_runs"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{_slugify_label(label)}_{now.strftime('%Y%m%d_%H%M%S')}.json",
              "w", encoding="utf-8") as fh:
        json.dump({"marker": marker, "saved_at": ts, "flags": flags}, fh, default=str)
