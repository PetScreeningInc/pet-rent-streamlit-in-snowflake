"""Tests for fetch_cache: hit/miss behavior, TTL expiry, error non-caching,
Entrata 4-tuple support, and force refresh."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch_cache


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    """These tests exercise the orchestration through the local backend —
    the Snowflake backend is integration-tested against the real table."""
    monkeypatch.setenv("PET_VALUE_CACHE_BACKEND", "local")


def _use_tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_cache, "CACHE_DIR", tmp_path / "fetch_cache")


def _props(*pids):
    return [{"PROPERTY_ID": pid, "PROPERTY_NAME": f"Prop{pid}", "PROPERTY_CODE": f"c{pid}"}
            for pid in pids]


def _charge(pid, amount="30"):
    return {"property_id": pid, "property_name": f"Prop{pid}",
            "charge_code": "PETRENT", "charge_amount": amount}


def _log(pid, status="Success (1 charges)", n=1):
    return {"property": f"Prop{pid}", "code": f"c{pid}", "status": status, "charges": n}


def test_miss_then_hit(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    calls = []

    def fetch_fn(props):
        calls.append([p["PROPERTY_ID"] for p in props])
        return [_charge(p["PROPERTY_ID"]) for p in props], [_log(p["PROPERTY_ID"]) for p in props]

    charges, log, ar, raw, info = fetch_cache.fetch_with_cache("yardi", _props(1, 2), fetch_fn)
    assert info == {"hits": 0, "misses": 2, "oldest": None, "backend": "local",
                    "extras_by_pid": {}}
    assert len(charges) == 2 and calls == [[1, 2]]

    charges2, log2, _, _, info2 = fetch_cache.fetch_with_cache("yardi", _props(1, 2), fetch_fn)
    assert info2["hits"] == 2 and info2["misses"] == 0
    assert len(calls) == 1  # API not called again
    assert len(charges2) == 2
    assert all(row["status"].startswith("Cached (") for row in log2)


def test_partial_hit_fetches_only_missing(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    fetch_fn = lambda props: (
        [_charge(p["PROPERTY_ID"]) for p in props],
        [_log(p["PROPERTY_ID"]) for p in props],
    )
    fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)

    calls = []
    def fetch_fn2(props):
        calls.append([p["PROPERTY_ID"] for p in props])
        return [_charge(p["PROPERTY_ID"]) for p in props], [_log(p["PROPERTY_ID"]) for p in props]

    charges, log, _, _, info = fetch_cache.fetch_with_cache("yardi", _props(1, 2), fetch_fn2)
    assert calls == [[2]]
    assert info["hits"] == 1 and info["misses"] == 1
    assert len(charges) == 2


def test_ttl_expiry(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    fetch_fn = lambda props: ([_charge(1)], [_log(1)])
    fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)

    # Age the entry beyond TTL
    payload = fetch_cache.load_property("yardi", 1, ttl_days=7)
    assert payload is not None
    payload["fetched_at"] = datetime.now() - timedelta(days=8)
    fetch_cache.store_property("yardi", 1, payload)

    assert fetch_cache.load_property("yardi", 1, ttl_days=7) is None
    _, _, _, _, info = fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)
    assert info["misses"] == 1


def test_errors_are_not_cached_but_appear_in_log(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    fetch_fn = lambda props: ([], [_log(1, status="Error: timeout", n=0)])
    charges, log, _, _, info = fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)
    assert charges == []
    assert log[0]["status"] == "Error: timeout"
    # Next call must retry (nothing cached)
    _, _, _, _, info2 = fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)
    assert info2["misses"] == 1


def test_empty_success_is_cached(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    calls = []
    def fetch_fn(props):
        calls.append(1)
        return [], [_log(1, status="Warning: No charges found", n=0)]
    fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)
    fetch_cache.fetch_with_cache("yardi", _props(1), fetch_fn)
    assert len(calls) == 1


def test_force_refresh_refetches_and_updates_cache(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    fetch_cache.fetch_with_cache("yardi", _props(1), lambda p: ([_charge(1, "30")], [_log(1)]))
    charges, _, _, _, info = fetch_cache.fetch_with_cache(
        "yardi", _props(1), lambda p: ([_charge(1, "99")], [_log(1)]), force_refresh=True
    )
    assert info["misses"] == 1
    assert charges[0]["charge_amount"] == "99"
    # The refreshed value replaced the cached one
    charges2, _, _, _, _ = fetch_cache.fetch_with_cache(
        "yardi", _props(1), lambda p: ([], []),
    )
    assert charges2[0]["charge_amount"] == "99"


def test_entrata_four_tuple_roundtrip(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ar_row = {"property_id": 1, "amount": 35}
    raw_row = {"property_name": "Prop1", "lease_id": "L1"}
    fetch_fn = lambda props: ([_charge(1)], [_log(1)], [ar_row], [raw_row])
    charges, log, ar, raw, _ = fetch_cache.fetch_with_cache("entrata", _props(1), fetch_fn)
    assert ar == [ar_row] and raw == [raw_row]
    # From cache
    charges2, _, ar2, raw2, info = fetch_cache.fetch_with_cache(
        "entrata", _props(1), lambda p: ([], [])
    )
    assert info["hits"] == 1
    assert ar2 == [ar_row] and raw2 == [raw_row]


def test_mode_isolates_realpage_variants(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    fetch_cache.fetch_with_cache("real_page", _props(1), lambda p: ([_charge(1)], [_log(1)]), mode="hybrid")
    _, _, _, _, info = fetch_cache.fetch_with_cache(
        "real_page", _props(1), lambda p: ([_charge(1)], [_log(1)]), mode="live"
    )
    assert info["misses"] == 1  # live mode does not reuse hybrid payloads


def test_clear_and_stats(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    fetch_cache.fetch_with_cache("yardi", _props(1, 2), lambda p: (
        [_charge(x["PROPERTY_ID"]) for x in p], [_log(x["PROPERTY_ID"]) for x in p]
    ))
    stats = fetch_cache.cache_stats()
    assert stats["total_entries"] == 2
    assert fetch_cache.clear_cache() == 2
    assert fetch_cache.cache_stats()["total_entries"] == 0
