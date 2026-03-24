#!/usr/bin/env python3
"""
Download the 6 batches from last night
"""

import csv
import json
import sys
import psycopg2

# Import the REAL classify_lead_tier from bot.py
from bot import classify_lead_tier

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DB_URL = "postgresql://postgres:wwGJdmaVFcygQAunmoTNgvohjGRrzuKl@ballast.proxy.rlwy.net:30705/railway"

# The 6 job IDs from last night
JOB_IDS = [
    "job_20260323_013956_c8589393",
    "job_20260323_013956_6a367716",
    "job_20260323_013956_40e39ced",
    "job_20260323_013956_124eb9cf",
    "job_20260323_013956_a7f6fb2a",
    "job_20260323_013956_6d05aa77",
]

# Using the real classify_lead_tier from bot.py

print("Connecting to database...")
conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

for job_id in JOB_IDS:
    print(f"\nProcessing {job_id}...")

    # Get all results for this job
    cur.execute("""
        SELECT domain, result_json
        FROM job_results
        WHERE job_id = %s AND result_json IS NOT NULL
        ORDER BY created_at
    """, (job_id,))

    rows = cur.fetchall()
    print(f"  Found {len(rows)} results")

    # Parse and classify
    results = []
    dm_count = 0
    or_count = 0
    ev_count = 0
    not_billable_count = 0

    for domain, result_json in rows:
        try:
            result = json.loads(result_json) if isinstance(result_json, str) else result_json

            # Use the REAL classify_lead_tier function
            tier, price_cents = classify_lead_tier(result)

            if tier == "decision_maker":
                dm_count += 1
            elif tier == "outreach_ready":
                or_count += 1
            elif tier == "event_verified":
                ev_count += 1
            else:
                not_billable_count += 1

            # Add tier to result
            result['tier'] = tier
            result['tier_price'] = price_cents
            results.append(result)
        except Exception as e:
            print(f"    Error parsing {domain}: {e}")
            import traceback
            traceback.print_exc()
            continue

    print(f"  Decision Makers: {dm_count}, Outreach Ready: {or_count}, Event Verified: {ev_count}, Not Billable: {not_billable_count}")

    # Write CSV with ALL 18 fields
    csv_file = f"{job_id}.csv"
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'nonprofit_name', 'query_domain', 'event_title', 'event_type',
            'evidence_date', 'auction_type', 'event_date', 'event_url',
            'confidence_score', 'evidence_auction', 'contact_name', 'contact_email',
            'email_status', 'contact_role', 'organization_address', 'organization_phone_maps',
            'contact_source_url', 'event_summary', 'tier', 'tier_price', 'status'
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                'nonprofit_name': r.get('nonprofit_name', ''),
                'query_domain': r.get('query_domain', ''),
                'event_title': r.get('event_title', ''),
                'event_type': r.get('event_type', ''),
                'evidence_date': r.get('evidence_date', ''),
                'auction_type': r.get('auction_type', ''),
                'event_date': r.get('event_date', ''),
                'event_url': r.get('event_url', ''),
                'confidence_score': r.get('confidence_score', ''),
                'evidence_auction': r.get('evidence_auction', ''),
                'contact_name': r.get('contact_name', ''),
                'contact_email': r.get('contact_email', ''),
                'email_status': r.get('email_status', ''),
                'contact_role': r.get('contact_role', ''),
                'organization_address': r.get('organization_address', ''),
                'organization_phone_maps': r.get('organization_phone_maps', ''),
                'contact_source_url': r.get('contact_source_url', ''),
                'event_summary': r.get('event_summary', ''),
                'tier': r.get('tier', ''),
                'tier_price': r.get('tier_price', 0),
                'status': r.get('status', ''),
            })

    # Write JSON
    json_file = f"{job_id}.json"
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"  Wrote {csv_file} and {json_file}")

cur.close()
conn.close()

print("\n✓ All 6 batches downloaded!")
print("\nDecision maker counts:")
print("  (Run this script to see the counts for each batch)")
