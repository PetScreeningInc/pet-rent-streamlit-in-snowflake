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
        SNOWFLAKE_PRIVATE_KEY             PEM private key content (deployment-friendly)
        SNOWFLAKE_PRIVATE_KEY_B64         base64-encoded PEM private key content
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE  optional, only if key is encrypted

    Password auth (fallback):
        SNOWFLAKE_PASSWORD
"""
import base64
import os
from pathlib import Path
from typing import Any, Optional

import snowflake.connector

# Load .env from the file's own directory if present, so this helper
# works whether or not the caller already loaded dotenv.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _load_private_key_bytes(key_path: str, passphrase: Optional[str]) -> bytes:
    """Load and decrypt a PKCS8 private key file, return raw DER bytes."""
    p = Path(key_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Snowflake private key not found: {p}")

    with open(p, "rb") as f:
        return _load_private_key_pem_bytes(f.read(), passphrase)


def _load_private_key_pem_bytes(key_bytes: bytes, passphrase: Optional[str]) -> bytes:
    """Load and decrypt PEM private key bytes, return raw DER bytes."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    pw_bytes = passphrase.encode() if passphrase else None
    pkey = serialization.load_pem_private_key(
        key_bytes,
        password=pw_bytes,
        backend=default_backend(),
    )

    return pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _NonClosingConnection:
    """Proxy around the Streamlit-in-Snowflake active session's connection.

    The app opens/closes connections liberally (each helper does
    get_snowflake_connection() ... conn.close()). Inside Snowflake there is
    exactly ONE session-owned connection, and closing it would kill the
    whole app session — so close() must be a no-op here.
    """

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _sis_connection():
    """Return the active session's connection when running inside
    Streamlit-in-Snowflake, else None (local dev)."""
    try:
        from snowflake.snowpark.context import get_active_session
        session = get_active_session()
        return _NonClosingConnection(session.connection)
    except Exception:
        return None


def get_app_secret(name: str, default: str = "") -> str:
    """API credential lookup that works in both runtimes.

    In Streamlit-in-Snowflake, secrets are attached to the STREAMLIT object
    (ALTER STREAMLIT ... SET SECRETS (...)) and read via the _snowflake
    module. Locally they come from the environment / .env.
    """
    try:
        import _snowflake  # only exists inside Snowflake
        val = _snowflake.get_generic_secret_string(name)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(name, default)


def get_snowflake_connection(database: Optional[str] = None):
    """
    Open a Snowflake connection.

    Inside Streamlit-in-Snowflake: reuses the active session's connection
    (no credentials needed; close() is a no-op).
    Locally: prefers keypair auth if SNOWFLAKE_PRIVATE_KEY_PATH is set;
    otherwise falls back to password.
    """
    sis_conn = _sis_connection()
    if sis_conn is not None:
        return sis_conn

    account   = os.getenv("SNOWFLAKE_ACCOUNT")
    user      = os.getenv("SNOWFLAKE_USER")
    warehouse = os.getenv("SNOWFLAKE_WAREHOUSE")
    role      = os.getenv("SNOWFLAKE_ROLE")
    db        = database or os.getenv("SNOWFLAKE_DATABASE")

    key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
    key_pem = os.getenv("SNOWFLAKE_PRIVATE_KEY")
    key_pem_b64 = os.getenv("SNOWFLAKE_PRIVATE_KEY_B64")

    kwargs: dict[str, Any] = {
        "account":   account,
        "user":      user,
        "warehouse": warehouse,
        "role":      role,
    }
    if db:
        kwargs["database"] = db

    if key_pem_b64:
        passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        kwargs["private_key"] = _load_private_key_pem_bytes(
            base64.b64decode(key_pem_b64),
            passphrase,
        )
    elif key_pem:
        passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        kwargs["private_key"] = _load_private_key_pem_bytes(key_pem.encode(), passphrase)
    elif key_path:
        passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        kwargs["private_key"] = _load_private_key_bytes(key_path, passphrase)
    else:
        password = os.getenv("SNOWFLAKE_PASSWORD")
        if not password:
            raise RuntimeError(
                "No Snowflake auth available. Set either "
                "SNOWFLAKE_PRIVATE_KEY / SNOWFLAKE_PRIVATE_KEY_B64 / "
                "SNOWFLAKE_PRIVATE_KEY_PATH (preferred) or SNOWFLAKE_PASSWORD."
            )
        kwargs["password"] = password

    return snowflake.connector.connect(**kwargs)
