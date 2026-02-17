# Letter to Anthropic Sales Team

**To:** sales@anthropic.com
**From:** Blake, Auction Intel (auctionintel.app)
**Subject:** Enterprise Inquiry — Web Search + Batch API for High-Volume Nonprofit Research Platform

---

Hi Anthropic Sales Team,

I'm the founder of **Auction Intel** (auctionintel.app), a SaaS platform that helps businesses find nonprofit auction and gala events across the United States. Our AI agent searches the web for upcoming auction events, verifies event details (dates, venues, auction types), and returns structured leads with contact information — all exported as clean spreadsheets.

We're currently running on **Gemini 2.5 Pro with Google Search grounding**, but we're hitting persistent rate limits (429 RESOURCE_EXHAUSTED) even when our Google API dashboard shows us well below our quotas. This is blocking our ability to serve customers reliably, and we're evaluating a switch to Claude.

I have several questions about Claude's capabilities, pricing, and capacity for our use case:

---

## Our Use Case

Each customer search works like this:
1. Customer selects 100-1,000 nonprofits from our IRS database (300K+ organizations)
2. Our AI agent researches each nonprofit for upcoming auction/gala events using **web search**
3. Each nonprofit requires 1-3 web-grounded API calls (quick scan, deep research, targeted follow-up with early stopping)
4. Results are returned as structured JSON with: event title, date, auction type, contact name/email, organization address/phone, event URL, confidence score
5. Results are saved as CSV/JSON/XLSX for download

**Web search is essential** — our entire product depends on the AI being able to search the live web for each nonprofit's upcoming events. This is equivalent to how we use Gemini's Google Search grounding today.

---

## Capacity Requirements

We need to plan for scaling to **10 concurrent users**, each running **up to 1,000 nonprofits** simultaneously:

- **Peak concurrent nonprofits in processing:** 10,000
- **API calls per nonprofit:** ~2 average (with early stopping)
- **Peak API calls per day:** ~20,000
- **Web searches per API call:** ~2 average
- **Peak web searches per day:** ~40,000

---

## Questions

### 1. Web Search Compatibility
Does Claude's web search tool (`web_search_20250305` / `web_search_20260209`) work the same way as Gemini's Google Search grounding for our use case? Specifically:
- Can Claude search the web for a specific nonprofit's website and find event pages, auction announcements, and gala invitations?
- Can we instruct Claude to return structured JSON output from its web search findings?
- Are there any restrictions on the types of queries or volume of searches we can perform?

### 2. Model Recommendation
Which model would you recommend for our use case, balancing cost and quality?
- We need reliable web search and structured JSON output
- We don't need creative writing or complex reasoning — this is information extraction
- **Haiku 4.5** ($1/$5 per MTok) or **Haiku 3.5** ($0.80/$4 per MTok) seem like the right price point
- Would Haiku models produce reliable enough structured output with web search, or would Sonnet be required?

### 3. Batch API + Web Search
Your docs confirm web search works with the Batch API. For our use case:
- Can we submit 1,000 nonprofit research requests as a single batch with web search enabled?
- Would this give us the 50% token discount while still performing live web searches?
- What's the typical turnaround time for a batch of 1,000 web-search-enabled requests?
- Are there any limitations on web search volume within a batch?

### 4. Rate Limits
This is our biggest pain point with Gemini. We need confidence that we won't hit rate limits at scale:
- What are the rate limits for web search requests at each usage tier?
- What tier would we need to support 10 concurrent users each running 1,000 nonprofit searches?
- Is there a way to get guaranteed throughput for our use case?
- What does the "too_many_requests" error handling look like in practice — is it a hard block or a temporary backoff?

### 5. Estimated Daily Cost
Based on our estimates (please validate):

| Component | Estimate | Daily Cost |
|-----------|----------|------------|
| Web searches | ~40,000/day | $400 |
| Input tokens (Haiku 4.5) | ~140M tokens/day | $140 |
| Output tokens (Haiku 4.5) | ~20M tokens/day | $100 |
| **Total (standard)** | | **~$640/day** |
| **Total (with Batch 50% off tokens)** | | **~$520/day** |
| **Total (Haiku 3.5 + Batch)** | | **~$496/day** |

With prompt caching on our system prompt (~1,500 tokens, identical across all calls):
- Cache hits at 0.1x base price could reduce input costs further
- Are cache hit rates reliable in batch mode? (Your docs mention 30-98% hit rates)

### 6. Volume Pricing
At ~$500-640/day ($15K-19K/month), are there volume discounts available? We're a growing startup and want to understand:
- Is there a committed-use discount?
- Enterprise tier pricing?
- Any startup or growth-stage programs?

### 7. Subscription / Plan Requirements
- Do we need to be on a specific plan to access the Batch API?
- Is web search available on all API tiers, or only certain plans?
- What usage tier do we need to reach for our throughput requirements?

---

## About Auction Intel

- **Product:** AI-powered nonprofit auction event finder
- **Market:** Companies that sell to nonprofit auction events (auctioneers, event suppliers, catering, etc.)
- **Current stage:** Live product with paying customers, evaluating infrastructure switch
- **Website:** auctionintel.app
- **Current stack:** Gemini 2.5 Pro + Google Search grounding (experiencing rate limit issues)

We're ready to do a test migration if the numbers work. Happy to jump on a call to discuss further.

Best regards,
Blake
Auction Intel
blake@auctionintel.us

---

*P.S. — If there's a solutions engineer who specializes in web search or batch processing use cases, we'd love to connect with them directly.*
