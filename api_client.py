#!/usr/bin/env python3
"""
Auction Intel API Client — Interactive batch domain research.

Usage:
    python api_client.py --key ak_xxx
    python api_client.py --key ak_xxx --output results.csv
    python api_client.py --key ak_xxx --file domains.txt --output results.csv
    python api_client.py --key ak_xxx --resume-job job_xxx --output results.csv

If --file is not provided, you'll be prompted to paste domains interactively.
"""

import argparse
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests")
    sys.exit(1)


DEFAULT_URL = "https://auctionintel.app"

TIER_INFO = {
    "1": {
        "key": "decision_maker",
        "name": "Decision Maker",
        "price": "$1.75/lead",
        "desc": "Named contact + verified email + event page",
    },
    "2": {
        "key": "outreach_ready",
        "name": "Outreach Ready",
        "price": "$1.25/lead",
        "desc": "Verified email + event page (no contact name)",
    },
    "3": {
        "key": "event_verified",
        "name": "Event Verified",
        "price": "$0.75/lead",
        "desc": "Verified event page only (no contact info)",
    },
}


def prompt_domains():
    """Prompt user to paste domains interactively."""
    print()
    print("=" * 60)
    print("  PASTE DOMAINS (one per line)")
    print("  When done, type 'done' on a new line and press Enter")
    print("=" * 60)
    print()

    domains = []
    while True:
        try:
            line = input().strip()
        except EOFError:
            break
        if line.lower() == "done":
            break
        if line and not line.startswith("#"):
            domains.append(line)

    if not domains:
        print("No domains entered. Exiting.")
        sys.exit(0)

    print(f"\n  {len(domains)} domain(s) entered.")
    return domains


def load_domains(filepath):
    """Load domains from a text file, one per line."""
    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        domains = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return domains


def prompt_tiers():
    """Prompt user to select which lead tiers to include."""
    print()
    print("=" * 60)
    print("  SELECT LEAD TIERS")
    print("  You only pay for leads at the tiers you select.")
    print("  Research fee: $0.04/nonprofit (charged regardless)")
    print("=" * 60)
    print()
    print("  [1] Decision Maker  — $1.75/lead")
    print("      Named contact + verified email + event page")
    print()
    print("  [2] Outreach Ready  — $1.25/lead")
    print("      Verified email + event page (no contact name)")
    print()
    print("  [3] Event Verified  — $0.75/lead")
    print("      Verified event page only (no contact info)")
    print()
    print("  [4] All tiers (1 + 2 + 3)")
    print()

    while True:
        choice = input("  Enter your choice (e.g. 1,2 or 4 for all): ").strip()
        if not choice:
            continue

        if "4" in choice:
            selected = ["decision_maker", "outreach_ready", "event_verified"]
            print("\n  Selected: All tiers")
            return selected

        selected = []
        names = []
        for c in choice.replace(" ", "").split(","):
            if c in TIER_INFO:
                tier = TIER_INFO[c]
                if tier["key"] not in selected:
                    selected.append(tier["key"])
                    names.append(f'{tier["name"]} ({tier["price"]})')

        if selected:
            print(f"\n  Selected: {', '.join(names)}")
            return selected

        print("  Invalid choice. Enter 1, 2, 3, or 4 (comma-separated for multiple).")


def confirm_job(domains, tiers):
    """Show summary and confirm before submitting."""
    print()
    print("=" * 60)
    print("  JOB SUMMARY")
    print("=" * 60)
    print(f"  Domains:       {len(domains)}")
    print(f"  Research fee:  ${len(domains) * 0.04:.2f} ({len(domains)} x $0.04)")

    tier_names = []
    for t in tiers:
        for info in TIER_INFO.values():
            if info["key"] == t:
                tier_names.append(f'{info["name"]} ({info["price"]})')
    print(f"  Lead tiers:    {', '.join(tier_names)}")

    # Estimate
    est_hits = int(len(domains) * 0.55)
    est_lead_cost = est_hits * 125  # avg $1.25 across tiers
    est_total = len(domains) * 4 + est_lead_cost
    print(f"  Est. leads:    ~{est_hits} (55% hit rate)")
    print(f"  Est. total:    ~${est_total / 100:.2f}")
    print()

    while True:
        confirm = input("  Submit this job? (y/n): ").strip().lower()
        if confirm in ("y", "yes"):
            return True
        if confirm in ("n", "no"):
            return False


def submit_job(base_url, api_key, domains, selected_tiers):
    """Submit a batch of domains for research."""
    resp = requests.post(
        f"{base_url}/api/v1/search",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={"domains": domains, "selected_tiers": selected_tiers},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: Submit failed ({resp.status_code}): {resp.text}")
        sys.exit(1)
    data = resp.json()
    print(f"\n  Job submitted: {data['job_id']} ({data['total_domains']} domains)")
    return data["job_id"]


def poll_status(base_url, api_key, job_id, interval=5):
    """Poll job status until complete, showing progress."""
    last_processed = 0
    while True:
        try:
            resp = requests.get(
                f"{base_url}/api/v1/status/{job_id}",
                headers={"X-API-Key": api_key},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"  Status check failed ({resp.status_code}): {resp.text}")
                time.sleep(interval)
                continue

            data = resp.json()
            status = data.get("status", "unknown")
            processed = data.get("processed", 0)
            total = data.get("total", 0)
            found = data.get("found", 0)
            eta = data.get("eta_seconds")

            if processed != last_processed:
                eta_str = f" | ETA: {eta//60}m{eta%60}s" if eta else ""
                pct = (processed / total * 100) if total else 0
                bar_len = 30
                filled = int(bar_len * processed / total) if total else 0
                bar = "=" * filled + "-" * (bar_len - filled)
                print(f"\r  [{bar}] {processed}/{total} ({pct:.0f}%) | Found: {found}{eta_str}    ", end="", flush=True)
                last_processed = processed

            if status in ("complete", "error"):
                print()  # newline after progress bar
                if status == "error":
                    print(f"  Job FAILED: {data.get('error', 'unknown error')}")
                    return data
                print(f"  Job COMPLETE: {processed}/{total} processed, {found} events found")
                if data.get("summary"):
                    s = data["summary"]
                    print(f"  Summary: found={s.get('found',0)}, 3rdpty={s.get('3rdpty_found',0)}, "
                          f"not_found={s.get('not_found',0)}, uncertain={s.get('uncertain',0)}")
                return data

        except requests.exceptions.RequestException as e:
            print(f"\n  Connection error: {e}. Retrying in {interval}s...")

        time.sleep(interval)


def display_results(base_url, api_key, job_id):
    """Fetch JSON results and display a rich terminal table like the app UI."""
    resp = requests.get(
        f"{base_url}/api/v1/results/{job_id}?format=json",
        headers={"X-API-Key": api_key},
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"  Could not fetch results for display: {resp.status_code}")
        return

    try:
        data = resp.json()
    except Exception:
        print("  Could not parse JSON results for display.")
        return

    results = data.get("results", [])
    if not results:
        print("\n  No leads found in this batch.")
        return

    summary = data.get("summary", {})
    meta = data.get("meta", {})

    print()
    print("  " + "=" * 76)
    print("  RESULTS")
    print("  " + "=" * 76)

    if summary:
        parts = []
        for k in ("found", "3rdpty_found", "not_found", "uncertain"):
            v = summary.get(k, 0)
            if v:
                label = k.replace("_", " ").title()
                parts.append(f"{label}: {v}")
        print(f"  {' | '.join(parts)}")
        if meta.get("processing_time_seconds"):
            print(f"  Processing time: {meta['processing_time_seconds']}s")
    print()

    for i, r in enumerate(results, 1):
        status = r.get("status", "unknown")
        conf = r.get("confidence_score", "")
        tier_label = ""
        if r.get("contact_name") and r.get("contact_email"):
            tier_label = "Decision Maker"
        elif r.get("contact_email"):
            tier_label = "Outreach Ready"
        elif r.get("event_url"):
            tier_label = "Event Verified"

        # Status color indicator
        if status in ("found", "3rdpty_found"):
            indicator = "[+]"
        elif status == "not_found":
            indicator = "[-]"
        else:
            indicator = "[?]"

        print(f"  {indicator} #{i}  {r.get('nonprofit_name', 'Unknown')}")
        print(f"  {'':>6}Status: {status}  |  Confidence: {conf}  |  Tier: {tier_label or 'N/A'}")

        if r.get("event_title"):
            print(f"  {'':>6}Event:   {r['event_title']}")
        if r.get("event_date"):
            print(f"  {'':>6}Date:    {r['event_date']}")
        if r.get("auction_type"):
            print(f"  {'':>6}Auction: {r['auction_type']}")
        if r.get("event_url"):
            print(f"  {'':>6}URL:     {r['event_url']}")
        if r.get("contact_name"):
            print(f"  {'':>6}Contact: {r['contact_name']}" +
                  (f" ({r.get('contact_role', '')})" if r.get("contact_role") else ""))
        if r.get("contact_email"):
            email_st = f" [{r['email_status']}]" if r.get("email_status") else ""
            print(f"  {'':>6}Email:   {r['contact_email']}{email_st}")
        if r.get("organization_phone_maps"):
            print(f"  {'':>6}Phone:   {r['organization_phone_maps']}")
        if r.get("organization_address"):
            print(f"  {'':>6}Address: {r['organization_address']}")
        if r.get("evidence_auction"):
            ev = r["evidence_auction"]
            if len(ev) > 120:
                ev = ev[:120] + "..."
            print(f"  {'':>6}Evidence: {ev}")

        print(f"  {'':>6}" + "-" * 66)

    print(f"\n  {len(results)} lead(s) displayed.\n")


def download_results(base_url, api_key, job_id, output_path):
    """Download results file."""
    ext = os.path.splitext(output_path)[1].lstrip(".")
    if ext not in ("csv", "json", "xlsx"):
        ext = "csv"

    resp = requests.get(
        f"{base_url}/api/v1/results/{job_id}?format={ext}",
        headers={"X-API-Key": api_key},
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"  Download failed ({resp.status_code}): {resp.text}")
        return False

    with open(output_path, "wb") as f:
        f.write(resp.content)
    size_kb = len(resp.content) / 1024
    print(f"  Results saved: {output_path} ({size_kb:.1f} KB)")
    return True


def resume_job(base_url, api_key, job_id):
    """Resume an interrupted job."""
    resp = requests.post(
        f"{base_url}/api/v1/resume/{job_id}",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={},
        timeout=30,
    )
    if resp.status_code != 200:
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        print(f"  Resume failed ({resp.status_code}): {data.get('error', resp.text)}")
        return None
    data = resp.json()
    print(f"  Resumed: new job {data['job_id']} ({data['remaining']} remaining, {data['completed_in_parent']} already done)")
    return data["job_id"]


def main():
    parser = argparse.ArgumentParser(description="Auction Intel API Client")
    parser.add_argument("--key", required=True, help="API key (ak_...)")
    parser.add_argument("--file", default="", help="Text file with domains (one per line). If omitted, paste interactively.")
    parser.add_argument("--output", default="results.csv", help="Output filename (default: results.csv)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default: {DEFAULT_URL})")
    parser.add_argument("--resume-job", default="", help="Specific job_id to resume")
    parser.add_argument("--poll", type=int, default=5, help="Poll interval in seconds (default: 5)")
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║      AUCTION INTEL API CLIENT        ║")
    print("  ║      auctionintel.app                ║")
    print("  ╚══════════════════════════════════════╝")

    # Resume flow
    if args.resume_job:
        print(f"\n  Resuming job: {args.resume_job}")
        new_job_id = resume_job(args.url, args.key, args.resume_job)
        if not new_job_id:
            sys.exit(1)
        print("\n  Polling for progress...")
        result = poll_status(args.url, args.key, new_job_id, interval=args.poll)
        if result and result.get("status") == "complete":
            display_results(args.url, args.key, new_job_id)
            download_results(args.url, args.key, new_job_id, args.output)
        return

    # Load or prompt for domains
    if args.file:
        domains = load_domains(args.file)
        print(f"\n  Loaded {len(domains)} domains from {args.file}")
    else:
        domains = prompt_domains()

    # Select tiers
    selected_tiers = prompt_tiers()

    # Confirm
    if not confirm_job(domains, selected_tiers):
        print("\n  Job cancelled.")
        sys.exit(0)

    # Submit
    job_id = submit_job(args.url, args.key, domains, selected_tiers)

    print("\n  Polling for progress...")
    result = poll_status(args.url, args.key, job_id, interval=args.poll)

    if result and result.get("status") == "complete":
        display_results(args.url, args.key, job_id)
        download_results(args.url, args.key, job_id, args.output)
    elif result and result.get("status") == "error":
        print("\n  Job failed. You can resume with:")
        print(f"  python api_client.py --key {args.key} --resume-job {job_id} --output {args.output}")


if __name__ == "__main__":
    main()
