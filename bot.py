#!/usr/bin/env python3
"""
AUCTIONFINDER — Nonprofit Auction Event Finder Bot (Poe Edition)

Calls the Poe bot one domain at a time. No batching, no workers.
Matches the proven AUCTIONINTEL.APP_BOT.PY calling pattern exactly.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv
load_dotenv()

import fastapi_poe as fp

# ─── Configuration ────────────────────────────────────────────────────────────

POE_API_KEY = os.environ.get("POE_API_KEY", "").strip()
POE_BOT_NAME = os.environ.get("POE_BOT_NAME", "auctionintel.app").strip()

MAX_NONPROFITS = 5000
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 15
PAUSE_BETWEEN_DOMAINS = 2

ALLOWLISTED_PLATFORMS = [
    "givesmart.com", "eventbrite.com", "givebutter.com", "charitybuzz.com",
    "biddingforgood.com", "32auctions.com", "auctria.com", "handbid.com",
    "onecause.com", "qtego.com", "galabid.com", "silentauctionpro.com",
    "bidpal.com", "networkforgood.com", "classy.org", "eventcaddy.com",
]

CSV_COLUMNS = [
    "nonprofit_name", "event_title", "event_type", "evidence_date",
    "auction_type", "event_date", "event_url", "confidence_score",
    "evidence_auction", "contact_name", "contact_email", "email_status",
    "contact_role", "organization_address", "organization_phone_maps",
    "contact_source_url", "event_summary",
]


# ─── Poe Bot API (matches AUCTIONINTEL.APP_BOT.PY exactly) ──────────────────

def call_poe_bot_sync(domain: str) -> str:
    """Send a single domain to the Poe bot and return the full streamed response.
    Copied from the working AUCTIONINTEL.APP_BOT.PY script."""
    message = fp.ProtocolMessage(role="user", content=domain)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            full_response = ""
            for partial in fp.get_bot_response_sync(
                messages=[message],
                bot_name=POE_BOT_NAME,
                api_key=POE_API_KEY,
            ):
                full_response += partial.text
            return full_response

        except Exception as e:
            error_msg = str(e)
            print(f"    Attempt {attempt}/{MAX_RETRIES} failed: {error_msg[:120]}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                print(f"    Retrying in {RETRY_DELAY_SECONDS}s...", file=sys.stderr)
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                print(f"    All {MAX_RETRIES} attempts failed.", file=sys.stderr)
                return ""


# ─── JSON Extraction (matches AUCTIONINTEL.APP_BOT.PY exactly) ──────────────

def extract_json_from_response(response_text: str) -> list:
    """Try to parse JSON event records from the bot response text.
    Returns a list of dicts (may be empty)."""
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

    # Strategy 3: ```json code block containing a single object
    match = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(1))
            return [obj]
        except json.JSONDecodeError:
            pass

    # Strategy 4: Individual JSON objects
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


# Keep old name as alias for app.py imports
def extract_json(text: str) -> dict:
    """Legacy wrapper — extracts first JSON object from response."""
    results = extract_json_from_response(text)
    if results:
        return results[0]
    raise json.JSONDecodeError("No valid JSON found in response", text, 0)


# ─── Lead Classification ─────────────────────────────────────────────────────

def _is_valid_email(email: str) -> bool:
    if not email or not isinstance(email, str):
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


EMAILABLE_API_KEY = os.environ.get("EMAILABLE_API_KEY", "")

def validate_email_emailable(email: str) -> str:
    """Call Emailable API. Returns: deliverable, risky, undeliverable, catch-all, unknown."""
    if not EMAILABLE_API_KEY:
        print(f"[EMAILABLE] SKIP — no API key set", flush=True)
        return "unknown"
    if not email:
        print(f"[EMAILABLE] SKIP — empty email", flush=True)
        return "unknown"
    try:
        print(f"[EMAILABLE] Calling API for: {email} (key: {EMAILABLE_API_KEY[:10]}...)", flush=True)
        resp = requests.get(
            "https://api.emailable.com/v1/verify",
            params={"email": email, "api_key": EMAILABLE_API_KEY},
            timeout=10,
        )
        print(f"[EMAILABLE] HTTP {resp.status_code} for {email}", flush=True)
        if resp.status_code == 200:
            data = resp.json()
            state = data.get("state", "unknown")
            score = data.get("score", "?")
            reason = data.get("reason", "?")
            print(f"[EMAILABLE] {email} -> state={state}, score={score}, reason={reason}", flush=True)
            return state
        else:
            print(f"[EMAILABLE] ERROR {resp.status_code}: {resp.text[:200]}", flush=True)
            return "unknown"
    except Exception as e:
        print(f"[EMAILABLE] EXCEPTION for {email}: {e}", flush=True)
        return "unknown"


def _has_valid_url(result: Dict[str, Any]) -> bool:
    url = result.get("event_url", "").strip()
    return url.startswith("http://") or url.startswith("https://")


def classify_lead_tier(result: Dict[str, Any]) -> tuple:
    """Returns (tier_name, price_cents) based on fields present.
    3-tier system: decision_maker ($1.75), outreach_ready ($1.25), event_verified ($0.75)."""
    has_title = bool(result.get("event_title", "").strip())
    has_date = bool(result.get("event_date", "").strip())
    has_url = _has_valid_url(result)
    has_name = bool(result.get("contact_name", "").strip())
    has_email = _is_valid_email(result.get("contact_email", ""))

    if not has_title or not has_date or not has_url:
        return ("not_billable", 0)

    if has_email and has_name:
        return ("decision_maker", 175)
    if has_email:
        return ("outreach_ready", 125)
    return ("event_verified", 75)


def _missing_billable_fields(result: Dict[str, Any]) -> List[str]:
    missing = []
    if not _has_valid_url(result):
        missing.append("event_url")
    if not _is_valid_email(result.get("contact_email", "")):
        missing.append("contact_email")
    if not result.get("event_date", "").strip():
        missing.append("event_date")
    if not result.get("auction_type", "").strip():
        missing.append("auction_type")
    if not result.get("contact_role", "").strip():
        missing.append("contact_role")
    if not result.get("organization_address", "").strip():
        missing.append("organization_address")
    if not result.get("organization_phone_maps", "").strip():
        missing.append("organization_phone_maps")
    return missing


# ─── Result Mapping ──────────────────────────────────────────────────────────

def _poe_result_to_full(poe_data: dict, nonprofit: str) -> Dict[str, Any]:
    """Map Poe bot JSON response fields to our CSV_COLUMNS format."""
    result = {col: "" for col in CSV_COLUMNS}
    result["nonprofit_name"] = poe_data.get("nonprofit_name", nonprofit)
    result["event_title"] = poe_data.get("event_title", "")
    result["event_type"] = poe_data.get("event_type", "")
    result["event_date"] = poe_data.get("event_date", "")
    result["event_url"] = poe_data.get("event_url", "")
    result["auction_type"] = poe_data.get("auction_type", "")
    result["evidence_date"] = poe_data.get("evidence_date", "")
    result["evidence_auction"] = poe_data.get("evidence_auction", "")
    result["contact_name"] = poe_data.get("contact_name", "")
    result["contact_email"] = poe_data.get("contact_email", "")
    result["contact_role"] = poe_data.get("contact_role", "")
    result["organization_address"] = poe_data.get("organization_address", "")
    result["organization_phone_maps"] = poe_data.get("organization_phone_maps", "")
    result["contact_source_url"] = poe_data.get("contact_source_url", "")
    result["event_summary"] = poe_data.get("event_summary", poe_data.get("notes", ""))

    conf = poe_data.get("confidence_score", poe_data.get("confidence", 0.0))
    try:
        result["confidence_score"] = float(conf)
    except (ValueError, TypeError):
        result["confidence_score"] = 0.0

    status = poe_data.get("status", "")
    has_event = poe_data.get("has_event", None)
    if status in ("found", "3rdpty_found", "not_found"):
        result["status"] = status
    elif has_event is True:
        result["status"] = "found"
    elif has_event is False:
        result["status"] = "not_found"
    elif result["event_title"] and result["event_url"]:
        result["status"] = "found"
    else:
        result["status"] = "not_found"

    result["_processed_at"] = datetime.now(timezone.utc).isoformat()
    result["_source"] = f"poe:{POE_BOT_NAME}"
    return result


def _error_result(nonprofit: str, error: str, raw: str = "") -> Dict[str, Any]:
    result = {col: "" for col in CSV_COLUMNS}
    result["nonprofit_name"] = nonprofit
    result["confidence_score"] = 0.0
    result["status"] = "uncertain"
    result["event_summary"] = f"Error during research: {error}"
    result["_processed_at"] = datetime.now(timezone.utc).isoformat()
    if raw:
        result["_raw_response"] = raw
    return result


# ─── I/O ──────────────────────────────────────────────────────────────────────

def parse_input(raw: str) -> List[str]:
    items = []
    for line in raw.replace(",", "\n").split("\n"):
        item = line.strip()
        if not item:
            continue
        if len(item) > 120:
            continue
        if item.replace(",", "").replace(".", "").replace("-", "").isdigit():
            continue
        items.append(item)
    return items


def write_csv(results: List[Dict[str, Any]], filepath: str) -> None:
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in results:
            row = {col: r.get(col, "") for col in CSV_COLUMNS}
            writer.writerow(row)


def write_json(results: List[Dict[str, Any]], filepath: str, elapsed: float) -> None:
    output = {
        "meta": {
            "total_nonprofits": len(results),
            "processing_time_seconds": round(elapsed, 2),
            "model": "Auctionintel.app",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "summary": {
            "found": sum(1 for r in results if r.get("status") == "found"),
            "3rdpty_found": sum(1 for r in results if r.get("status") == "3rdpty_found"),
            "not_found": sum(1 for r in results if r.get("status") == "not_found"),
            "uncertain": sum(1 for r in results if r.get("status") == "uncertain"),
        },
        "results": results,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


# ─── Main (CLI) ──────────────────────────────────────────────────────────────

def main():
    input_file: Optional[str] = None
    output_prefix: str = ""

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--output" and i + 1 < len(args):
            output_prefix = args[i + 1]
            i += 2
        elif not args[i].startswith("--"):
            input_file = args[i]
            i += 1
        else:
            i += 1

    if not POE_API_KEY:
        print("ERROR: POE_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    if input_file:
        with open(input_file, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        print("=" * 60, file=sys.stderr)
        print("  AUCTIONFINDER — Nonprofit Auction Event Finder", file=sys.stderr)
        print(f"  Powered by Poe Bot: {POE_BOT_NAME}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("Enter nonprofit domains or names (comma or newline separated).", file=sys.stderr)
        print("Press Ctrl+Z then Enter (Windows) or Ctrl+D (Unix) when done:\n", file=sys.stderr)
        raw = sys.stdin.read()

    nonprofits = parse_input(raw)
    if not nonprofits:
        print("No nonprofits provided. Exiting.", file=sys.stderr)
        sys.exit(1)

    total = len(nonprofits)
    print(f"\nProcessing {total} nonprofit(s) via Poe Bot: {POE_BOT_NAME}\n", file=sys.stderr)

    start = time.time()
    all_results = []

    for idx, domain in enumerate(nonprofits, start=1):
        print(f"  [{idx}/{total}] {domain}", file=sys.stderr)

        text = call_poe_bot_sync(domain)
        if text:
            events = extract_json_from_response(text)
            for ev in events:
                result = _poe_result_to_full(ev, domain)
                result["_api_calls"] = 1
                all_results.append(result)
                tier, _ = classify_lead_tier(result)
                print(f"    -> {result['status'].upper()} ({tier}): {result.get('event_title', '')}", file=sys.stderr)
            if not events:
                all_results.append(_error_result(domain, "No JSON events in response", text[:300]))
                print(f"    -> No events found", file=sys.stderr)
        else:
            all_results.append(_error_result(domain, "Poe bot returned empty response"))
            print(f"    -> FAILED", file=sys.stderr)

        if idx < total:
            time.sleep(PAUSE_BETWEEN_DOMAINS)

    elapsed = time.time() - start

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = f"{output_prefix}.csv" if output_prefix else f"auction_results_{timestamp}.csv"
    json_file = f"{output_prefix}.json" if output_prefix else f"auction_results_{timestamp}.json"

    write_csv(all_results, csv_file)
    write_json(all_results, json_file, elapsed)

    found = sum(1 for r in all_results if r.get("status") == "found")
    not_found = sum(1 for r in all_results if r.get("status") == "not_found")
    uncertain = sum(1 for r in all_results if r.get("status") == "uncertain")

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"  RESULTS: {found} found, {not_found} not found, {uncertain} uncertain", file=sys.stderr)
    print(f"  Time: {elapsed:.1f}s | CSV: {csv_file} | JSON: {json_file}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


if __name__ == "__main__":
    main()
