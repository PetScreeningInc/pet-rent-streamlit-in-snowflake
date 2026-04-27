#!/usr/bin/env python3
"""
Quick test: connect to Snowflake using keypair auth (no Duo, no password).
Run BEFORE patching anything in batch_pdf.py / app.py / fetch_charges.py.

Usage:
    python test_snowflake_keypair.py
"""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed; reading os.environ only")

try:
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    print("   Run: pip install snowflake-connector-python cryptography python-dotenv")
    sys.exit(1)


def load_private_key(key_path: str, passphrase: str | None) -> bytes:
    p = Path(key_path).expanduser()
    if not p.exists():
        sys.exit(f"❌ Private key not found: {p}")

    pw_bytes = passphrase.encode() if passphrase else None
    with open(p, "rb") as f:
        try:
            pkey = serialization.load_pem_private_key(
                f.read(),
                password=pw_bytes,
                backend=default_backend(),
            )
        except Exception as e:
            sys.exit(
                f"❌ Failed to decrypt private key.\n"
                f"   Likely wrong passphrase. Underlying error: {e}"
            )

    return pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def main():
    account     = os.getenv("SNOWFLAKE_ACCOUNT")
    user        = os.getenv("SNOWFLAKE_USER")
    warehouse   = os.getenv("SNOWFLAKE_WAREHOUSE")
    role        = os.getenv("SNOWFLAKE_ROLE")
    key_path    = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH")
    passphrase  = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")

    print("=" * 60)
    print("Snowflake Keypair Auth Test")
    print("=" * 60)
    print(f"Account:    {account}")
    print(f"User:       {user}")
    print(f"Warehouse:  {warehouse}")
    print(f"Role:       {role}")
    print(f"Key path:   {key_path}")
    print(f"Passphrase: {'(set)' if passphrase else '(NONE)'}")
    print()

    missing = [k for k, v in {
        "SNOWFLAKE_ACCOUNT": account,
        "SNOWFLAKE_USER": user,
        "SNOWFLAKE_PRIVATE_KEY_PATH": key_path,
    }.items() if not v]
    if missing:
        sys.exit(f"❌ Missing env vars: {', '.join(missing)}")

    print("→ Loading and decrypting private key...")
    pkb = load_private_key(key_path, passphrase)
    print("✓ Key decrypted OK\n")

    print("→ Connecting to Snowflake (no Duo, no password)...")
    try:
        conn = snowflake.connector.connect(
            account=account,
            user=user,
            private_key=pkb,
            warehouse=warehouse,
            role=role,
        )
    except Exception as e:
        msg = str(e)
        print(f"❌ Connection failed: {e}\n")
        if "JWT token is invalid" in msg or "390144" in msg or "390101" in msg:
            print("   → The public key is probably NOT registered against your")
            print("     Snowflake user. Ask Gucci to run:")
            print()
            print("     ALTER USER EDUARDOSALVADOR SET RSA_PUBLIC_KEY='<one-line key>';")
        sys.exit(1)

    print("✓ Connected!\n")

    print("→ Running SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()...")
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_TIMESTAMP()")
    row = cur.fetchone()
    print(f"   user:      {row[0]}")
    print(f"   role:      {row[1]}")
    print(f"   warehouse: {row[2]}")
    print(f"   time:      {row[3]}")
    cur.close()
    conn.close()
    print("\n✅ All good. Keypair auth works. You can now patch batch_pdf.py.")


if __name__ == "__main__":
    main()
