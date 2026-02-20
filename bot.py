#!/usr/bin/env python3
"""
AUCTIONFINDER — Nonprofit Auction Event Finder Bot

Takes nonprofit domains/names as input (comma or newline separated),
searches for upcoming auction events using Claude with web search grounding,
and outputs structured CSV + JSON results.

Usage:
    python bot.py                      # interactive stdin input
    python bot.py nonprofits.txt       # read from file
    python bot.py --output results     # custom output filename prefix
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

import anthropic

# ─── Configuration ────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

BATCH_SIZE = 5          # 5 nonprofits per batch
MAX_PARALLEL_BATCHES = 3  # 3 batches (15 concurrent) — Tier 2: 30 web searches/sec
MAX_NONPROFITS = 1000
DELAY_BETWEEN_BATCHES = 1  # seconds between batches (Tier 2 handles high throughput)
MAX_RETRIES = 3             # retry 429 errors up to 3 times
RETRY_BACKOFF = [10, 20, 40]  # seconds to wait before each retry

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

# ─── Prompt ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a nonprofit event researcher. Search the web thoroughly for each nonprofit to find upcoming fundraising events, especially galas, auctions, or similar benefit events.

Look for:
1. The official nonprofit name
2. Upcoming fundraising events (galas, auctions, benefit dinners, etc.) in 2026 or later
3. Whether the event includes a silent auction, live auction, or both
4. Event dates, URLs, and descriptions
5. Contact information (name, email, role) for the events/development team
6. Organization address and phone number

Prioritize events that include auctions (silent or live). Galas and benefit dinners very often include auction components — check carefully. Also check """ + ", ".join(ALLOWLISTED_PLATFORMS) + """ for events.

Rules:
- ONLY events from 2026 or later — ignore all past events
- "status" = "found" if qualifying event found, "3rdpty_found" if on a third-party platform, "not_found" if nothing found
- confidence_score 0.0-1.0 reflecting accuracy
- Use empty string "" for fields you can't find
- Always include real URLs, never fabricated ones
- evidence_date = raw date text from the site; evidence_auction = direct quote proving auction component
- contact_role = their actual job title, not a generic label
- Do not use generic footer emails — only emails from staff/team/contact pages

You MUST respond with a JSON code block:

```json
{
  "nonprofit_name": "Full Organization Name",
  "event_title": "Full Event Title",
  "evidence_date": "Raw text from site showing the date",
  "auction_type": "silent|live|Live and Silent|unknown",
  "event_date": "M/D/YYYY",
  "event_url": "https://url-to-event-page",
  "confidence_score": 0.95,
  "evidence_auction": "Raw text proving auction component exists",
  "contact_name": "First Last",
  "contact_email": "email@domain.org",
  "contact_role": "Actual job title",
  "organization_address": "Full street address, City, ST ZIP",
  "organization_phone_maps": "phone number",
  "contact_source_url": "URL where contact info was found",
  "event_summary": "2-3 sentence summary of what was found",
  "status": "found|3rdpty_found|not_found"
}
```"""


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


def build_user_prompt(nonprofit: str) -> str:
    return f'Research this nonprofit and find upcoming auction events: "{nonprofit}"'


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


# ─── Quick Scan Prompt ────────────────────────────────────────────────────────

QUICK_SCAN_PROMPT = """Search the web thoroughly for the nonprofit at "{nonprofit}" and find information about their upcoming fundraising events, especially galas, auctions, or similar benefit events.

Look for:
1. The official nonprofit name
2. Upcoming fundraising events (galas, auctions, benefit dinners, etc.) in 2026 or later
3. Whether the event includes a silent auction, live auction, or both
4. Event dates, URLs, and descriptions
5. Contact information (name, email, role) for the events/development team
6. Organization address and phone number

Prioritize events that include auctions (silent or live). Galas and benefit dinners very often include auction components — check carefully.

You MUST respond with ONLY a JSON code block in this exact format — no other text before or after:

```json
{{
  "has_event": true,
  "confidence": 0.85,
  "nonprofit_name": "Full official name of the nonprofit",
  "event_title": "Name of the fundraising event",
  "event_date": "M/D/YYYY format",
  "evidence_date": "Raw text showing date evidence found on the site",
  "event_url": "URL where event info was found",
  "auction_type": "silent|live|Live and Silent|unknown",
  "evidence_auction": "Direct quote or evidence that an auction is part of the event",
  "contact_name": "Name of events/development contact",
  "contact_email": "email@domain.org",
  "contact_role": "Their actual job title",
  "organization_address": "Full street address",
  "organization_phone_maps": "Phone number",
  "contact_source_url": "URL where contact info was found",
  "notes": "2-3 sentence summary of what was found"
}}
```

Rules:
- Set "has_event" to true if you found a qualifying event, false if not
- "confidence" should be 0.0-1.0 reflecting accuracy of the data
- If you cannot find info for a field, use an empty string ""
- Always include real URLs you found, not made-up ones
- "evidence_date" should be raw text from the website showing the date
- "evidence_auction" should be a direct quote proving the auction component
- ONLY events from 2026 or later — ignore past events"""

TARGETED_PROMPT = """You are a research assistant. Find the {missing_field} for the following event:

Organization: {nonprofit}
Event: {event_title}
Event Date: {event_date}

## Search strategy by field type:
- event_url: Find and visit the actual event/auction page on the org's website. Prefer /auction over /events.
- contact_email: Check /contact, /staff, /about, /team pages on the org website.
- contact_role: FIRST check the org's own website (/about, /staff, /team, /news, /blog). If not found, search Google for the contact person's name + org name. The org site ALWAYS trumps LinkedIn.
- organization_address: Check the org's /contact page, website footer, or search Google Maps for the org name.
- organization_phone_maps: Check the org's /contact page, website footer, or Google Maps listing.

NEVER fabricate or guess URLs. NEVER construct search/query URLs like "/search?query=...". Only return URLs of real pages you actually visited.

Respond with ONLY valid JSON (no markdown):
{{
  "{missing_field}": "the value you found or empty string",
  "source_url": "URL where you found it"
}}"""


# ─── 3-Phase Research Functions ──────────────────────────────────────────────

SEARCH_SYSTEM = """You are a nonprofit event researcher. You MUST use your web_search tool to find information — NEVER answer from memory alone. Search the web thoroughly for each query. If you cannot find information through web search, say so — do not guess or fabricate."""


def _claude_call(client: anthropic.Anthropic, prompt: str, system: str = None) -> str:
    """Synchronous Claude call with web search grounding. Retries on 429."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            kwargs = dict(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=system or SEARCH_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                temperature=0.2,
            )
            response = client.messages.create(**kwargs)
            # Extract text from response content blocks
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            return "\n".join(text_parts)
        except anthropic.RateLimitError:
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt]
                print(f"    [429] Rate limited, waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


async def _quick_scan(client: anthropic.Anthropic, nonprofit: str) -> Dict[str, Any]:
    """Phase 1: Quick scan — 1 Claude call, lightweight prompt."""
    prompt = QUICK_SCAN_PROMPT.format(nonprofit=nonprofit)
    text = await asyncio.to_thread(_claude_call, client, prompt)
    return extract_json(text)


async def _deep_research(client: anthropic.Anthropic, nonprofit: str) -> Dict[str, Any]:
    """Phase 2: Deep research — 1 Claude call with full SYSTEM_PROMPT as system param."""
    prompt = f"""Research this nonprofit and find upcoming auction events: "{nonprofit}"

You MUST search the web. Try these searches:
1. "{nonprofit} gala 2026"
2. "{nonprofit} auction 2026"
3. "{nonprofit} fundraiser 2026"

Remember: ONLY return valid JSON, no other text."""
    text = await asyncio.to_thread(_claude_call, client, prompt, system=SYSTEM_PROMPT)
    return extract_json(text)


async def _targeted_followup(
    client: anthropic.Anthropic, nonprofit: str, event_title: str,
    event_date: str, missing_field: str,
) -> Dict[str, Any]:
    """Phase 3: Targeted follow-up — 1 Claude call for a single missing field."""
    prompt = TARGETED_PROMPT.format(
        missing_field=missing_field, nonprofit=nonprofit,
        event_title=event_title, event_date=event_date,
    )
    text = await asyncio.to_thread(_claude_call, client, prompt)
    return extract_json(text)


def _quick_scan_to_full(scan: Dict[str, Any], nonprofit: str) -> Dict[str, Any]:
    """Convert a quick-scan result into the full 16-column format."""
    result = {col: "" for col in CSV_COLUMNS}
    result["nonprofit_name"] = scan.get("nonprofit_name", nonprofit)
    result["event_title"] = scan.get("event_title", "")
    result["event_date"] = scan.get("event_date", "")
    result["event_url"] = scan.get("event_url", "")
    result["auction_type"] = scan.get("auction_type", "")
    result["evidence_date"] = scan.get("evidence_date", "")
    result["contact_name"] = scan.get("contact_name", "")
    result["contact_email"] = scan.get("contact_email", "")
    result["contact_role"] = scan.get("contact_role", "")
    result["organization_address"] = scan.get("organization_address", "")
    result["organization_phone_maps"] = scan.get("organization_phone_maps", "")
    result["contact_source_url"] = scan.get("contact_source_url", "")
    result["confidence_score"] = scan.get("confidence", 0.0)
    result["evidence_auction"] = scan.get("evidence_auction", "")
    result["event_summary"] = scan.get("notes", "")
    result["status"] = "found" if scan.get("has_event") else "not_found"
    return result


def _missing_billable_fields(result: Dict[str, Any]) -> List[str]:
    """Return list of important fields that are missing/empty.
    Prioritized: billing-critical fields first, then completeness fields."""
    missing = []
    # Billing-critical (affect tier/price)
    if not _has_valid_url(result):
        missing.append("event_url")
    if not _is_valid_email(result.get("contact_email", "")):
        missing.append("contact_email")
    if not result.get("event_date", "").strip():
        missing.append("event_date")
    if not result.get("auction_type", "").strip():
        missing.append("auction_type")
    # Completeness fields (every field matters)
    if not result.get("contact_role", "").strip():
        missing.append("contact_role")
    if not result.get("organization_address", "").strip():
        missing.append("organization_address")
    if not result.get("organization_phone_maps", "").strip():
        missing.append("organization_phone_maps")
    return missing


# ─── API Calls ────────────────────────────────────────────────────────────────

async def research_nonprofit(
    client: anthropic.Anthropic,
    nonprofit: str,
    semaphore: asyncio.Semaphore,
    index: int,
    total: int,
) -> Dict[str, Any]:
    """Research a single nonprofit using 3-phase early-stop strategy."""
    async with semaphore:
        # ── Cache check ──
        from db import cache_get, cache_put
        cached = cache_get(nonprofit)
        if cached:
            print(f"  [{index}/{total}] CACHED: {nonprofit} -> {cached.get('event_title', '') or 'not_found'}", file=sys.stderr)
            cached["_source"] = "cache"
            cached["_api_calls"] = 0
            return cached

        print(f"  [{index}/{total}] Researching: {nonprofit}", file=sys.stderr)
        api_calls = 0
        text = ""
        try:
            # ── Phase 1: Quick Scan ──
            api_calls += 1
            scan = await _quick_scan(client, nonprofit)

            # Quick negative — high confidence no event
            if not scan.get("has_event") and scan.get("confidence", 0) >= 0.80:
                print(f"  [{index}/{total}] NOT_FOUND (quick): {nonprofit}", file=sys.stderr)
                result = _quick_scan_to_full(scan, nonprofit)
                result["_api_calls"] = api_calls
                result["_phase"] = "quick_scan"
                result["_processed_at"] = datetime.now(timezone.utc).isoformat()
                result["_source"] = "claude"
                cache_put(nonprofit, result)
                return result

            # Quick positive — only early-stop if ALL fields are filled
            if scan.get("has_event") and scan.get("confidence", 0) >= 0.85:
                full_from_scan = _quick_scan_to_full(scan, nonprofit)
                tier, _ = classify_lead_tier(full_from_scan)
                _all_filled = (
                    tier == "full"
                    and _has_valid_url(full_from_scan)
                    and full_from_scan.get("evidence_date", "").strip()
                    and full_from_scan.get("contact_role", "").strip()
                    and full_from_scan.get("organization_address", "").strip()
                    and full_from_scan.get("organization_phone_maps", "").strip()
                    and full_from_scan.get("contact_source_url", "").strip()
                )
                if _all_filled:
                    print(f"  [{index}/{total}] FOUND (quick-full): {nonprofit} -> {scan.get('event_title', '')}", file=sys.stderr)
                    full_from_scan["_api_calls"] = api_calls
                    full_from_scan["_phase"] = "quick_scan"
                    full_from_scan["_processed_at"] = datetime.now(timezone.utc).isoformat()
                    full_from_scan["_source"] = "claude"
                    cache_put(nonprofit, full_from_scan)
                    return full_from_scan

            # ── Phase 2: Deep Research ──
            api_calls += 1
            print(f"  [{index}/{total}] Phase 2 (deep): {nonprofit}", file=sys.stderr)
            result = await _deep_research(client, nonprofit)
            result["_processed_at"] = datetime.now(timezone.utc).isoformat()
            result["_source"] = "claude"

            status = result.get("status", "uncertain")

            # Dead lead from deep research
            if status == "not_found":
                print(f"  [{index}/{total}] NOT_FOUND (deep): {nonprofit}", file=sys.stderr)
                result["_api_calls"] = api_calls
                result["_phase"] = "deep_research"
                cache_put(nonprofit, result)
                return result

            # Check if billable
            tier, _ = classify_lead_tier(result)
            missing = _missing_billable_fields(result)

            # If already billable or missing more than 1 field, stop here
            if tier != "not_billable" and len(missing) == 0:
                title = result.get("event_title", "")
                print(f"  [{index}/{total}] {status.upper()} ({tier}): {nonprofit} -> {title}", file=sys.stderr)
                result["_api_calls"] = api_calls
                result["_phase"] = "deep_research"
                cache_put(nonprofit, result)
                return result

            # ── Phase 3: Targeted Follow-up (chase up to 3 missing fields) ──
            if missing and result.get("event_title", "").strip():
                for field in missing[:3]:
                    api_calls += 1
                    print(f"  [{index}/{total}] Phase 3 (targeted: {field}): {nonprofit}", file=sys.stderr)
                    try:
                        followup = await _targeted_followup(
                            client, nonprofit,
                            result.get("event_title", ""),
                            result.get("event_date", ""),
                            field,
                        )
                        value = followup.get(field, "").strip()
                        if value:
                            result[field] = value
                            if followup.get("source_url"):
                                result["contact_source_url"] = followup["source_url"]
                    except Exception:
                        pass  # Phase 3 failure is non-fatal

            # Final classification
            tier, _ = classify_lead_tier(result)
            title = result.get("event_title", "")
            status = result.get("status", "uncertain")
            print(f"  [{index}/{total}] {status.upper()} ({tier}): {nonprofit} -> {title}", file=sys.stderr)
            result["_api_calls"] = api_calls
            result["_phase"] = f"phase_{api_calls}"
            cache_put(nonprofit, result)
            return result

        except json.JSONDecodeError:
            print(f"  [{index}/{total}] ERROR (JSON parse): {nonprofit}", file=sys.stderr)
            err = _error_result(nonprofit, "Failed to parse response as JSON", text[:500] if text else "")
            cache_put(nonprofit, err)
            return err

        except Exception as e:
            err_msg = str(e)
            print(f"  [{index}/{total}] ERROR: {nonprofit} — {err_msg}", file=sys.stderr)
            # Don't cache rate limit errors — those should be retried immediately
            err = _error_result(nonprofit, err_msg)
            if "429" not in err_msg and "RESOURCE_EXHAUSTED" not in err_msg:
                cache_put(nonprofit, err)
            return err


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


# ─── Batch Processing ────────────────────────────────────────────────────────

async def process_batch(
    client: anthropic.Anthropic,
    batch: List[str],
    batch_num: int,
    total_batches: int,
    global_offset: int,
    total_nonprofits: int,
    semaphore: asyncio.Semaphore,
) -> List[Dict[str, Any]]:
    """Process a batch of nonprofits concurrently."""
    print(f"\n[Batch {batch_num}/{total_batches}] Starting {len(batch)} nonprofits...", file=sys.stderr)

    tasks = [
        research_nonprofit(client, np, semaphore, global_offset + i + 1, total_nonprofits)
        for i, np in enumerate(batch)
    ]
    results = await asyncio.gather(*tasks)

    # Batch summary
    statuses: Dict[str, int] = {}
    for r in results:
        s = r.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    summary = ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))
    print(f"[Batch {batch_num}/{total_batches}] Complete — {summary}", file=sys.stderr)

    return list(results)


async def run(nonprofits: List[str]) -> List[Dict[str, Any]]:
    """Run the full research pipeline."""
    if len(nonprofits) > MAX_NONPROFITS:
        print(f"Warning: Truncating to {MAX_NONPROFITS} nonprofits (received {len(nonprofits)})", file=sys.stderr)
        nonprofits = nonprofits[:MAX_NONPROFITS]

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Create batches
    batches = [nonprofits[i:i + BATCH_SIZE] for i in range(0, len(nonprofits), BATCH_SIZE)]
    total_batches = len(batches)

    print(f"Processing {len(nonprofits)} nonprofits in {total_batches} batch(es)", file=sys.stderr)
    print(f"Batch size: {BATCH_SIZE} | Max parallel batches: {MAX_PARALLEL_BATCHES}", file=sys.stderr)
    print(f"Model: {CLAUDE_MODEL} (Claude + Web Search)", file=sys.stderr)

    all_results: List[Dict[str, Any]] = []

    # Semaphore to limit total concurrent API calls
    semaphore = asyncio.Semaphore(BATCH_SIZE * MAX_PARALLEL_BATCHES)

    # Process batches in groups
    for i in range(0, total_batches, MAX_PARALLEL_BATCHES):
        group = batches[i:i + MAX_PARALLEL_BATCHES]
        global_offset = sum(len(b) for b in batches[:i])

        batch_tasks = [
            process_batch(
                client, batch,
                i + j + 1, total_batches,
                global_offset + sum(len(g) for g in group[:j]),
                len(nonprofits),
                semaphore,
            )
            for j, batch in enumerate(group)
        ]

        group_results = await asyncio.gather(*batch_tasks)
        for batch_results in group_results:
            all_results.extend(batch_results)

        completed = min(i + MAX_PARALLEL_BATCHES, total_batches)
        processed = sum(len(b) for b in batches[:completed])
        print(f"\n--- Progress: {processed}/{len(nonprofits)} nonprofits | "
              f"{completed}/{total_batches} batches ---", file=sys.stderr)

        # Delay between batch groups to respect rate limits
        if completed < total_batches:
            print(f"Waiting {DELAY_BETWEEN_BATCHES}s before next batch group...", file=sys.stderr)
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)

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
        # Skip lines that are too long to be a nonprofit name/domain (max 120 chars)
        if len(item) > 120:
            continue
        # Skip obvious non-nonprofit lines (numbers-only, single words like headers)
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
            "model": CLAUDE_MODEL,
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
    # Check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        print("Set it with: set ANTHROPIC_API_KEY=...", file=sys.stderr)
        sys.exit(1)

    # Parse arguments
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

    # Read input
    if input_file:
        with open(input_file, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        print("=" * 60, file=sys.stderr)
        print("  AUCTIONFINDER — Nonprofit Auction Event Finder", file=sys.stderr)
        print("  Powered by Claude + Web Search", file=sys.stderr)
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
    results = asyncio.run(run(nonprofits))
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
    print(f"  CSV output:       {csv_file}", file=sys.stderr)
    print(f"  JSON output:      {json_file}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


if __name__ == "__main__":
    main()
