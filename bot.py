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
MAX_PARALLEL_BATCHES = 2  # 2 parallel batches
MAX_NONPROFITS = 1000
DELAY_BETWEEN_BATCHES = 2  # seconds between batches to avoid rate limits

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

SYSTEM_PROMPT = """You are an expert nonprofit auction event researcher. Your job is to find upcoming auction/gala/fundraiser events that include an auction component (silent auction, live auction, or both) for a given nonprofit organization.

## Research Methodology

Follow these steps IN ORDER for each nonprofit:

1. **Official Domain Search**: If a domain or URL is provided, search the official website for event pages. Look for URLs containing these HIGH-PRIORITY keywords:
   - "/gala" — DEAD GIVEAWAY of an auction event
   - "/auction", "/silent-auction", "/live-auction"
   - "/raffle" — raffle pages VERY OFTEN also include a silent auction component, ALWAYS check the full page text
   - "/benefit", "/ball", "/soiree", "/banquet"
   - "/fundraiser", "/fundraising", "/fund-a-need"
   - "/events", "/special-events", "/upcoming-events", "/calendar"
   - "/support", "/giving", "/donate", "/sponsorship"
   - "/dinner", "/luncheon", "/brunch", "/celebration"
   - "/tournament", "/golf" — golf tournaments often include silent auctions
   IMPORTANT: When you find ANY of these URL patterns, you MUST visit that page and scan the FULL text for auction indicators. A page titled "Gun/Meat Raffle" may also contain a silent auction — READ THE WHOLE PAGE.

2. **On-Page Auction Detection**: Once you find an event page, scan the FULL page text for these auction indicators:

   LIVE AUCTION indicators (if ANY of these appear, the event has a LIVE auction):
   "live auction", "auctioneer", "paddle raise", "raise the paddle", "bid calling", "bidding wars",
   "live bidding begins at", "auction block", "spotters", "ringmen", "table sponsorship", "reserved tables",
   "fund-a-need", "fund a need", "fund the need", "special appeal", "paddle up", "cash call",
   "raise your paddle", "live lot", "live lots", "grand auction", "gavel", "hammer price", "live bidding",
   "item opens live", "bid spotter", "floor bidder", "from the stage", "live appeal", "auctioneer-led",
   "call bid", "live auction begins", "live auction starts", "live auctioneer"

   SILENT AUCTION indicators (if ANY of these appear, the event has a SILENT auction):
   "silent auction", "mobile bidding", "bid from your phone", "text to bid", "browse items at your leisure",
   "proxy bidding", "auto-bid", "outbid notification", "silent auction items",
   "virtual auction", "online auction", "browse auction items"

   CLASSIFICATION RULES:
   - If ONLY live indicators found → auction_type = "live"
   - If ONLY silent indicators found → auction_type = "silent"
   - If BOTH live AND silent indicators found → auction_type = "Live and Silent"
   - Copy the exact matching text into evidence_auction field as proof

   Also look for GiveSmart/Handbid/OneCause/BiddingForGood links on the page — these confirm auction component (usually silent)

3. **Third-Party Platform Search**: Search these allowlisted auction/event platforms for events by this nonprofit:
   """ + ", ".join(ALLOWLISTED_PLATFORMS) + """

4. **General Web Search**: Search for "[nonprofit name] gala 2026", "[nonprofit name] auction 2026", "[nonprofit name] fundraiser auction 2026", "[nonprofit name] gala 2027", "[nonprofit name] silent auction 2026", "[nonprofit name] benefit dinner 2026".

5. **Contact Information**: Find a contact person for the event AND their job title:
   - First check the event page itself for contact info
   - Then check the org's /about, /staff, /team, /leadership, /our-team, /contact, /directory, /news, /blog pages
   - NEVER use email addresses found in the website footer — those are sitewide generic emails, not event contacts. Only use emails found on the actual event page, staff page, or contact directory.
   - For job title, the org's own website is the MOST TRUSTED source — search their site first
   - Search Google for "[contact name] [org name]" — org website results often show their title in snippets
   - Only use LinkedIn as a LAST RESORT if the org site doesn't show the title: search "[contact name] [org name] title"
   - contact_role must be their ACTUAL job title (e.g. "Engagement Specialist", "Director of Development"), not a generic label
   - Also find the organization's physical address and phone number (check footer, contact page, or Google Maps)

## CRITICAL DATE REQUIREMENT
- Today's date is February 2026.
- ONLY return events dated 2026 or later (2026, 2027, etc.)
- NEVER return past events from 2025 or earlier — those are NOT valid results
- If the only events you find are from 2025 or earlier, classify as "not_found"
- If a page shows both past and future events, ONLY report the future ones

## What Qualifies as a Match
- The event MUST have an auction component: silent auction, live auction, paddle raise, raffle with auction, or similar
- The event MUST be dated 2026 or later — no past events
- Evidence of the auction must be found in actual text on the page

## Classification
- "found" — auction event confirmed on the nonprofit's official domain
- "3rdpty_found" — auction event found on a third-party platform
- "not_found" — no upcoming auction events discovered anywhere
- "uncertain" — ambiguous results needing human review

## Required Output

You MUST respond with ONLY valid JSON (no markdown fences, no extra text) in this exact structure:

{
  "nonprofit_name": "Full Organization Name",
  "event_title": "Full Event Title",
  "evidence_date": "Raw text snippet from the website proving the date",
  "auction_type": "silent, live, or Live and Silent",
  "event_date": "MM/DD/YYYY",
  "event_url": "https://direct-url-to-event-page",
  "confidence_score": 0.95,
  "evidence_auction": "Raw text snippet from the website proving auction component exists",
  "contact_name": "First Last",
  "contact_email": "email@domain.org",
  "contact_role": "events or sponsorship or development",
  "organization_address": "Full street address, City, ST ZIP",
  "organization_phone_maps": "phone number",
  "contact_source_url": "URL where contact info was found",
  "event_summary": "1-2 sentence summary explaining what was found and how it was confirmed",
  "status": "found or 3rdpty_found or not_found or uncertain"
}

## Few-Shot Examples

### Example 1 (live and silent auction):
Input: "National Museum of Mexican Art"
Output:
{"nonprofit_name": "National Museum of Mexican Art", "event_title": "Gala de Arte: Agua Sagrada", "evidence_date": "Gala de Arte:  Agua Sagrada\\n\\nMay 1, 2026, 6:00 11:00 pm Aon Grand Ballroom at Navy Pier", "auction_type": "Live and Silent", "event_date": "5/1/2026", "event_url": "https://nationalmuseumofmexicanart.org/events/galadearte", "confidence_score": 0.98, "evidence_auction": "The Gala de Arte promises an unforgettable evening. In addition to networking with fellow supporters, youll enjoy a silent auction, live auction, exquisite cuisine, live music, and dancing.", "contact_name": "Barbara Engelskirchen", "contact_email": "barbara@nationalmuseumofmexicanart.org", "contact_role": "sponsorship", "organization_address": "1852 W 19th Street, Chicago, IL 60608", "organization_phone_maps": "(312) 738-1503", "contact_source_url": "https://nationalmuseumofmexicanart.org/events/galadearte", "event_summary": "The National Museum of Mexican Arts official Events page lists \\"Gala de Arte: Agua Sagrada\\" on May 1, 2026 at the Aon Grand Ballroom at Navy Pier in Chicago. The event description clearly states that attendees will enjoy a silent auction and live auction, confirming a 2026 gala auction on the official domain.", "status": "found"}

### Example 2 (silent auction):
Input: "Radio Milwaukee"
Output:
{"nonprofit_name": "Radio Milwaukee", "event_title": "SoundBites 2026 from Radio Milwaukee", "evidence_date": "06:00 PM - 10:00 PM on Thu, 5 Mar 2026", "auction_type": "silent", "event_date": "3/5/2026", "event_url": "https://radiomilwaukee.org/community-calendar/event/soundbites-16-01-2025-10-34-53", "confidence_score": 0.96, "evidence_auction": "General admission\\n\\u2022 Snacks and treats ...\\n\\u2022 Premium whiskey tasting ...\\n\\u2022 Silent auction with items ranging from arts and entertainment to food and beverages\\n\\u2022 Gift-card pull from local restaurants", "contact_name": "Jeremy Zuleger", "contact_email": "jeremy@radiomilwaukee.org", "contact_role": "sponsorship", "organization_address": "220 E Pittsburgh Ave, Milwaukee, WI 53204", "organization_phone_maps": "414-892-8900", "contact_source_url": "https://radiomilwaukee.org/community-calendar/event/soundbites-16-01-2025-10-34-53", "event_summary": "Radio Milwaukees official site lists SoundBites 2026, its signature fundraising soir\\u00e9e at the Harley-Davidson Museum on Thursday, March 5, 2026. The event description explicitly includes a silent auction with items and states that all proceeds benefit Radio Milwaukee, making it a confirmed upcoming auction fundraiser.", "status": "found"}

### Example 3 (not found — use this format when no auction event is found):
Input: "Some Unknown Nonprofit"
Output:
{"nonprofit_name": "Some Unknown Nonprofit", "event_title": "", "evidence_date": "", "auction_type": "", "event_date": "", "event_url": "", "confidence_score": 0.0, "evidence_auction": "", "contact_name": "", "contact_email": "", "contact_role": "", "organization_address": "", "organization_phone_maps": "", "contact_source_url": "", "event_summary": "No upcoming auction events found after searching the official domain, third-party platforms, and general web search.", "status": "not_found"}

## CRITICAL RULES
- Return ONLY the JSON object, no markdown code fences, no explanation before or after
- evidence_date and evidence_auction must be ACTUAL text copied from the source pages — NEVER leave these blank when event_date or auction_type is filled in. You saw the text on the page — copy it verbatim.
- event_date must be in M/D/YYYY format (no leading zeros required)
- confidence_score must be a number between 0.0 and 1.0
- If multiple auction events exist, return the EARLIEST upcoming one (2026 or later)
- If no auction event is found, return the not_found template with empty strings
- ONLY events from 2026 or later are valid — IGNORE all 2025 or earlier events
- Search thoroughly: check the actual event page URL, look for "Save the Date" text, check for dates like "September 17, 2026" that may be buried in the page content
- Look for paddle raise, fund-a-need, raise the paddle — these count as auction components

## URL QUALITY RULES — MANDATORY
- NEVER fabricate or guess URLs — every URL you return must be a real page you actually visited
- NEVER construct search/query URLs like "/search?query=..." — those are NOT valid
- event_url: If the event has a dedicated auction page (e.g. /auction, /silent-auction, /gala-auction), use THAT instead of a generic page like /annual-dinner or /events. The most specific auction-related URL wins.
- contact_source_url: MUST be the real URL where you found the contact name/email. If you found contact info, you found it somewhere — record that URL. NEVER leave this blank when contact_name or contact_email is filled in."""


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

QUICK_SCAN_PROMPT = """You are a nonprofit auction event researcher. Determine if the following nonprofit has any upcoming auction/gala/fundraiser events (2026 or later) with an auction component (silent auction, live auction, paddle raise, etc.).

Search the web for "{nonprofit}" upcoming events. If you find an event, you MUST visit the actual event page. Do NOT rely only on search snippets.

## YOUR #1 GOAL: FILL IN EVERY SINGLE FIELD BELOW
An incomplete result is a POOR result. Every blank field is a failure. Search multiple pages to fill all fields.

## SEARCH STEPS (do ALL of these):
1. Find the event page — get event_title, event_date, evidence_date, auction_type, evidence_auction, event_url
2. Find a contact person — check the event page, then /about, /staff, /team, /contact pages for name + email
     IMPORTANT: NEVER use email addresses found in the website footer — those are sitewide generic emails, not event contacts. Only use emails found on the actual event page, staff page, or contact directory.
3. Find their job title — PRIORITY ORDER:
     a. Check the org's own website first: /about, /staff, /team, /news, /blog pages — the org site is the MOST TRUSTED source
     b. Search Google for "[name] [org name]" — the org's own pages often appear showing their title
     c. Only if not found on org site, try LinkedIn: search "[name] [org name] title"
     The org's own website ALWAYS trumps LinkedIn for job title accuracy.
4. Find org address + phone — check footer, /contact page, or Google Maps for "[nonprofit name]"
5. Record contact_source_url — the URL where you found the contact info

## URL RULES
- event_url must be a REAL page you visited — NEVER fabricate URLs
- NEVER construct search/query URLs like "/search?query=..."
- Prefer the most specific auction page URL (e.g. /auction over /annual-dinner)

## EVIDENCE RULES
- evidence_date: copy the RAW date text from the page verbatim (e.g. "Saturday, April 10, 2026 | 6:00 PM"). NEVER blank when event_date is filled.
- evidence_auction: copy the RAW text proving auction exists. NEVER blank when auction_type is filled.

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "has_event": true/false,
  "confidence": 0.0 to 1.0,
  "nonprofit_name": "Full Name",
  "event_title": "Event Title or empty string",
  "event_date": "M/D/YYYY or empty string",
  "evidence_date": "Raw date text copied from the page",
  "event_url": "https://most-specific-auction-page-url or empty string",
  "auction_type": "silent/live/Live and Silent or empty string",
  "contact_name": "Name or empty string",
  "contact_email": "email or empty string",
  "contact_role": "Actual job title from staff page or LinkedIn",
  "organization_address": "Full street address, City, ST ZIP",
  "organization_phone_maps": "Phone number from website or Google Maps",
  "contact_source_url": "URL where contact info was found",
  "evidence_auction": "Raw text from page proving auction exists",
  "notes": "Brief explanation"
}}"""

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

def _claude_call(client: anthropic.Anthropic, prompt: str) -> str:
    """Synchronous Claude call with web search grounding."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        temperature=0.2,
    )
    # Extract text from response content blocks
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    return "\n".join(text_parts)


async def _quick_scan(client: anthropic.Anthropic, nonprofit: str) -> Dict[str, Any]:
    """Phase 1: Quick scan — 1 Claude call, lightweight prompt."""
    prompt = QUICK_SCAN_PROMPT.format(nonprofit=nonprofit)
    text = await asyncio.to_thread(_claude_call, client, prompt)
    return extract_json(text)


async def _deep_research(client: anthropic.Anthropic, nonprofit: str) -> Dict[str, Any]:
    """Phase 2: Deep research — 1 Claude call with full SYSTEM_PROMPT."""
    prompt = f"""{SYSTEM_PROMPT}

Research this nonprofit and find upcoming auction events: "{nonprofit}"

Remember: ONLY return valid JSON, no other text."""
    text = await asyncio.to_thread(_claude_call, client, prompt)
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
    """Parse comma or newline separated nonprofit names/domains."""
    items = []
    for line in raw.replace(",", "\n").split("\n"):
        item = line.strip()
        if item:
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
