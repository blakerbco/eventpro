#!/usr/bin/env python3
"""
AUCTIONFINDER — Nonprofit Auction Event Finder Bot (Poe Edition)

Takes nonprofit domains/names as input (comma or newline separated),
searches for upcoming auction events via Poe bot (Gemini Pro 3 + Google Search),
and outputs structured CSV + JSON results.

Usage:
    python bot.py                      # interactive stdin input
    python bot.py nonprofits.txt       # read from file
    python bot.py --output results     # custom output filename prefix
    python bot.py --workers 3          # run 3 parallel Poe workers
"""

import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
load_dotenv()

import fastapi_poe as fp

# ─── Configuration ────────────────────────────────────────────────────────────

POE_API_KEY = os.environ.get("POE_API_KEY", "")
POE_BOT_NAME = os.environ.get("POE_BOT_NAME", "auctionintel.app")

BATCH_SIZE = 5          # nonprofits per batch
MAX_PARALLEL_BATCHES = 1  # sequential by default (overridden by --workers)
MAX_NONPROFITS = 5000
MAX_RETRIES = 3
RETRY_BACKOFF = [5, 10, 20]

# Delay between Poe API calls (seconds) — only kicks in on rate limits
DELAY_BETWEEN_CALLS = 0    # no delay by default — Poe calls already take 30-45s
DELAY_PER_WORKER = 0       # increased automatically on rate limit errors
MAX_WORKERS = 5

ALLOWLISTED_PLATFORMS = [
    "givesmart.com",
    "eventbrite.com",
    "givebutter.com",
    "charitybuzz.com",
    "biddingforgood.com",
    "32auctions.com",
    "auctria.com",
    "handbid.com",
    "onecause.com",
    "qtego.com",
    "galabid.com",
    "silentauctionpro.com",
    "bidpal.com",
    "networkforgood.com",
    "classy.org",
    "eventcaddy.com",
]

CSV_COLUMNS = [
    "nonprofit_name",
    "event_title",
    "event_type",
    "evidence_date",
    "auction_type",
    "event_date",
    "event_url",
    "confidence_score",
    "evidence_auction",
    "contact_name",
    "contact_email",
    "contact_role",
    "organization_address",
    "organization_phone_maps",
    "contact_source_url",
    "event_summary",
]

# ─── JSON Extraction ─────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
    """Robustly extract a JSON object from LLM response text.

    Handles markdown fences, surrounding prose, and nested braces.
    """
    text = text.strip()

    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first { and last } to extract JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Try matching balanced braces from the first {
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    raise json.JSONDecodeError("No valid JSON object found in response", text, 0)


# ─── Lead Classification ─────────────────────────────────────────────────────

def _is_valid_email(email: str) -> bool:
    """Basic email format check."""
    if not email or not isinstance(email, str):
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


GENERIC_PREFIXES = {"info@", "contact@", "admin@", "support@"}
DOWNGRADE_GENERIC_EMAILS = False  # toggle in settings


def _has_valid_url(result: Dict[str, Any]) -> bool:
    """Check if event_url is a real URL (starts with http)."""
    url = result.get("event_url", "").strip()
    return url.startswith("http://") or url.startswith("https://")


def classify_lead_tier(result: Dict[str, Any]) -> tuple:
    """Returns (tier_name, price_cents) based on fields present.
    All billable tiers REQUIRE event_url — no URL means $0."""
    has_title = bool(result.get("event_title", "").strip())
    has_date = bool(result.get("event_date", "").strip())
    has_url = _has_valid_url(result)
    has_auction = bool(result.get("auction_type", "").strip())
    has_name = bool(result.get("contact_name", "").strip())
    has_email = _is_valid_email(result.get("contact_email", ""))

    # Must have title + date + URL for any billable tier
    if not has_title or not has_date or not has_url:
        return ("not_billable", 0)

    tier = "not_billable"
    price = 0

    if has_title and has_date and has_url and has_auction and has_name and has_email:
        tier, price = "full", 150       # $1.50
    elif has_title and has_date and has_url and has_auction and has_email:
        tier, price = "partial", 125    # $1.25
    elif has_title and has_date and has_url and has_email:
        tier, price = "semi", 100       # $1.00
    elif has_title and has_date and has_url:
        tier, price = "bare", 75        # $0.75

    # Optional generic email downgrade
    if DOWNGRADE_GENERIC_EMAILS and has_email:
        email_lower = result.get("contact_email", "").lower()
        if any(email_lower.startswith(p) for p in GENERIC_PREFIXES):
            downgrades = {"full": "partial", "partial": "semi", "semi": "bare", "bare": "bare"}
            new_tier = downgrades.get(tier, tier)
            if new_tier != tier:
                tier = new_tier
                tier_prices = {"full": 150, "partial": 125, "semi": 100, "bare": 75, "not_billable": 0}
                price = tier_prices.get(tier, 0)

    return (tier, price)


def _missing_billable_fields(result: Dict[str, Any]) -> List[str]:
    """Return list of important fields that are missing/empty."""
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


# ─── Poe Bot API ─────────────────────────────────────────────────────────────

def call_poe_bot_sync(nonprofit: str, extra_delay: float = 0) -> str:
    """Call the Poe bot synchronously, streaming the full response text.
    Retries on transient/rate-limit errors with adaptive backoff:
    - Rate limit: wait 60s + add 1s to future delays, keep retrying until success
    - Other errors: standard retry with backoff
    """
    prompt = nonprofit
    attempt = 0
    max_attempts = 20  # generous limit for rate-limit retries

    while attempt < max_attempts:
        try:
            message = fp.ProtocolMessage(role="user", content=prompt)
            full_text = ""
            for partial in fp.get_bot_response_sync(
                messages=[message],
                bot_name=POE_BOT_NAME,
                api_key=POE_API_KEY,
            ):
                full_text += partial.text
            return full_text
        except Exception as e:
            err_msg = str(e)
            is_rate_limit = any(x in err_msg.lower() for x in ["429", "rate", "too many", "quota"])

            if is_rate_limit:
                # Adaptive backoff: wait 60s, add 1s to global delay
                global DELAY_BETWEEN_CALLS
                DELAY_BETWEEN_CALLS += 1
                wait = 60
                print(f"    [RATE LIMIT] Poe rate limited, waiting {wait}s. "
                      f"Increased base delay to {DELAY_BETWEEN_CALLS}s. "
                      f"Attempt {attempt + 1}...", file=sys.stderr)
                time.sleep(wait)
                attempt += 1
            elif attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else 40
                print(f"    [ERROR] Poe call failed ({err_msg[:80]}), retrying in {wait}s ({attempt + 1}/{MAX_RETRIES})...", file=sys.stderr)
                time.sleep(wait)
                attempt += 1
            else:
                raise

    raise Exception(f"Poe bot call failed after {max_attempts} attempts for: {nonprofit}")


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

    # Confidence
    conf = poe_data.get("confidence_score", poe_data.get("confidence", 0.0))
    try:
        result["confidence_score"] = float(conf)
    except (ValueError, TypeError):
        result["confidence_score"] = 0.0

    # Status
    status = poe_data.get("status", "")
    has_event = poe_data.get("has_event", None)
    if status in ("found", "3rdpty_found", "not_found"):
        result["status"] = status
    elif has_event is True:
        result["status"] = "found"
    elif has_event is False:
        result["status"] = "not_found"
    else:
        # Infer from fields
        if result["event_title"] and result["event_url"]:
            result["status"] = "found"
        else:
            result["status"] = "not_found"

    result["_processed_at"] = datetime.now(timezone.utc).isoformat()
    result["_source"] = f"poe:{POE_BOT_NAME}"
    return result


def _error_result(nonprofit: str, error: str, raw: str = "") -> Dict[str, Any]:
    """Return a standardized error/not-found result."""
    result = {col: "" for col in CSV_COLUMNS}
    result["nonprofit_name"] = nonprofit
    result["confidence_score"] = 0.0
    result["status"] = "uncertain"
    result["event_summary"] = f"Error during research: {error}"
    result["_processed_at"] = datetime.now(timezone.utc).isoformat()
    if raw:
        result["_raw_response"] = raw
    return result


# ─── Research Pipeline ────────────────────────────────────────────────────────

async def research_nonprofit(
    nonprofit: str,
    semaphore: asyncio.Semaphore,
    index: int,
    total: int,
    worker_id: int = 0,
) -> Dict[str, Any]:
    """Research a single nonprofit via Poe bot (single call)."""
    async with semaphore:
        # ── Cache check ──
        from db import cache_get, cache_put
        cached = cache_get(nonprofit)
        if cached:
            print(f"  [{index}/{total}] CACHED: {nonprofit} -> {cached.get('event_title', '') or 'not_found'}", file=sys.stderr)
            cached["_source"] = "cache"
            cached["_api_calls"] = 0
            return cached

        delay = DELAY_BETWEEN_CALLS + (worker_id * DELAY_PER_WORKER)
        print(f"  [{index}/{total}] Researching [Poe:{POE_BOT_NAME} w{worker_id}]: {nonprofit}", file=sys.stderr)

        text = ""
        try:
            text = await asyncio.to_thread(call_poe_bot_sync, nonprofit)
            poe_data = extract_json(text)
            result = _poe_result_to_full(poe_data, nonprofit)
            result["_api_calls"] = 1

            tier, _ = classify_lead_tier(result)
            status = result.get("status", "uncertain")
            title = result.get("event_title", "")
            print(f"  [{index}/{total}] {status.upper()} ({tier}): {nonprofit} -> {title}", file=sys.stderr)

            cache_put(nonprofit, result)

            # Delay between calls to avoid rate limits
            await asyncio.sleep(delay)
            return result

        except json.JSONDecodeError:
            print(f"  [{index}/{total}] ERROR (JSON parse): {nonprofit}", file=sys.stderr)
            err = _error_result(nonprofit, "Failed to parse Poe response as JSON", text[:500] if text else "")
            cache_put(nonprofit, err)
            await asyncio.sleep(delay)
            return err

        except Exception as e:
            err_msg = str(e)[:500]
            print(f"  [{index}/{total}] ERROR: {nonprofit} -> {err_msg}", file=sys.stderr)
            err = _error_result(nonprofit, err_msg)
            # Don't cache transient errors
            is_transient = any(x in err_msg for x in ["429", "rate", "timeout", "overloaded"])
            if not is_transient:
                cache_put(nonprofit, err)
            await asyncio.sleep(delay)
            return err


async def process_batch(
    batch: List[str],
    batch_num: int,
    total_batches: int,
    global_offset: int,
    total_nonprofits: int,
    semaphore: asyncio.Semaphore,
    worker_id: int = 0,
) -> List[Dict[str, Any]]:
    """Process a batch of nonprofits sequentially (one at a time per worker)."""
    print(f"\n[Batch {batch_num}/{total_batches}] Starting {len(batch)} nonprofits (worker {worker_id})...", file=sys.stderr)

    results = []
    for i, np_name in enumerate(batch):
        result = await research_nonprofit(
            np_name, semaphore,
            global_offset + i + 1, total_nonprofits,
            worker_id=worker_id,
        )
        results.append(result)

    # Batch summary
    statuses: Dict[str, int] = {}
    for r in results:
        s = r.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    summary = ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))
    print(f"[Batch {batch_num}/{total_batches}] Complete — {summary}", file=sys.stderr)

    return results


async def run(nonprofits: List[str], num_workers: int = 1) -> List[Dict[str, Any]]:
    """Run the full research pipeline with parallel Poe workers."""
    if len(nonprofits) > MAX_NONPROFITS:
        print(f"Warning: Truncating to {MAX_NONPROFITS} nonprofits (received {len(nonprofits)})", file=sys.stderr)
        nonprofits = nonprofits[:MAX_NONPROFITS]

    num_workers = min(num_workers, MAX_WORKERS)

    print(f"Processing {len(nonprofits)} nonprofits with {num_workers} Poe worker(s)", file=sys.stderr)
    print(f"Bot: {POE_BOT_NAME} | Base delay: {DELAY_BETWEEN_CALLS}s + {DELAY_PER_WORKER}s/worker", file=sys.stderr)

    # Split nonprofits across workers (round-robin)
    worker_lists: List[List[str]] = [[] for _ in range(num_workers)]
    for i, np in enumerate(nonprofits):
        worker_lists[i % num_workers].append(np)

    # Each worker gets its own semaphore (1 concurrent call per worker)
    semaphore = asyncio.Semaphore(num_workers)

    async def worker_task(worker_id: int, np_list: List[str]) -> List[Dict[str, Any]]:
        results = []
        for i, np_name in enumerate(np_list):
            # Global index: figure out the original position
            global_idx = worker_id + (i * num_workers) + 1
            r = await research_nonprofit(
                np_name, semaphore, global_idx, len(nonprofits),
                worker_id=worker_id,
            )
            results.append(r)
        return results

    # Launch all workers in parallel
    tasks = [
        worker_task(w, worker_lists[w])
        for w in range(num_workers)
        if worker_lists[w]  # skip empty workers
    ]
    worker_results = await asyncio.gather(*tasks)

    # Flatten and sort by original order
    all_results = []
    for wr in worker_results:
        all_results.extend(wr)

    return all_results


# ─── I/O ──────────────────────────────────────────────────────────────────────

def parse_input(raw: str) -> List[str]:
    """Parse comma or newline separated nonprofit names/domains.
    Filters out lines that are too long or look like documentation text."""
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
    """Write results to CSV matching the sample format."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in results:
            row = {col: r.get(col, "") for col in CSV_COLUMNS}
            writer.writerow(row)


def write_json(results: List[Dict[str, Any]], filepath: str, elapsed: float) -> None:
    """Write results to JSON with metadata."""
    output = {
        "meta": {
            "total_nonprofits": len(results),
            "processing_time_seconds": round(elapsed, 2),
            "model": f"Poe Bot: {POE_BOT_NAME}",
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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    input_file: Optional[str] = None
    output_prefix: str = ""
    num_workers: int = 1

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--output" and i + 1 < len(args):
            output_prefix = args[i + 1]
            i += 2
        elif args[i] == "--workers" and i + 1 < len(args):
            num_workers = min(int(args[i + 1]), MAX_WORKERS)
            i += 2
        elif not args[i].startswith("--"):
            input_file = args[i]
            i += 1
        else:
            i += 1

    if not POE_API_KEY:
        print("ERROR: POE_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    # Read input
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

    print(f"\nFound {len(nonprofits)} nonprofit(s) to research.\n", file=sys.stderr)

    # Run research
    start = time.time()
    results = asyncio.run(run(nonprofits, num_workers=num_workers))
    elapsed = time.time() - start

    # Generate output filenames
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output_prefix:
        csv_file = f"{output_prefix}.csv"
        json_file = f"{output_prefix}.json"
    else:
        csv_file = f"auction_results_{timestamp}.csv"
        json_file = f"auction_results_{timestamp}.json"

    # Write outputs
    write_csv(results, csv_file)
    write_json(results, json_file, elapsed)

    # Summary
    found = sum(1 for r in results if r.get("status") == "found")
    external = sum(1 for r in results if r.get("status") == "3rdpty_found")
    not_found = sum(1 for r in results if r.get("status") == "not_found")
    uncertain = sum(1 for r in results if r.get("status") == "uncertain")

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"  RESULTS SUMMARY", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"  Total processed:  {len(results)}", file=sys.stderr)
    print(f"  Found:            {found}", file=sys.stderr)
    print(f"  3rd Party found:  {external}", file=sys.stderr)
    print(f"  Not found:        {not_found}", file=sys.stderr)
    print(f"  Uncertain:        {uncertain}", file=sys.stderr)
    print(f"  Time elapsed:     {elapsed:.1f}s", file=sys.stderr)
    print(f"  Workers:          {num_workers}", file=sys.stderr)
    print(f"  CSV output:       {csv_file}", file=sys.stderr)
    print(f"  JSON output:      {json_file}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


if __name__ == "__main__":
    main()
