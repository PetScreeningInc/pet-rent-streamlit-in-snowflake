"""
Upload large CSV to Snowflake via PUT + COPY INTO.
Handles files too big for the web UI upload.

Usage:
    python upload_charges_to_snowflake.py /path/to/file.csv

Reads Snowflake creds from .env in the same directory as this script.
"""

import os
import sys
import snowflake.connector
from dotenv import load_dotenv
from pathlib import Path

# ── config ──────────────────────────────────────────────────────────
TABLE_NAME   = "ENTRATA_CHARGES_HQ_ASSET_LIVING_2026_03_19"
SCHEMA       = "RAW.MISC"
STAGE_NAME   = f"@{SCHEMA}.%{TABLE_NAME}"

# ── load creds ──────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

def get_connection():
    # Prefers keypair auth (SNOWFLAKE_PRIVATE_KEY_PATH); falls back to password.
    from snowflake_auth import get_snowflake_connection as _build
    return _build(database=os.getenv("SNOWFLAKE_DATABASE", "RAW"))

def main():
    if len(sys.argv) < 2:
        print("Usage: python upload_charges_to_snowflake.py <csv_file_path>")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        sys.exit(1)

    file_size_mb = os.path.getsize(csv_path) / (1024 * 1024)
    print(f"File: {csv_path} ({file_size_mb:.0f} MB)")

    conn = get_connection()
    cur = conn.cursor()

    try:
        # 1. create the table
        print(f"\n1. Creating table {SCHEMA}.{TABLE_NAME}...")
        cur.execute(f"""
            CREATE OR REPLACE TABLE {SCHEMA}.{TABLE_NAME} (
                parent_company          VARCHAR,
                property_id             VARCHAR,
                property_name           VARCHAR,
                property_code           VARCHAR,
                launch_date             VARCHAR,
                unit_code               VARCHAR,
                unit_type               VARCHAR,
                market_rent             VARCHAR,
                lease_id                VARCHAR,
                tenant_code             VARCHAR,
                first_name              VARCHAR,
                last_name               VARCHAR,
                tenant_status           VARCHAR,
                move_in                 VARCHAR,
                move_out                VARCHAR,
                email                   VARCHAR,
                lease_from              VARCHAR,
                lease_to                VARCHAR,
                charge_code             VARCHAR,
                charge_type             VARCHAR,
                charge_amount           VARCHAR,
                charge_from_date        VARCHAR,
                charge_to_date          VARCHAR,
                frequency               VARCHAR,
                lease_interval_id       VARCHAR,
                _entrata_charge_dedup_key VARCHAR
            )
        """)
        print("   ✅ Table created")

        # 2. PUT file to internal stage (auto-compresses + splits)
        print(f"\n2. Staging file (this may take a few minutes for {file_size_mb:.0f} MB)...")
        put_sql = f"PUT 'file://{os.path.abspath(csv_path)}' {STAGE_NAME} AUTO_COMPRESS=TRUE OVERWRITE=TRUE"
        cur.execute(put_sql)
        result = cur.fetchone()
        print(f"   ✅ Staged: {result}")

        # 3. COPY INTO from stage
        print(f"\n3. Loading into {SCHEMA}.{TABLE_NAME}...")
        cur.execute(f"""
            COPY INTO {SCHEMA}.{TABLE_NAME}
            FROM {STAGE_NAME}
            FILE_FORMAT = (
                TYPE = 'CSV'
                FIELD_OPTIONALLY_ENCLOSED_BY = '"'
                SKIP_HEADER = 1
                FIELD_DELIMITER = ','
                NULL_IF = ('', 'NULL', 'null')
                EMPTY_FIELD_AS_NULL = TRUE
                ERROR_ON_COLUMN_COUNT_MISMATCH = FALSE
            )
            ON_ERROR = 'CONTINUE'
            PURGE = TRUE
        """)
        load_result = cur.fetchall()
        for row in load_result:
            print(f"   File: {row[0]}")
            print(f"   Status: {row[1]}")
            print(f"   Rows parsed: {row[2]}")
            print(f"   Rows loaded: {row[3]}")
            if row[4]:  # errors_seen
                print(f"   ⚠️  Errors: {row[4]}")
            if row[5]:  # first_error
                print(f"   First error: {row[5]}")

        # 4. verify
        print(f"\n4. Verifying...")
        cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE_NAME}")
        count = cur.fetchone()[0]
        print(f"   ✅ {count:,} rows loaded")

        cur.execute(f"SELECT * FROM {SCHEMA}.{TABLE_NAME} LIMIT 3")
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description]
        print(f"   Columns: {cols}")
        for r in rows:
            print(f"   Sample: {r[:5]}...")

        print(f"\n🎉 Done. Table ready at {SCHEMA}.{TABLE_NAME}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
