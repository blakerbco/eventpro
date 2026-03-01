#!/usr/bin/env python3
"""
Auction Intel API Client — Local script for batch domain research.

Usage:
    python api_client.py --key ak_xxx --file domains.txt --output results.csv
    python api_client.py --key ak_xxx --file domains.txt --output results.csv --resume

Options:
    --key       API key (ak_...)
    --file      Text file with one domain per line
    --output    Output filename (csv, json, or xlsx)
    --url       API base URL (default: https://auctionintel.app)
    --resume    Auto-resume the most recent interrupted job for these domains
    --poll      Polling interval in seconds (default: 5)
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


def load_domains(filepath):
    """Load domains from a text file, one per line."""
    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        domains = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return domains


def submit_job(base_url, api_key, domains):
    """Submit a batch of domains for research."""
    resp = requests.post(
        f"{base_url}/api/v1/search",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={"domains": domains},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: Submit failed ({resp.status_code}): {resp.text}")
        sys.exit(1)
    data = resp.json()
    print(f"Job submitted: {data['job_id']} ({data['total_domains']} domains)")
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
    parser.add_argument("--file", required=True, help="Text file with domains (one per line)")
    parser.add_argument("--output", default="results.csv", help="Output filename (default: results.csv)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default: {DEFAULT_URL})")
    parser.add_argument("--resume", action="store_true", help="Resume the most recent interrupted job")
    parser.add_argument("--resume-job", default="", help="Specific job_id to resume")
    parser.add_argument("--poll", type=int, default=5, help="Poll interval in seconds (default: 5)")
    args = parser.parse_args()

    domains = load_domains(args.file)
    print(f"Loaded {len(domains)} domains from {args.file}")

    if args.resume_job:
        print(f"Resuming job: {args.resume_job}")
        new_job_id = resume_job(args.url, args.key, args.resume_job)
        if not new_job_id:
            sys.exit(1)
        job_id = new_job_id
    else:
        job_id = submit_job(args.url, args.key, domains)

    print("Polling for progress...")
    result = poll_status(args.url, args.key, job_id, interval=args.poll)

    if result and result.get("status") == "complete":
        download_results(args.url, args.key, job_id, args.output)
    elif result and result.get("status") == "error":
        print("Job failed. You can resume with:")
        print(f"  python api_client.py --key {args.key} --file {args.file} --resume-job {job_id} --output {args.output}")


if __name__ == "__main__":
    main()
