"""
Seed the confirmed_auction_nonprofits table from seed_db_26.csv.

Reads ~26K confirmed auction domains, matches them against the IRS
tax_year_2019_search Website column, and copies matching rows into
a new confirmed_auction_nonprofits table with identical schema.

Usage:
  python seed_confirmed_auctions.py                                          # uses env vars
  python seed_confirmed_auctions.py postgresql://user:pass@host:port/dbname  # explicit
"""

import csv
import os
import re
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

SEED_CSV = "seed_db_26.csv"


def normalize_domain(raw: str) -> str:
    """Normalize a URL/domain to bare domain for matching.
    e.g. 'http://www.100blackmen.com/' -> '100blackmen.com'
    """
    d = raw.strip().lower()
    d = re.sub(r'^https?://', '', d)
    d = re.sub(r'^www\.', '', d)
    d = d.rstrip('/')
    # Remove spaces (e.g. "Zooboise. Org" -> "zooboise.org")
    d = d.replace(' ', '')
    return d


def main():
    # Connection string
    if len(sys.argv) > 1:
        conn_string = sys.argv[1]
    else:
        conn_string = (
            os.environ.get("IRS_DB_CONNECTION")
            or os.environ.get("DATABASE_URL")
            or os.environ.get("DATABASE_PRIVATE_URL")
            or "postgresql://localhost/irs"
        )

    # 1. Read seed domains from non_profit column
    print(f"Reading seed domains from {SEED_CSV}...")
    seed_domains = set()
    with open(SEED_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("non_profit", "").strip()
            if raw:
                d = normalize_domain(raw)
                if d and '.' in d:
                    seed_domains.add(d)
    seed_domains.discard("")
    print(f"  Loaded {len(seed_domains):,} unique seed domains")

    # 2. Connect
    print(f"Connecting to: {conn_string[:40]}...")
    conn = psycopg2.connect(conn_string)
    cur = conn.cursor()

    # 3. Load seed domains into a temp table for bulk matching
    print("Loading seed domains into temp table...")
    cur.execute("CREATE TEMP TABLE seed_domains (domain TEXT PRIMARY KEY)")
    batch = []
    for d in seed_domains:
        batch.append((d,))
        if len(batch) >= 5000:
            cur.executemany("INSERT INTO seed_domains (domain) VALUES (%s) ON CONFLICT DO NOTHING", batch)
            batch = []
    if batch:
        cur.executemany("INSERT INTO seed_domains (domain) VALUES (%s) ON CONFLICT DO NOTHING", batch)
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM seed_domains")
    print(f"  {cur.fetchone()[0]:,} domains in temp table")

    # 4. Create confirmed_auction_nonprofits table (same schema as tax_year_2019_search)
    print("Creating confirmed_auction_nonprofits table...")
    cur.execute("DROP TABLE IF EXISTS confirmed_auction_nonprofits")
    cur.execute("""
        CREATE TABLE confirmed_auction_nonprofits (
            EIN TEXT,
            OrganizationName TEXT,
            Website TEXT,
            PhysicalAddress TEXT,
            PhysicalCity TEXT,
            PhysicalState TEXT,
            PhysicalZIP TEXT,
            BusinessOfficerPhone TEXT,
            PrincipalOfficerName TEXT,
            TotalRevenue BIGINT,
            GrossReceipts BIGINT,
            NetIncome BIGINT,
            TotalAssets BIGINT,
            ContributionsReceived BIGINT,
            ProgramServiceRevenue BIGINT,
            FundraisingGrossIncome BIGINT,
            FundraisingDirectExpenses BIGINT,
            Event1Name TEXT,
            Event1GrossReceipts BIGINT,
            Event1GrossRevenue BIGINT,
            Event1NetIncome BIGINT,
            Event2Name TEXT,
            Event2GrossReceipts BIGINT,
            Event2GrossRevenue BIGINT,
            Event1Keyword TEXT,
            Event2Keyword TEXT,
            PrimaryEventType TEXT,
            ProspectTier TEXT,
            ProspectScore NUMERIC,
            Region5 TEXT,
            MissionDescriptionShort TEXT,
            HasGala INTEGER,
            HasAuction INTEGER,
            HasRaffle INTEGER,
            HasBall INTEGER,
            HasDinner INTEGER,
            HasBenefit INTEGER,
            HasTournament INTEGER,
            HasGolf INTEGER,
            HasFundraisingActivities INTEGER
        )
    """)
    conn.commit()

    # 5. Match: normalize IRS Website and join against seed domains
    print("Matching IRS records against seed domains...")
    cur.execute("""
        INSERT INTO confirmed_auction_nonprofits
        SELECT t.*
        FROM tax_year_2019_search t
        JOIN seed_domains s
          ON s.domain = REPLACE(TRIM(TRAILING '/' FROM
                         REGEXP_REPLACE(
                           REGEXP_REPLACE(LOWER(TRIM(t.Website)), '^https?://', ''),
                           '^www\\.', ''
                         )), ' ', '')
        WHERE t.Website IS NOT NULL AND t.Website != ''
    """)
    matched = cur.rowcount
    conn.commit()
    print(f"  Matched {matched:,} IRS rows into confirmed_auction_nonprofits")

    # 6. Create indexes for fast searches
    print("Creating indexes...")
    cur.execute("CREATE INDEX idx_confirmed_state ON confirmed_auction_nonprofits (PhysicalState);")
    cur.execute("CREATE INDEX idx_confirmed_revenue ON confirmed_auction_nonprofits (TotalRevenue DESC);")
    cur.execute("CREATE INDEX idx_confirmed_orgname ON confirmed_auction_nonprofits (OrganizationName);")
    cur.execute("CREATE INDEX idx_confirmed_region ON confirmed_auction_nonprofits (Region5);")
    cur.execute("CREATE INDEX idx_confirmed_event_type ON confirmed_auction_nonprofits (PrimaryEventType);")
    cur.execute("CREATE INDEX idx_confirmed_prospect ON confirmed_auction_nonprofits (ProspectTier);")
    cur.execute("CREATE INDEX idx_confirmed_website ON confirmed_auction_nonprofits (Website);")
    conn.commit()
    print("Indexes created.")

    # 7. Stats
    cur.execute("SELECT COUNT(*) FROM confirmed_auction_nonprofits")
    total_confirmed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tax_year_2019_search")
    total_irs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT PhysicalState) FROM confirmed_auction_nonprofits")
    states = cur.fetchone()[0]

    # Show match rate breakdown
    unmatched = len(seed_domains) - total_confirmed
    print(f"\n=== RESULTS ===")
    print(f"  Seed domains:       {len(seed_domains):,}")
    print(f"  IRS total rows:     {total_irs:,}")
    print(f"  Confirmed matches:  {total_confirmed:,}")
    print(f"  Unmatched domains:  {unmatched:,}")
    print(f"  Match rate:         {total_confirmed / len(seed_domains) * 100:.1f}% of seed domains")
    print(f"  States covered:     {states}")

    # Show top 5 states
    cur.execute("""
        SELECT PhysicalState, COUNT(*) as cnt
        FROM confirmed_auction_nonprofits
        WHERE PhysicalState IS NOT NULL
        GROUP BY PhysicalState
        ORDER BY cnt DESC
        LIMIT 5
    """)
    print(f"\n  Top 5 states:")
    for row in cur.fetchall():
        print(f"    {row[0]}: {row[1]:,}")

    print(f"\n  Done! Table 'confirmed_auction_nonprofits' is ready.")
    print(f"  The original 'tax_year_2019_search' table is untouched.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
