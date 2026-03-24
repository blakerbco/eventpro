#!/usr/bin/env python3
"""
Diagnostic script to check which database the app is connected to
"""

import os
import psycopg2

print("=== DATABASE CONNECTION CHECK ===\n")

# Check environment variables
irs_db = os.environ.get("IRS_DB_CONNECTION")
db_url = os.environ.get("DATABASE_URL")
db_private = os.environ.get("DATABASE_PRIVATE_URL")

print("Environment variables:")
print(f"  IRS_DB_CONNECTION: {irs_db[:50] if irs_db else 'NOT SET'}...")
print(f"  DATABASE_URL: {db_url[:50] if db_url else 'NOT SET'}...")
print(f"  DATABASE_PRIVATE_URL: {db_private[:50] if db_private else 'NOT SET'}...\n")

# Determine which one will be used (same logic as db.py)
db_conn = irs_db or db_url or db_private or "postgresql://localhost/irs"

if irs_db:
    source = "IRS_DB_CONNECTION"
elif db_url:
    source = "DATABASE_URL"
elif db_private:
    source = "DATABASE_PRIVATE_URL"
else:
    source = "FALLBACK"

print(f"Will use: {source}")
print(f"Connection string: {db_conn[:80]}...\n")

# Check if it's the new or old database
if "ballast.proxy.rlwy.net:30705" in db_conn:
    print("✓ CONNECTED TO NEW DATABASE (ballast)")
elif "crossover.proxy.rlwy.net:23768" in db_conn:
    print("✗ CONNECTED TO OLD DATABASE (crossover) - THIS IS WRONG!")
else:
    print("? CONNECTED TO UNKNOWN DATABASE")

print("\n=== CHECKING DATA ===\n")

try:
    conn = psycopg2.connect(db_conn)
    cur = conn.cursor()

    # Check job_results count
    cur.execute("SELECT COUNT(*) FROM job_results")
    job_count = cur.fetchone()[0]
    print(f"job_results records: {job_count}")

    # Check search_jobs count
    cur.execute("SELECT COUNT(*) FROM search_jobs")
    search_count = cur.fetchone()[0]
    print(f"search_jobs records: {search_count}")

    # Check recent jobs
    cur.execute("""
        SELECT job_id, COUNT(*) as domains
        FROM job_results
        GROUP BY job_id
        ORDER BY MIN(created_at) DESC
        LIMIT 3
    """)
    print("\nRecent jobs:")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]} domains")

    cur.close()
    conn.close()

    print("\n✓ Database connection successful!")

    if job_count > 8000 and search_count > 20:
        print("✓ Data looks good - this is the NEW database")
    elif job_count < 100:
        print("✗ Almost no data - this might be the OLD database")

except Exception as e:
    print(f"\n✗ Database connection FAILED: {e}")
