"""
Shared Snowflake connection helper.

Supports BOTH keypair auth (preferred — no Duo, no password expiry)
and password auth (legacy fallback). Picks based on env vars present.

Env vars:
    SNOWFLAKE_ACCOUNT              required
    SNOWFLAKE_USER                 required
    SNOWFLAKE_WAREHOUSE            required
    SNOWFLAKE_ROLE                 required
    SNOWFLAKE_DATABASE             optional

    Keypair auth (preferred):
        SNOWFLAKE_PRIVATE_KEY_PATH        path to .p8 file
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE  optional, only if key is encrypted

    Password auth (fallback):
        SNOWFLAKE_PASSWORD
"""
import os
from pathlib import Path
from typing import Optional

import snowflake.connector

# Load .env from the file's own directory if present, so this helper
# works whether or not the caller already loaded dotenv.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _load_private_key_bytes(key_path: str, passphrase: Optional[str]) -> bytes:
    """Load and decrypt a PKCS8 private key, return raw DER bytes."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    p = Path(key_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Snowflake private key not found: {p}")

    pw_bytes = passphrase.encode() if passphrase else None
    with open(p, "rb") as f:
        pkey = serialization.load_pem_private_key(
            f.read(),
            password=pw_bytes,
            backend=default_backend(),
        )

    return pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def get_snowflake_connection(database: Optional[str] = None):
    """
    Open a Snowflake connection. Prefers keypair auth if
    SNOWFLAKE_PRIVATE_KEY_PATH is set; otherwise falls back to password.
    """
    account   = os.getenv("SNOWFLAKE_ACCOUNT")
    user      = os.getenv("SNOWFLAKE_USER")
    warehouse = os.getenv("SNOWFLAKE_WAREHOUSE")
    role      = os.getenv("SNOWFLAKE_ROLE")
    db        = database or os.getenv("SNOWFLAKE_DATABASE")

    key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")

    kwargs = {
        "account":   account,
        "user":      user,
        "warehouse": warehouse,
        "role":      role,
    }
    if db:
        kwargs["database"] = db

    if key_path:
        passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        kwargs["private_key"] = _load_private_key_bytes(key_path, passphrase)
    else:
        password = os.getenv("SNOWFLAKE_PASSWORD")
        if not password:
            raise RuntimeError(
                "No Snowflake auth available. Set either "
                "SNOWFLAKE_PRIVATE_KEY_PATH (preferred) or SNOWFLAKE_PASSWORD."
            )
        kwargs["password"] = password

    return snowflake.connector.connect(**kwargs)
