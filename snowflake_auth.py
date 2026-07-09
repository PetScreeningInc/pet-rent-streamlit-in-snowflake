"""Back-compat shim — the connection/auth helpers moved to services.snowflake_io.

Batch scripts, fetch_cache, snapshot_tables, and realpage_live_api import
from here. They get the RAW connection builder (no Streamlit caching), the
same behavior this module always had.
"""
from services.snowflake_io import (  # noqa: F401
    build_snowflake_connection as get_snowflake_connection,
    get_app_secret,
    _load_private_key_bytes,
    _load_private_key_pem_bytes,
    _NonClosingConnection,
    _sis_connection,
)
