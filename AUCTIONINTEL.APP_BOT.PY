#!/usr/bin/env python3
"""
Local script to call the Auction-Event-Finder bot on Poe via the fastapi_poe API.

Sends ONE domain at a time with a 3-second pause between each to avoid
hammering the API. Prints a running scoreboard after every domain.

Requirements:
  pip install fastapi-poe python-dotenv

Setup:
  1. Create a .env file with:  POE_API_KEY=your_api_key_here
     (Get your key from https://poe.com/api_key)
  2. Put your domain list in event_domains_now.txt (one domain per line)
  3. Run:  python run_auction_finder.py
"""

import os
import sys
import json
import csv
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import fastapi_poe as fp

# ── Configuration ──────────────────────────────────────────────────────
load_dotenv()  # Reads .env in the same directory

API_KEY = os.getenv("POE_API_KEY")
BOT_NAME = "auctionintel.app"  # ← Your published Poe script bot name
DOMAINS_FILE = "blake_domains_3.txt"
OUTPUT_DIR = "output"

# Pause between each domain (seconds) — keeps us from hammering the API
PAUSE_BETWEEN_DOMAINS = 2

# Retry settings
MAX_RETRIES = 1
RETRY_DELAY_SECONDS = 15
# ───────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "nonprofit_name", "event_title", "event_type", "evidence_date",
    "auction_type", "event_date", "event_time", "venue_name", "venue_address",
    "event_url", "confidence_score", "evidence_auction",
    "contact_name", "contact_email", "contact_role", "contact_phone",
    "organization_address", "organization_phone_maps",
    "contact_source_url", "event_summary", "status", "_processed_at",
]


def load_domains(filepath: str) -> list[str]:
    """Load domain list from a text file, one per line."""
    path = Path(filepath)
    if not path.exists():
        print(f"❌  Domain file not found: {filepath}")
        print(f"    Create '{filepath}' with one nonprofit domain per line, e.g.:")
        print(f"      ucp-li.org")
        print(f"      mercyhousingct.org")
        sys.exit(1)

    raw = path.read_text(encoding="utf-8")
    domains = [line.strip() for line in raw.splitlines() if line.strip()]

    if not domains:
        print(f"❌  '{filepath}' is empty. Add at least one domain.")
        sys.exit(1)

    return domains


def call_bot_single(domain: str) -> str:
    """Send a single domain to the bot and return the full streamed response."""
    message = fp.ProtocolMessage(role="user", content=domain)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            full_response = ""
            chunk_count = 0

            for partial in fp.get_bot_response_sync(
                messages=[message],
                bot_name=BOT_NAME,
                api_key=API_KEY,
            ):
                full_response += partial.text
                chunk_count += 1
                if chunk_count % 20 == 0:
                    print(".", end="", flush=True)

            if chunk_count >= 20:
                print()  # newline after dots
            return full_response

        except Exception as e:
            error_msg = str(e)
            print(f"\n    ⚠️  Attempt {attempt}/{MAX_RETRIES} failed: {error_msg[:120]}")
            if attempt < MAX_RETRIES:
                print(f"    ⏳  Retrying in {RETRY_DELAY_SECONDS}s …")
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"    ❌  All {MAX_RETRIES} attempts failed.")
                return ""


def extract_json_from_response(response_text: str) -> list[dict]:
    """Try to parse JSON event records from the bot response text."""
    if not response_text:
        return []

    # Strategy 1: ```json code block containing an array
    match = re.search(r"```json\s*(\[.*?\])\s*```", response_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2: Bare JSON array
    match = re.search(r"\[.*\]", response_text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # Strategy 3: Individual JSON objects (one per event)
    objects = re.findall(r"\{[^{}]*\}", response_text)
    results = []
    for obj_str in objects:
        try:
            obj = json.loads(obj_str)
            if "nonprofit_name" in obj or "event_title" in obj:
                results.append(obj)
        except json.JSONDecodeError:
            continue
    return results


def print_scoreboard(idx, total, domain, status, events_this,
                     all_results, failed_domains, found_domains, start_time):
    """Print a running scoreboard after each domain."""
    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)

    # Estimate remaining time
    avg_per_domain = elapsed / idx if idx > 0 else 0
    remaining = avg_per_domain * (total - idx)
    rem_mins, rem_secs = divmod(int(remaining), 60)

    pct = (idx / total) * 100

    print()
    print(f"┌──────────────────────────────────────────────────────────┐")
    print(f"│  RUNNING TOTAL                          {idx:>3}/{total:<3}  ({pct:5.1f}%)  │")
    print(f"├──────────────────────────────────────────────────────────┤")
    print(f"│  Last domain:  {domain:<40} │")
    print(f"│  Status:       {status:<40} │")
    print(f"│                                                          │")
    print(f"│  🏆  Events found:     {len(all_results):<34} │")
    print(f"│  ✅  Domains w/ hits:  {len(found_domains):<34} │")
    print(f"│  ❌  Failed domains:   {len(failed_domains):<34} │")
    print(f"│  ⏭️   No events:       {idx - len(found_domains) - len(failed_domains):<34} │")
    print(f"│                                                          │")
    print(f"│  ⏱️   Elapsed:  {mins:>2}m {secs:02d}s    Est. remaining:  {rem_mins:>2}m {rem_secs:02d}s   │")
    print(f"└──────────────────────────────────────────────────────────┘")

    # Show found events so far
    if all_results:
        print(f"\n  📋 Events found so far:")
        for i, r in enumerate(all_results, 1):
            auction_label = (r.get("auction_type") or "TBD").capitalize()
            print(f"     {i}. {r.get('nonprofit_name', '?')} — "
                  f"{r.get('event_title', '?')} — "
                  f"{r.get('event_date', '?')} — "
                  f"Auction: {auction_label}")
    print()


def save_results(all_results: list[dict], all_raw_responses: list[str]):
    """Save merged results to JSON, CSV, and raw text files."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Save all raw responses
    raw_path = os.path.join(OUTPUT_DIR, f"raw_responses_{timestamp}.txt")
    separator = "\n\n" + "=" * 80 + "\n\n"
    Path(raw_path).write_text(separator.join(all_raw_responses), encoding="utf-8")
    print(f"💾  Raw responses saved  → {raw_path}")

    if not all_results:
        print(
            "\n⚠️   No structured event data could be extracted from the responses.\n"
            "    Check the raw response file above for any results.\n"
            "    💡  Tip: You can also chat with the bot directly on Poe to\n"
            f"        download CSV/JSON attachments from @{BOT_NAME}."
        )
        return

    # Deduplicate by (nonprofit_name, event_title)
    seen = set()
    unique_results = []
    for r in all_results:
        key = (r.get("nonprofit_name", ""), r.get("event_title", ""))
        if key not in seen:
            seen.add(key)
            unique_results.append(r)

    # Save JSON
    json_path = os.path.join(OUTPUT_DIR, f"auction_events_{timestamp}.json")
    Path(json_path).write_text(
        json.dumps(unique_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"📄  JSON results saved  → {json_path}  ({len(unique_results)} events)")

    # Save CSV
    csv_path = os.path.join(OUTPUT_DIR, f"auction_events_{timestamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in unique_results:
            writer.writerow(row)
    print(f"📊  CSV results saved   → {csv_path}  ({len(unique_results)} rows)")


def main():
    if not API_KEY:
        print("❌  POE_API_KEY not found!")
        print("    Create a .env file in this directory with:")
        print("      POE_API_KEY=your_api_key_here")
        print("    Get your key at: https://poe.com/api_key")
        sys.exit(1)

    domains = load_domains(DOMAINS_FILE)
    total = len(domains)

    print(f"✅  Loaded {total:,} domain(s) from {DOMAINS_FILE}")
    print(f"🤖  Bot: @{BOT_NAME}")
    print(f"📂  Output: {OUTPUT_DIR}/")
    print(f"⏸️   Pause between domains: {PAUSE_BETWEEN_DOMAINS}s")
    print(f"🔄  Retries per domain: {MAX_RETRIES}")
    print(f"\n{'='*60}")
    print(f"  Starting 1-by-1 processing of {total} domains …")
    print(f"{'='*60}")

    start = time.time()
    all_results = []
    all_raw_responses = []
    failed_domains = []
    found_domains = []

    for idx, domain in enumerate(domains, start=1):
        print(f"\n🔎  [{idx}/{total}]  {domain}")

        response_text = call_bot_single(domain)

        if response_text:
            all_raw_responses.append(
                f"--- [{idx}/{total}] {domain} ---\n\n{response_text}"
            )
            events = extract_json_from_response(response_text)
            all_results.extend(events)

            if events:
                status = f"🏆 FOUND {len(events)} event(s)!"
                found_domains.append(domain)
            else:
                status = "⏭️  No auction events found"
        else:
            failed_domains.append(domain)
            all_raw_responses.append(f"--- [{idx}/{total}] {domain} --- FAILED ---")
            events = []
            status = "❌ FAILED (all retries exhausted)"

        # ── Running scoreboard ──
        print_scoreboard(
            idx, total, domain, status, len(events),
            all_results, failed_domains, found_domains, start
        )

        # ── 3-second pause before next domain ──
        if idx < total:
            print(f"    ⏸️  Pausing {PAUSE_BETWEEN_DOMAINS}s before next domain …", end="", flush=True)
            time.sleep(PAUSE_BETWEEN_DOMAINS)
            print(" go!")

    # ── Final save ──
    print(f"\n{'='*60}")
    print(f"  ALL {total} DOMAINS PROCESSED — SAVING RESULTS")
    print(f"{'='*60}\n")

    save_results(all_results, all_raw_responses)

    elapsed = time.time() - start
    minutes, seconds = divmod(int(elapsed), 60)

    print(f"\n{'='*60}")
    print(f"  ✅  FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"  Total domains:       {total}")
    print(f"  Events found:        {len(all_results)}")
    print(f"  Domains w/ hits:     {len(found_domains)}")
    print(f"  Domains w/ nothing:  {total - len(found_domains) - len(failed_domains)}")
    print(f"  Failed domains:      {len(failed_domains)}")
    if failed_domains:
        for fd in failed_domains:
            print(f"     ❌  {fd}")
    print(f"  Total time:          {minutes}m {seconds}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
