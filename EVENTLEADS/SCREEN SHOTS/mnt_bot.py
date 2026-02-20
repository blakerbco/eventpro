# poe: name=Event-Finder

import json
import re
from datetime import datetime, timezone

from fastapi_poe.types import SettingsResponse

MODEL_BOT = "Gemini-3-Pro"
MODEL_PARAMS = {"web_search": True, "thinking_budget": 0}

REQUIRED_FIELDS = [
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
    "status",
]

RESEARCH_PROMPT = """You are a nonprofit event researcher. Your task is to research the nonprofit at the domain "{domain}" and find information about their upcoming fundraising events, especially galas, auctions, or similar benefit events.

Search the web thoroughly for this nonprofit's website, events page, and any related fundraising event information. Look for:
1. The official nonprofit name
2. Upcoming fundraising events (galas, auctions, benefit dinners, etc.)
3. Whether the event includes a silent auction, live auction, or both
4. Event dates, URLs, and descriptions
5. Contact information (name, email, role) for the events/development team
6. Organization address and phone number

Focus on events happening in 2025 or 2026. Prioritize events that include auctions (silent or live).

You MUST respond with ONLY a JSON code block in this exact format - no other text before or after:

```json
{{
  "nonprofit_name": "Full official name of the nonprofit",
  "event_title": "Name of the fundraising event",
  "event_type": "gala|auction|dinner|walkathon|golf|other",
  "evidence_date": "Raw text showing date evidence found on the site",
  "auction_type": "silent|live|both|none|unknown",
  "event_date": "MM/DD/YYYY format",
  "event_url": "URL where event info was found",
  "confidence_score": 0.85,
  "evidence_auction": "Direct quote or evidence that an auction is part of the event",
  "contact_name": "Name of events/development contact",
  "contact_email": "email@domain.org",
  "contact_role": "events|development|executive_director|other",
  "organization_address": "Full street address",
  "organization_phone_maps": "Phone number",
  "contact_source_url": "URL where contact info was found",
  "event_summary": "2-4 sentence summary of what was found, including source details",
  "status": "found|not_found"
}}
```

Rules for the response:
- "status" must be "found" if you found a qualifying fundraising event with auction potential, or "not_found" if you could not find one.
- "confidence_score" should be 0.0-1.0 reflecting how confident you are in the accuracy of the data.
- If you cannot find information for a field, use an empty string "" for text fields, 0.0 for confidence_score.
- For "event_type", use the most specific applicable type.
- For "auction_type", use "unknown" if the event likely has an auction but you can't confirm the type.
- Always include real URLs you found, not made-up ones.
- The "evidence_date" should be the raw text from the website showing the date.
- The "evidence_auction" should be a direct quote or clear evidence about the auction component.
"""

BATCH_SIZE = 5  # How many domains to research in parallel


def parse_json_from_response(response_text, required_fields=None):
    """Extract and parse JSON from response."""
    match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
    if match:
        json_str = match.group(1).strip()
    else:
        match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if match:
            json_str = match.group(0).strip()
        else:
            return None

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    if required_fields:
        missing_fields = set(required_fields) - set(parsed.keys())
        if missing_fields:
            return None
    return parsed


def parse_domains(text):
    """Parse a list of domains from user input. Handles newline-separated, comma-separated, etc."""
    # Split by newlines, commas, spaces, or semicolons
    raw_items = re.split(r'[\n,;\s]+', text.strip())
    domains = []
    for item in raw_items:
        item = item.strip().lower()
        # Remove protocol prefixes if present
        item = re.sub(r'^https?://', '', item)
        # Remove trailing slashes/paths
        item = item.split('/')[0]
        # Basic domain validation
        if item and '.' in item and len(item) > 3:
            domains.append(item)
    return domains


poe.update_settings(SettingsResponse(
    introduction_message=(
        "Hi! I'm **Event Finder**. Paste a list of nonprofit domains (up to 5000) and I'll "
        "research each one to find upcoming fundraising events with auctions.\n\n"
        "**Input format:** One domain per line, e.g.:\n"
        "```\nlpb.org\nanhspfan.org\nspoonsacrossamerica.org\n```\n\n"
        "For each domain, I'll search the web and return structured JSON with event details, "
        "auction info, contact information, and more."
    ),
))


class EventFinder:
    def research_domain(self, domain):
        """Research a single domain and return the result dict."""
        try:
            response = poe.call(
                MODEL_BOT,
                RESEARCH_PROMPT.format(domain=domain),
                parameters=MODEL_PARAMS,
            )
            result = parse_json_from_response(response.text, ["status"])
            if result:
                result["_processed_at"] = datetime.now(timezone.utc).isoformat()
                return result
            else:
                return {
                    "nonprofit_name": domain,
                    "event_title": "",
                    "event_type": "",
                    "evidence_date": "",
                    "auction_type": "",
                    "event_date": "",
                    "event_url": "",
                    "confidence_score": 0.0,
                    "evidence_auction": "",
                    "contact_name": "",
                    "contact_email": "",
                    "contact_role": "",
                    "organization_address": "",
                    "organization_phone_maps": "",
                    "contact_source_url": "",
                    "event_summary": "Failed to parse structured response from research.",
                    "status": "error",
                    "_processed_at": datetime.now(timezone.utc).isoformat(),
                }
        except Exception as e:
            return {
                "nonprofit_name": domain,
                "event_title": "",
                "event_type": "",
                "evidence_date": "",
                "auction_type": "",
                "event_date": "",
                "event_url": "",
                "confidence_score": 0.0,
                "evidence_auction": "",
                "contact_name": "",
                "contact_email": "",
                "contact_role": "",
                "organization_address": "",
                "organization_phone_maps": "",
                "contact_source_url": "",
                "event_summary": f"Error during research: {str(e)}",
                "status": "error",
                "_processed_at": datetime.now(timezone.utc).isoformat(),
            }

    def run(self):
        user_text = poe.query.text.strip()
        if not user_text:
            raise poe.BotError("Please paste a list of nonprofit domains to research.")

        domains = parse_domains(user_text)
        if not domains:
            raise poe.BotError(
                "Could not find any valid domains in your message. "
                "Please paste domains one per line (e.g., lpb.org)"
            )

        if len(domains) > 5000:
            raise poe.BotError(
                f"You provided {len(domains)} domains, but the maximum is 5000. "
                "Please reduce your list."
            )

        with poe.start_message() as msg:
            msg.write(f"**Researching {len(domains)} nonprofit domain(s) using {MODEL_BOT}...**\n\n")

        all_results = []
        total = len(domains)

        # Process in batches
        for batch_start in range(0, total, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total)
            batch_domains = domains[batch_start:batch_end]

            with poe.start_message() as msg:
                msg.write(f"🔍 Processing batch {batch_start // BATCH_SIZE + 1}: domains {batch_start + 1}-{batch_end} of {total}...")

            # Research batch in parallel
            batch_results = poe.parallel(
                *[lambda d=d: self.research_domain(d) for d in batch_domains],
                return_exceptions=True,
            )

            for i, result in enumerate(batch_results):
                domain = batch_domains[i]
                if isinstance(result, Exception):
                    result = {
                        "nonprofit_name": domain,
                        "event_title": "",
                        "event_type": "",
                        "evidence_date": "",
                        "auction_type": "",
                        "event_date": "",
                        "event_url": "",
                        "confidence_score": 0.0,
                        "evidence_auction": "",
                        "contact_name": "",
                        "contact_email": "",
                        "contact_role": "",
                        "organization_address": "",
                        "organization_phone_maps": "",
                        "contact_source_url": "",
                        "event_summary": f"Exception during research: {str(result)}",
                        "status": "error",
                        "_processed_at": datetime.now(timezone.utc).isoformat(),
                    }
                all_results.append(result)

            # Save checkpoint after each batch
            batch_num = batch_start // BATCH_SIZE + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            checkpoint_json = json.dumps(all_results, indent=2, ensure_ascii=False).encode("utf-8")
            with poe.start_message() as msg:
                msg.write(f"💾 **Checkpoint saved** — batch {batch_num}/{total_batches} done ({len(all_results)}/{total} domains)\n")
                msg.attach_file(
                    name=f"checkpoint_batch_{batch_num}_of_{total_batches}.json",
                    contents=checkpoint_json,
                    content_type="application/json",
                )

        # Count results by status
        found_count = sum(1 for r in all_results if r.get("status") == "found")
        not_found_count = sum(1 for r in all_results if r.get("status") == "not_found")
        error_count = sum(1 for r in all_results if r.get("status") == "error")

        # Output final summary and full JSON
        with poe.start_message() as msg:
            msg.write(f"**Research Complete!**\n\n")
            msg.write(f"- ✅ **Found events:** {found_count}\n")
            msg.write(f"- ❌ **No events found:** {not_found_count}\n")
            if error_count > 0:
                msg.write(f"- ⚠️ **Errors:** {error_count}\n")
            msg.write(f"- **Total processed:** {total}\n\n")
            msg.write("---\n\n")
            msg.write("**Full JSON Results:**\n\n")
            msg.write("````json\n")
            msg.write(json.dumps(all_results, indent=2, ensure_ascii=False))
            msg.write("\n````\n")
            msg.attach_file(
                name="results_final.json",
                contents=json.dumps(all_results, indent=2, ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
            )


if __name__ == "__main__":
    bot = EventFinder()
    bot.run()