"""Snowflake connection — works in Snowflake-hosted Streamlit (SiS) or locally via .env."""

import os
from pathlib import Path
from typing import Optional

import streamlit as st
from snowflake.snowpark import Session


def _load_private_key_bytes(key_path: str, passphrase: Optional[str]) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    p = Path(key_path).expanduser()
    pw_bytes = passphrase.encode() if passphrase else None
    with open(p, "rb") as f:
        pkey = serialization.load_pem_private_key(f.read(), password=pw_bytes, backend=default_backend())
    return pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@st.cache_resource
def get_session() -> Session:
    """Returns a Snowpark Session — works in Snowflake-hosted Streamlit or locally via .env."""
    # 1. Snowflake-hosted Streamlit: use active session
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        pass

    # 2. Local: build from .env file
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    params = {
        "account":   os.environ["SNOWFLAKE_ACCOUNT"],
        "user":      os.environ["SNOWFLAKE_USER"],
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        "role":      os.getenv("SNOWFLAKE_ROLE", "DEVELOPER"),
    }
    key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
    if key_path:
        params["private_key"] = _load_private_key_bytes(
            key_path, os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        )
    else:
        params["password"] = os.environ["SNOWFLAKE_PASSWORD"]

    return Session.builder.configs(params).create()


def get_secret(key: str, default: str = "") -> str:
    """Read a secret from st.secrets (works in both local dev and SiS).

    Resolution order:
      1. st.secrets[key]  — reads secrets.toml locally, Snowflake-managed secrets in SiS
      2. os.getenv(key)   — fallback for .env-only local workflows
    """
    try:
        return st.secrets[key]
    except (KeyError, FileNotFoundError):
        pass
    return os.getenv(key, default)


def run_query(sql: str) -> list[dict]:
    """Execute SQL, return list of dicts with uppercase column names (matches old DictCursor output)."""
    session = get_session()
    df = session.sql(sql).to_pandas()
    # Ensure column names are uppercase to match old DictCursor behaviour
    df.columns = [c.upper() for c in df.columns]
    return df.to_dict("records")
