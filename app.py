#!/usr/bin/env python3
"""
AUCTIONFINDER — Web Interface

User-authenticated web UI with wallet-based billing for the Nonprofit
Auction Event Finder. Uses Poe bot for research, Stripe
for wallet top-ups, and SSE for real-time progress streaming.

Usage:
    set POE_API_KEY=...
    set STRIPE_SECRET_KEY=sk_test_...
    set STRIPE_PUBLISHABLE_KEY=pk_test_...
    python app.py
"""

import asyncio
import csv
import io
import hashlib
import json
import os
import secrets
import sys
import time
import threading
import queue
import datetime as _dt
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, request, Response, jsonify, session,
    redirect, url_for, send_file,
)
import stripe
import openpyxl
import psycopg2

# ─── Import bot config & prompts ─────────────────────────────────────────────

from bot import (
    MAX_NONPROFITS, POE_BOT_NAME, PAUSE_BETWEEN_DOMAINS,
    ALLOWLISTED_PLATFORMS, CSV_COLUMNS, parse_input,
    _error_result, extract_json, extract_json_from_response,
    classify_lead_tier, _has_valid_url,
    _missing_billable_fields, call_poe_bot_sync, _poe_result_to_full,
)

from db import (
    init_db, create_user, authenticate, get_user, get_user_full,
    get_balance, add_funds, charge_research_fee, charge_lead_fee,
    has_sufficient_balance, get_transactions, get_research_fee_cents,
    update_password, get_spending_summary, get_job_breakdowns,
    create_search_job, complete_search_job, fail_search_job,
    get_user_jobs, cleanup_expired_jobs, cleanup_stale_running_jobs,
    get_user_by_email, create_reset_token, validate_reset_token,
    consume_reset_token,
    create_ticket, get_ticket, get_tickets_for_user, get_all_tickets,
    get_ticket_messages, add_ticket_message, update_ticket_status,
    mark_messages_read_by_user, mark_messages_read_by_admin,
    get_unread_ticket_count,
    purchase_exclusive_lead, is_lead_exclusive, get_user_exclusive_leads,
    EXCLUSIVE_LEAD_PRICE_CENTS,
    cache_get, cache_put, flush_uncertain_cache,
    save_result_file, get_result_file,
)
import emails
from html import escape as html_escape

# ─── App Configuration ───────────────────────────────────────────────────────

app = Flask(__name__)
# Stable secret key: use FLASK_SECRET_KEY env var, or derive from admin password so
# sessions survive Railway redeploys (no more random key = no more logout on every deploy)
_stable_seed = os.environ.get("AUCTIONFINDER_PASSWORD", "")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or (
    hashlib.sha256(f"auctionfinder-session::{_stable_seed}".encode()).hexdigest()
    if _stable_seed else secrets.token_hex(32)
)
app.permanent_session_lifetime = _dt.timedelta(days=30)

@app.before_request
def _make_session_permanent():
    session.permanent = True


# ─── Rate Limiting ──────────────────────────────────────────────────────────

_rate_limits: Dict[str, list] = {}  # key -> list of timestamps

def _rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Returns True if rate limit exceeded. Cleans up old entries."""
    now = time.time()
    if key not in _rate_limits:
        _rate_limits[key] = []
    # Remove expired timestamps
    _rate_limits[key] = [t for t in _rate_limits[key] if now - t < window_seconds]
    if len(_rate_limits[key]) >= max_requests:
        return True
    _rate_limits[key].append(now)
    return False

def _get_client_ip() -> str:
    """Get real client IP, respecting proxy headers."""
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

RESULTS_DIR = os.environ.get("RESULTS_DIR", "results")
DB_CONN_STRING = (
    os.environ.get("IRS_DB_CONNECTION")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PRIVATE_URL")
    or "postgresql://localhost/irs"
)
_src = "IRS_DB_CONNECTION" if os.environ.get("IRS_DB_CONNECTION") else \
       "DATABASE_URL" if os.environ.get("DATABASE_URL") else \
       "DATABASE_PRIVATE_URL" if os.environ.get("DATABASE_PRIVATE_URL") else "FALLBACK"
print(f"[IRS DB] Using: {_src} -> {DB_CONN_STRING[:30]}...")

# Stripe config
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
DOMAIN = os.environ.get("APP_DOMAIN", "http://localhost:5000")

os.makedirs(RESULTS_DIR, exist_ok=True)

# Store active jobs: job_id -> { status, results, progress_queue, ... }
jobs: Dict[str, Dict[str, Any]] = {}


def get_irs_db():
    """Get an IRS PostgreSQL database connection."""
    return psycopg2.connect(DB_CONN_STRING)


def _enrich_from_irs(results: List[Dict[str, Any]]) -> None:
    """Fill missing address/phone on lead results from the IRS nonprofit database."""
    # Collect names that need enrichment
    needs = [r for r in results if r.get("nonprofit_name") and (
        not r.get("organization_address") or not r.get("organization_phone_maps")
    )]
    if not needs:
        return
    try:
        conn = get_irs_db()
        cur = conn.cursor()
        for r in needs:
            name = r["nonprofit_name"].strip()
            # Try exact match first, then wildcard (handles "Inc", "Foundation", etc.)
            cur.execute(
                'SELECT "PhysicalAddress", "PhysicalCity", "PhysicalState", '
                '"PhysicalZIP", "BusinessOfficerPhone" '
                'FROM tax_year_2019_search WHERE "OrganizationName" ILIKE %s LIMIT 1',
                (name,)
            )
            row = cur.fetchone()
            if not row:
                # Try with wildcard suffix (e.g., "New Moms" -> "New Moms%")
                cur.execute(
                    'SELECT "PhysicalAddress", "PhysicalCity", "PhysicalState", '
                    '"PhysicalZIP", "BusinessOfficerPhone" '
                    'FROM tax_year_2019_search WHERE "OrganizationName" ILIKE %s LIMIT 1',
                    (name + '%',)
                )
                row = cur.fetchone()
            if not row:
                print(f"[IRS ENRICH] No match for: {name}")
                continue
            print(f"[IRS ENRICH] Matched: {name}")
            addr, city, state, zipcode, phone = row
            if not r.get("organization_address") and addr:
                parts = [p for p in [addr, city, f"{state} {zipcode}" if state else zipcode] if p]
                r["organization_address"] = ", ".join(parts)
            if not r.get("organization_phone_maps") and phone:
                r["organization_phone_maps"] = phone
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[IRS ENRICH] Warning: {e}")


# ─── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.headers.get("Accept", "").startswith("text/event-stream"):
                return Response("Unauthorized", status=401)
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def _current_user():
    """Get current user dict from session."""
    uid = session.get("user_id")
    if uid:
        return get_user(uid)
    return None


def _is_admin():
    """Check if current user is admin."""
    user = _current_user()
    return user and user.get("is_admin", False)


def _inject_nav_badge(html: str) -> str:
    """Replace {{SUPPORT_BADGE}} with unread count badge if any."""
    uid = session.get("user_id")
    if not uid:
        return html.replace("{{SUPPORT_BADGE}}", "")
    is_admin = session.get("is_admin", False)
    count = get_unread_ticket_count(uid, is_admin)
    if count > 0:
        badge = f' <span style="background:#f87171;color:#fff;border-radius:50%;padding:1px 6px;font-size:10px;font-weight:700;">{count}</span>'
    else:
        badge = ""
    return html.replace("{{SUPPORT_BADGE}}", badge)


# ─── Sidebar Navigation ─────────────────────────────────────────────────────

_SIDEBAR_CSS = """
  .sidebar { position:fixed; top:0; left:0; width:260px; height:100vh; background:#0a0a0a; border-right:1px solid #1a1a1a; display:flex; flex-direction:column; z-index:100; overflow-y:auto; transition:transform 0.3s ease; scrollbar-width:thin; scrollbar-color:#eab308 #000; }
  .sidebar::-webkit-scrollbar { width:8px; }
  .sidebar::-webkit-scrollbar-track { background:#000; }
  .sidebar::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }
  .sidebar::-webkit-scrollbar-thumb:hover { background:#ffd900; }
  .sidebar-logo { padding:20px 24px; border-bottom:1px solid #1a1a1a; }
  .sidebar-logo img { height:44px; }
  .sidebar-section { padding:16px 12px 4px; }
  .sidebar-label { font-size:10px; color:#525252; text-transform:uppercase; letter-spacing:1px; padding:0 12px; margin-bottom:4px; font-weight:600; }
  .sidebar-nav { display:flex; flex-direction:column; }
  .sidebar-nav a { display:flex; align-items:center; gap:10px; padding:9px 12px; color:#a3a3a3; text-decoration:none; font-size:13px; border-radius:6px; border-left:3px solid transparent; margin:1px 0; }
  .sidebar-nav a:hover { color:#f5f5f5; background:#141414; }
  .sidebar-nav a.active { color:#f5f5f5; background:#141414; border-left-color:#eab308; }
  .sidebar-nav a svg { width:16px; height:16px; flex-shrink:0; opacity:0.5; }
  .sidebar-nav a:hover svg, .sidebar-nav a.active svg { opacity:1; }
  .sidebar-bottom { margin-top:auto; padding:12px; border-top:1px solid #1a1a1a; }
  .sidebar-bottom a { display:flex; align-items:center; gap:10px; padding:9px 12px; color:#737373; text-decoration:none; font-size:13px; border-radius:6px; }
  .sidebar-bottom a svg { width:14px; height:14px; flex-shrink:0; }
  .sidebar-bottom a:hover { color:#f87171; background:#141414; }
  .topbar { position:fixed; top:0; left:260px; right:0; height:48px; background:#0a0a0a; border-bottom:1px solid #1a1a1a; display:flex; align-items:center; justify-content:flex-end; padding:0 24px; z-index:99; }
  .topbar .user-email { color:#737373; font-size:12px; }
  .hamburger { display:none; background:none; border:none; color:#a3a3a3; cursor:pointer; padding:8px; margin-right:auto; }
  .main-content { margin-left:260px; padding-top:48px; min-height:100vh; }
  .sidebar-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:99; }
  @media (max-width:768px) {
    .sidebar { transform:translateX(-100%); }
    .sidebar.open { transform:translateX(0); }
    .topbar { left:0; }
    .hamburger { display:block; }
    .main-content { margin-left:0; }
    .sidebar-overlay.open { display:block; z-index:99; }
  }
"""

_SIDEBAR_ICONS = {
    "search": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
    "database": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/></svg>',
    "wallet": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="6" width="20" height="14" rx="2"/><path d="M2 10h20"/><circle cx="16" cy="14" r="1"/></svg>',
    "billing": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 2v20l3-2 3 2 3-2 3 2V2l-3 2-3-2-3 2Z"/><path d="M8 10h8M8 14h4"/></svg>',
    "results": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    "getting-started": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
    "support": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    "profile": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="8" r="4"/><path d="M20 21a8 8 0 1 0-16 0"/></svg>',
    "logout": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
    "tools": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    "analyzer": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="2"/><path d="M21 21l-4.3-4.3"/><path d="M3.05 11a8 8 0 0 1 15.9 0M3.05 13a8 8 0 0 0 15.9 0"/></svg>',
}

_SIDEBAR_NAV_ITEMS = [
    ("Research", [
        ("database", "/database", "Search Database"),
        ("search", "/", "Auction Search"),
    ]),
    ("Account", [
        ("wallet", "/wallet", "Wallet"),
        ("billing", "/billing", "Billing"),
        ("results", "/results", "Results"),
    ]),
    ("Help", [
        ("getting-started", "/getting-started", "Getting Started"),
        ("support", "/support", "Support{{SUPPORT_BADGE}}"),
    ]),
    ("Tools", [
        ("tools", "/tools/merge", "File Merger"),
        ("analyzer", "/tools/analyzer", "Field Analyzer", True),  # admin-only
    ]),
    ("Settings", [
        ("profile", "/profile", "Profile"),
    ]),
]


def _build_sidebar_html(active):
    """Build sidebar + topbar HTML. `active` = page key like 'wallet', 'search'."""
    is_admin = _is_admin()
    sections = ""
    for group_label, items in _SIDEBAR_NAV_ITEMS:
        links = ""
        for item in items:
            key, href, label = item[0], item[1], item[2]
            admin_only = item[3] if len(item) > 3 else False
            if admin_only and not is_admin:
                continue
            cls = ' class="active"' if key == active else ""
            icon = _SIDEBAR_ICONS.get(key, "")
            links += f'      <a href="{href}"{cls}>{icon} {label}</a>\n'
        sections += (
            f'  <div class="sidebar-section">\n'
            f'    <div class="sidebar-label">{group_label}</div>\n'
            f'    <nav class="sidebar-nav">\n{links}    </nav>\n'
            f'  </div>\n'
        )

    return (
        '<div class="sidebar-overlay" id="sidebarOverlay" onclick="document.getElementById(\'sidebar\').classList.remove(\'open\');this.classList.remove(\'open\');"></div>\n'
        '<aside class="sidebar" id="sidebar">\n'
        '  <div class="sidebar-logo"><a href="/"><img src="/static/logo_dark.png" alt="Auction Intel" style="height:44px;"></a></div>\n'
        f'{sections}'
        '  <div class="sidebar-bottom">\n'
        f'    <a href="/logout">{_SIDEBAR_ICONS["logout"]} Logout</a>\n'
        '  </div>\n'
        '</aside>\n'
        '<div class="topbar">\n'
        '  <button class="hamburger" onclick="document.getElementById(\'sidebar\').classList.toggle(\'open\');document.getElementById(\'sidebarOverlay\').classList.toggle(\'open\');">\n'
        '    <svg width="20" height="20" fill="none" viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>\n'
        '  </button>\n'
        '  <span class="user-email">{{EMAIL}}</span>\n'
        '</div>\n'
    )


_FAVICON_TAG = '<link rel="icon" type="image/png" href="/static/favicon.png">'

def _inject_sidebar(html, active):
    """Replace {{SIDEBAR_HTML}} placeholder with built sidebar, and {{SIDEBAR_CSS}} with CSS."""
    html = html.replace("{{SIDEBAR_CSS}}", _SIDEBAR_CSS)
    html = html.replace("{{SIDEBAR_HTML}}", _build_sidebar_html(active))
    # Inject favicon into all sidebar pages
    if _FAVICON_TAG not in html:
        html = html.replace("</head>", _FAVICON_TAG + "\n</head>", 1)
    return html


# ─── Research Worker (runs in background thread with its own event loop) ─────

def _research_one(
    nonprofit: str, index: int, total: int, progress_q: queue.Queue,
    user_id: Optional[int] = None, job_id: str = "", is_admin: bool = False,
    is_trial: bool = False, balance_exhausted: list = None,
) -> Dict[str, Any]:
    """Research a single nonprofit via Poe bot — simple synchronous call.
    Matches the proven AUCTIONINTEL.APP_BOT.PY pattern exactly.
    """
    # Skip if balance already exhausted
    if balance_exhausted and balance_exhausted[0]:
        result = _error_result(nonprofit, "Skipped — insufficient balance")
        result["_balance_skipped"] = True
        progress_q.put({
            "type": "result", "index": index, "total": total,
            "nonprofit": nonprofit, "status": "error",
            "event_title": "Skipped — insufficient balance", "confidence": 0,
            "tier": "not_billable", "tier_price": 0,
        })
        return result

    progress_q.put({"type": "processing", "index": index, "total": total, "nonprofit": nonprofit})

    # ── Cache check ──
    cached = cache_get(nonprofit)
    if cached:
        cached["_source"] = "cache"
        cached["_api_calls"] = 0
        status = cached.get("status", "uncertain")
        tier, price = classify_lead_tier(cached)
        title = cached.get("event_title", "")

        if user_id and not is_admin and status in ("found", "3rdpty_found") and price > 0:
            fee = get_research_fee_cents(total)
            total_charge = fee + price
            if is_trial and price <= 0:
                pass
            elif get_balance(user_id) >= total_charge:
                charge_research_fee(user_id, 1, job_id, fee)
                charge_lead_fee(user_id, tier, price, job_id, nonprofit)
                progress_q.put({"type": "balance", "balance": get_balance(user_id)})
            else:
                if balance_exhausted:
                    balance_exhausted[0] = True
                cached["_balance_exhausted"] = True
                progress_q.put({"type": "balance_warning", "message": "Insufficient balance — stopping job."})
                tier, price = "not_billable", 0
        elif user_id and not is_admin and not is_trial:
            fee = get_research_fee_cents(total)
            if get_balance(user_id) >= fee:
                charge_research_fee(user_id, 1, job_id, fee)
                progress_q.put({"type": "balance", "balance": get_balance(user_id)})

        progress_q.put({
            "type": "result", "index": index, "total": total,
            "nonprofit": nonprofit, "status": status,
            "event_title": title, "confidence": cached.get("confidence_score", 0),
            "tier": tier, "tier_price": price,
        })
        return cached

    # ── Call Poe bot (same as AUCTIONINTEL.APP_BOT.PY) ──
    text = call_poe_bot_sync(nonprofit)

    if not text:
        # Empty response = bot call failed
        progress_q.put({
            "type": "result", "index": index, "total": total,
            "nonprofit": nonprofit, "status": "error",
            "event_title": "Poe bot returned empty response", "confidence": 0,
            "tier": "not_billable", "tier_price": 0,
        })
        err = _error_result(nonprofit, "Poe bot returned empty response")
        return err

    # Parse response — bot may return array of events
    events = extract_json_from_response(text)
    if not events:
        progress_q.put({
            "type": "result", "index": index, "total": total,
            "nonprofit": nonprofit, "status": "not_found",
            "event_title": "No events found", "confidence": 0,
            "tier": "not_billable", "tier_price": 0,
        })
        err = _error_result(nonprofit, "No JSON events in Poe response", text[:300])
        cache_put(nonprofit, err)
        return err

    # Use first event as the primary result
    result = _poe_result_to_full(events[0], nonprofit)
    result["_api_calls"] = 1

    tier, price = classify_lead_tier(result)
    status = result.get("status", "uncertain")

    # Charge fees
    if user_id and not is_admin:
        fee = get_research_fee_cents(total)
        bal = get_balance(user_id)
        if is_trial:
            if price > 0:
                total_charge = fee + price
                if bal >= total_charge:
                    charge_research_fee(user_id, 1, job_id, fee)
                    charge_lead_fee(user_id, tier, price, job_id, nonprofit)
                    progress_q.put({"type": "balance", "balance": get_balance(user_id)})
                else:
                    if balance_exhausted:
                        balance_exhausted[0] = True
                    result["_balance_exhausted"] = True
                    progress_q.put({"type": "balance_warning", "message": "Insufficient balance — stopping job."})
                    tier, price = "not_billable", 0
        else:
            total_charge = fee + (price if price > 0 else 0)
            if bal >= total_charge:
                charge_research_fee(user_id, 1, job_id, fee)
                if price > 0:
                    charge_lead_fee(user_id, tier, price, job_id, nonprofit)
                progress_q.put({"type": "balance", "balance": get_balance(user_id)})
            else:
                if balance_exhausted:
                    balance_exhausted[0] = True
                result["_balance_exhausted"] = True
                progress_q.put({"type": "balance_warning", "message": "Insufficient balance — stopping job."})
                tier, price = "not_billable", 0

    progress_q.put({
        "type": "result", "index": index, "total": total,
        "nonprofit": nonprofit, "status": status,
        "event_title": result.get("event_title", ""),
        "event_url": result.get("event_url", ""),
        "confidence": result.get("confidence_score", 0),
        "tier": tier, "tier_price": price,
    })
    cache_put(nonprofit, result)
    return result


def _run_job(
    nonprofits: List[str], job_id: str, progress_q: queue.Queue,
    user_id: Optional[int] = None, is_admin: bool = False, is_trial: bool = False,
    complete_only: bool = False,
):
    """Run research — one domain at a time, no batching, no async.
    Matches AUCTIONINTEL.APP_BOT.PY sequential pattern."""
    if len(nonprofits) > MAX_NONPROFITS:
        nonprofits = nonprofits[:MAX_NONPROFITS]

    total = len(nonprofits)
    progress_q.put({"type": "started", "total": total, "batches": 1})

    all_results: List[Dict[str, Any]] = []
    billing_summary = {"research_fees": 0, "lead_fees": {}, "total_charged": 0}
    balance_exhausted = [False]
    start = time.time()

    for idx, np_name in enumerate(nonprofits, start=1):
        # Stop if balance exhausted
        if balance_exhausted[0] and not is_admin:
            skipped = total - idx + 1
            progress_q.put({
                "type": "balance_warning",
                "message": f"Job stopped early — insufficient balance. {skipped} nonprofit(s) skipped.",
            })
            break

        # Send ETA with each domain
        elapsed_so_far = time.time() - start
        avg_per = elapsed_so_far / (idx - 1) if idx > 1 else 45  # estimate ~45s first call
        remaining_secs = int(avg_per * (total - idx + 1))
        progress_q.put({
            "type": "eta",
            "elapsed": int(elapsed_so_far),
            "remaining": remaining_secs,
            "index": idx,
            "total": total,
        })

        result = _research_one(
            np_name, idx, total, progress_q,
            user_id=user_id, job_id=job_id, is_admin=is_admin,
            is_trial=is_trial, balance_exhausted=balance_exhausted,
        )
        all_results.append(result)

        # Pause between domains (like the working script)
        if idx < total:
            time.sleep(PAUSE_BETWEEN_DOMAINS)

    elapsed = time.time() - start

    # Enrich missing address/phone from IRS database
    _enrich_from_irs(all_results)

    # Compute billing summary
    fee_cents = get_research_fee_cents(len(nonprofits))
    billing_summary["research_fees"] = len(nonprofits) * fee_cents
    tier_counts = {}
    for r in all_results:
        tier, price = classify_lead_tier(r)
        if price > 0:
            if tier not in tier_counts:
                tier_counts[tier] = {"count": 0, "price_each": price, "total": 0}
            tier_counts[tier]["count"] += 1
            tier_counts[tier]["total"] += price
            billing_summary["lead_fees"] = tier_counts
    billing_summary["total_charged"] = (
        billing_summary["research_fees"]
        + sum(t["total"] for t in tier_counts.values())
    )

    # Determine which results to save (billable only for non-admin)
    if is_admin:
        save_results = all_results
    else:
        save_results = [r for r in all_results if classify_lead_tier(r)[1] > 0]

    # If complete_only mode, further filter to only "full" tier leads
    if complete_only and not is_admin:
        save_results = [r for r in save_results if classify_lead_tier(r)[0] == "full"]
        # Recalculate billing to only include full-tier leads
        tier_counts = {}
        for r in save_results:
            tier, price = classify_lead_tier(r)
            if price > 0:
                if tier not in tier_counts:
                    tier_counts[tier] = {"count": 0, "price_each": price, "total": 0}
                tier_counts[tier]["count"] += 1
                tier_counts[tier]["total"] += price
        billing_summary["lead_fees"] = tier_counts
        billing_summary["total_charged"] = (
            billing_summary["research_fees"]
            + sum(t["total"] for t in tier_counts.values())
        )

    # Save results
    csv_file = os.path.join(RESULTS_DIR, f"{job_id}.csv")
    json_file = os.path.join(RESULTS_DIR, f"{job_id}.json")

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in save_results:
            row = {col: r.get(col, "") for col in CSV_COLUMNS}
            writer.writerow(row)

    output = {
        "meta": {
            "total_nonprofits": len(all_results),
            "processing_time_seconds": round(elapsed, 2),
            "model": f"Poe Bot: {POE_BOT_NAME}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "summary": {
            "found": sum(1 for r in all_results if r.get("status") == "found"),
            "3rdpty_found": sum(1 for r in all_results if r.get("status") == "3rdpty_found"),
            "not_found": sum(1 for r in all_results if r.get("status") == "not_found"),
            "uncertain": sum(1 for r in all_results if r.get("status") == "uncertain"),
        },
        "billing": billing_summary if not is_admin else None,
        "results": save_results,
    }
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    xlsx_file = os.path.join(RESULTS_DIR, f"{job_id}.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Auction Results"
    ws.append(CSV_COLUMNS)
    for r in save_results:
        ws.append([r.get(col, "") for col in CSV_COLUMNS])
    for col_cells in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col_cells), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 50)
    wb.save(xlsx_file)

    # Store result files in database (survives Railway redeploys)
    try:
        with open(csv_file, "rb") as f:
            save_result_file(job_id, "csv", f.read())
        with open(json_file, "rb") as f:
            save_result_file(job_id, "json", f.read())
        with open(xlsx_file, "rb") as f:
            save_result_file(job_id, "xlsx", f.read())
    except Exception as e:
        print(f"[WARN] Failed to save result files to DB: {e}", file=sys.stderr)

    # Remove checkpoint file now that final files are saved
    checkpoint_file = os.path.join(RESULTS_DIR, f"{job_id}_checkpoint.csv")
    if os.path.exists(checkpoint_file):
        os.remove(checkpoint_file)

    # Final event
    complete_event = {
        "type": "complete",
        "job_id": job_id,
        "elapsed": round(elapsed, 1),
        "summary": output["summary"],
        "csv_file": f"{job_id}.csv",
        "json_file": f"{job_id}.json",
        "xlsx_file": f"{job_id}.xlsx",
    }
    if not is_admin:
        complete_event["billing"] = billing_summary
        complete_event["balance"] = get_balance(user_id) if user_id else 0
        complete_event["billable_count"] = len(save_results)
    progress_q.put(complete_event)
    progress_q.put(None)  # sentinel

    jobs[job_id]["status"] = "complete"
    jobs[job_id]["results"] = output

    # Persist job completion to SQLite
    found_count = output["summary"].get("found", 0) + output["summary"].get("3rdpty_found", 0)
    billable_count = len(save_results) if not is_admin else found_count
    total_cost_cents = billing_summary["total_charged"] if not is_admin else 0
    complete_search_job(
        job_id,
        found_count=found_count,
        billable_count=billable_count,
        total_cost_cents=total_cost_cents,
        results_summary=json.dumps(output["summary"]),
    )

    # Send job completion email
    if user_id and not is_admin:
        user = get_user(user_id)
        if user:
            emails.send_job_complete(
                user["email"], job_id, len(nonprofits),
                found_count, billable_count, total_cost_cents,
            )


def _job_worker(
    nonprofits: List[str], job_id: str, progress_q: queue.Queue,
    user_id: Optional[int] = None, is_admin: bool = False, is_trial: bool = False,
    complete_only: bool = False,
):
    """Thread target that runs the job (synchronous — no async needed)."""
    try:
        _run_job(nonprofits, job_id, progress_q, user_id=user_id, is_admin=is_admin, is_trial=is_trial, complete_only=complete_only)
    except Exception as e:
        progress_q.put({"type": "error", "message": str(e)})
        progress_q.put(None)
        jobs[job_id]["status"] = "error"
        fail_search_job(job_id, str(e))
    finally:
        loop.close()


# ─── Routes: Auth ────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET"])
def register_page():
    return REGISTER_HTML

@app.route("/register", methods=["POST"])
def register_submit():
    ip = _get_client_ip()
    # 3 registration attempts per IP per hour
    if _rate_limit(f"register:{ip}", 3, 3600):
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Too many registration attempts. Try again later.</p>'), 429
    from db import is_work_email

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    phone = request.form.get("phone", "").strip()
    company = request.form.get("company", "").strip()
    promo_code = request.form.get("promo_code", "").strip()

    if not email or not password:
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">All fields are required</p>')
    if not is_work_email(email):
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Please use a work email address (no Gmail, Yahoo, etc.)</p>')
    if not company:
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Company name is required</p>')
    if not phone:
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Phone number is required</p>')
    if password != confirm:
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Passwords do not match</p>')
    if len(password) < 6:
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Password must be at least 6 characters</p>')

    # Check for duplicate email before creating
    if get_user_by_email(email):
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Email already registered</p>')

    try:
        user_id = create_user(email, password, phone=phone, company=company, promo_code=promo_code)
    except Exception as e:
        print(f"[REGISTER ERROR] {type(e).__name__}: {e}")
        return REGISTER_HTML.replace("<!-- error -->", '<p class="error">Registration failed. Please try again.</p>')

    session["user_id"] = user_id
    session["is_admin"] = False
    is_trial = promo_code.strip().upper() == "26AUCTION26"
    session["is_trial"] = is_trial

    try:
        emails.send_welcome(email, is_trial=is_trial)
    except Exception:
        pass

    return redirect(url_for("wallet_page"))


@app.route("/login", methods=["GET"])
def login_page():
    return LOGIN_HTML


@app.route("/login", methods=["POST"])
def login_submit():
    ip = _get_client_ip()
    # 5 login attempts per IP per 5 minutes
    if _rate_limit(f"login:{ip}", 5, 300):
        return LOGIN_HTML.replace("<!-- error -->", '<p class="error">Too many login attempts. Try again in a few minutes.</p>'), 429
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = authenticate(email, password)
    if user:
        session["user_id"] = user["id"]
        session["is_admin"] = user["is_admin"]
        session["is_trial"] = user.get("is_trial", False)
        return redirect(url_for("database_page"))
    return LOGIN_HTML.replace("<!-- error -->", '<p class="error">Invalid email or password</p>')


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ─── Routes: Password Reset ──────────────────────────────────────────────────

@app.route("/forgot-password", methods=["GET"])
def forgot_password_page():
    return FORGOT_PASSWORD_HTML


@app.route("/forgot-password", methods=["POST"])
def forgot_password_submit():
    ip = _get_client_ip()
    # 3 reset attempts per IP per 15 minutes
    if _rate_limit(f"reset:{ip}", 3, 900):
        return FORGOT_PASSWORD_HTML.replace("<!-- error -->", '<p class="error">Too many reset attempts. Try again later.</p>'), 429
    email = request.form.get("email", "").strip().lower()
    if email:
        user = get_user_by_email(email)
        if user:
            token = create_reset_token(user["id"])
            reset_url = f"{DOMAIN}/reset-password?token={token}"
            emails.send_password_reset(email, reset_url)
    # Always show same message to prevent email enumeration
    return FORGOT_PASSWORD_HTML.replace(
        "<!-- message -->",
        '<p class="success">If an account exists with that email, we sent a password reset link. Check your inbox.</p>'
    )


@app.route("/reset-password", methods=["GET"])
def reset_password_page():
    token = request.args.get("token", "")
    user_id = validate_reset_token(token)
    if not user_id:
        return FORGOT_PASSWORD_HTML.replace(
            "<!-- message -->",
            '<p class="error">This reset link is invalid or has expired. Please request a new one.</p>'
        )
    return RESET_PASSWORD_HTML.replace("{{TOKEN}}", token)


@app.route("/reset-password", methods=["POST"])
def reset_password_submit():
    token = request.form.get("token", "")
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    if len(password) < 6:
        return RESET_PASSWORD_HTML.replace("{{TOKEN}}", token).replace(
            "<!-- error -->", '<p class="error">Password must be at least 6 characters</p>'
        )
    if password != confirm:
        return RESET_PASSWORD_HTML.replace("{{TOKEN}}", token).replace(
            "<!-- error -->", '<p class="error">Passwords do not match</p>'
        )

    user_id = validate_reset_token(token)
    if not user_id:
        return FORGOT_PASSWORD_HTML.replace(
            "<!-- message -->",
            '<p class="error">This reset link is invalid or has expired. Please request a new one.</p>'
        )

    update_password(user_id, password)
    consume_reset_token(token)
    return LOGIN_HTML.replace(
        "<!-- error -->",
        '<p class="success">Password reset successfully. Please log in with your new password.</p>'
    )


# ─── Routes: Wallet ──────────────────────────────────────────────────────────

@app.route("/wallet")
@login_required
def wallet_page():
    user_id = session["user_id"]
    balance = get_balance(user_id)
    txns = get_transactions(user_id, limit=50)
    user = _current_user()

    txn_rows = ""
    for t in txns:
        amt = t["amount_cents"]
        amt_str = f"+${amt/100:.2f}" if amt > 0 else f"-${abs(amt)/100:.2f}"
        color = "#4ade80" if amt > 0 else "#f87171"
        txn_rows += f'<tr><td>{t["created_at"]}</td><td>{t["type"]}</td><td style="color:{color}">{amt_str}</td><td>{t["description"] or ""}</td></tr>'

    html = WALLET_HTML.replace("{{BALANCE}}", f"${balance/100:.2f}")
    html = html.replace("{{STRIPE_PK}}", STRIPE_PUBLISHABLE_KEY)
    html = html.replace("{{TXN_ROWS}}", txn_rows)
    html = _inject_sidebar(html, "wallet")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    return _inject_nav_badge(html)


@app.route("/api/wallet/topup", methods=["POST"])
@login_required
def wallet_topup():
    data = request.get_json()
    amount_dollars = data.get("amount", 0)

    try:
        amount_dollars = float(amount_dollars)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount"}), 400

    if amount_dollars < 250 or amount_dollars > 9999:
        return jsonify({"error": "Amount must be between $250 and $9,999"}), 400

    amount_cents = int(amount_dollars * 100)
    user_id = session["user_id"]

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            metadata={"user_id": str(user_id)},
            description="AUCTIONFINDER Wallet Top-Up",
        )
        return jsonify({"client_secret": intent.client_secret})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except (ValueError, stripe.error.SignatureVerificationError):
            return "Invalid signature", 400
    else:
        try:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
        except Exception:
            return "Invalid payload", 400

    if event["type"] == "payment_intent.succeeded":
        intent = event["data"]["object"]
        user_id = int(intent["metadata"].get("user_id", 0))
        amount_cents = intent["amount"]
        intent_id = intent["id"]

        if user_id:
            credited = add_funds(user_id, amount_cents,
                                 f"Stripe payment: {intent_id[:20]}",
                                 stripe_intent_id=intent_id)
            if credited:
                user = get_user(user_id)
                if user:
                    new_balance = get_balance(user_id)
                    emails.send_funds_receipt(user["email"], amount_cents, new_balance)
                print(f"[STRIPE] Credited ${amount_cents/100:.2f} to user {user_id} (intent: {intent_id})")
            else:
                print(f"[STRIPE] Duplicate intent {intent_id}, skipping")

    return "ok", 200


# ─── Routes: Profile ─────────────────────────────────────────────────────────

@app.route("/profile")
@login_required
def profile_page():
    user = get_user_full(session["user_id"])
    balance = get_balance(session["user_id"])
    summary = get_spending_summary(session["user_id"])

    html = PROFILE_HTML
    html = _inject_sidebar(html, "profile")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("{{CREATED_AT}}", user["created_at"] or "Unknown" if user else "Unknown")
    html = html.replace("{{ACCOUNT_TYPE}}", "Administrator" if user and user["is_admin"] else "Standard")
    html = html.replace("{{BALANCE}}", f"${balance/100:.2f}")
    html = html.replace("{{TOTAL_SPENT}}", f"${summary['total_spent']/100:.2f}")
    html = html.replace("{{TOTAL_TOPUPS}}", f"${summary['total_topups']/100:.2f}")
    html = html.replace("{{JOB_COUNT}}", str(summary["job_count"]))
    return _inject_nav_badge(html)


@app.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    user = get_user(session["user_id"])
    if not user:
        return redirect(url_for("profile_page"))

    # Verify current password
    auth = authenticate(user["email"], current)
    if not auth:
        return redirect(url_for("profile_page") + "?error=current")
    if len(new_pw) < 6:
        return redirect(url_for("profile_page") + "?error=length")
    if new_pw != confirm:
        return redirect(url_for("profile_page") + "?error=match")

    update_password(session["user_id"], new_pw)
    return redirect(url_for("profile_page") + "?success=1")


# ─── Routes: Billing ─────────────────────────────────────────────────────────

@app.route("/billing")
@login_required
def billing_page():
    user_id = session["user_id"]
    user = _current_user()
    balance = get_balance(user_id)
    summary = get_spending_summary(user_id)
    persistent_jobs = get_user_jobs(user_id, limit=50)
    txns = get_transactions(user_id, limit=100)

    job_rows = ""
    for j in persistent_jobs:
        cost = j["total_cost_cents"] or 0
        status_color = "#4ade80" if j["status"] == "complete" else "#f87171" if j["status"] == "error" else "#eab308"
        dl_links = ""
        if j["status"] == "complete":
            jid = j["job_id"]
            dl_links = (
                f'<a href="/api/download/{jid}/csv" style="color:#eab308;text-decoration:none;margin-right:6px;">CSV</a>'
                f'<a href="/api/download/{jid}/json" style="color:#7c3aed;text-decoration:none;margin-right:6px;">JSON</a>'
                f'<a href="/api/download/{jid}/xlsx" style="color:#60a5fa;text-decoration:none;">XLSX</a>'
            )
        job_rows += (
            f'<tr><td>{j["created_at"] or ""}</td>'
            f'<td style="color:{status_color}">{j["status"]}</td>'
            f'<td>{j["nonprofit_count"]}</td>'
            f'<td>{j["found_count"] or 0}</td>'
            f'<td>{j["billable_count"] or 0}</td>'
            f'<td style="font-weight:700;color:#eab308;">${cost/100:.2f}</td>'
            f'<td>{dl_links}</td></tr>'
        )

    txn_rows = ""
    for t in txns:
        amt = t["amount_cents"]
        amt_str = f"+${amt/100:.2f}" if amt > 0 else f"-${abs(amt)/100:.2f}"
        color = "#4ade80" if amt > 0 else "#f87171"
        txn_rows += (
            f'<tr><td>{t["created_at"]}</td><td>{t["type"]}</td>'
            f'<td style="color:{color}">{amt_str}</td>'
            f'<td>{t["description"] or ""}</td>'
            f'<td style="font-family:monospace;font-size:11px;">{t["job_id"] or ""}</td></tr>'
        )

    html = _inject_sidebar(BILLING_HTML, "billing")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("{{BALANCE}}", f"${balance/100:.2f}")
    html = html.replace("{{TOTAL_SPENT}}", f"${summary['total_spent']/100:.2f}")
    html = html.replace("{{RESEARCH_FEES}}", f"${summary['research_fees']/100:.2f}")
    html = html.replace("{{LEAD_FEES}}", f"${summary['lead_fees']/100:.2f}")
    html = html.replace("{{TOTAL_TOPUPS}}", f"${summary['total_topups']/100:.2f}")
    html = html.replace("{{JOB_COUNT}}", str(summary["job_count"]))
    html = html.replace("{{JOB_ROWS}}", job_rows)
    html = html.replace("{{TXN_ROWS}}", txn_rows)
    return _inject_nav_badge(html)


# ─── Routes: Results ─────────────────────────────────────────────────────────

@app.route("/results")
@login_required
def results_page():
    user_id = session["user_id"]
    user = _current_user()
    past_jobs = get_user_jobs(user_id, limit=50)

    job_cards = ""
    for j in past_jobs:
        jid = j["job_id"]
        cost = j["total_cost_cents"] or 0
        status = j["status"]
        status_color = "#4ade80" if status == "complete" else "#f87171" if status == "error" else "#eab308"
        status_label = status.upper()

        dl_links = ""
        if status == "complete":
            dl_links = (
                f'<div class="dl-btns">'
                f'<a href="/api/download/{jid}/csv" class="dl-btn csv">CSV</a>'
                f'<a href="/api/download/{jid}/json" class="dl-btn json">JSON</a>'
                f'<a href="/api/download/{jid}/xlsx" class="dl-btn xlsx">XLSX</a>'
                f'</div>'
            )

        job_cards += (
            f'<div class="job-card">'
            f'<div class="job-header">'
            f'<span class="job-date">{j["created_at"] or ""}</span>'
            f'<span class="job-status" style="color:{status_color}">{status_label}</span>'
            f'</div>'
            f'<div class="job-stats">'
            f'<div class="js"><span class="jn">{j["nonprofit_count"]}</span><span class="jl">Searched</span></div>'
            f'<div class="js"><span class="jn" style="color:#4ade80">{j["found_count"] or 0}</span><span class="jl">Found</span></div>'
            f'<div class="js"><span class="jn" style="color:#eab308">{j["billable_count"] or 0}</span><span class="jl">Billable</span></div>'
            f'<div class="js"><span class="jn" style="color:#f87171">${cost/100:.2f}</span><span class="jl">Cost</span></div>'
            f'</div>'
            f'{dl_links}'
            f'</div>'
        )

    if not job_cards:
        job_cards = '<div class="empty-state"><p>No search results yet.</p><p>Run your first search from <a href="/search" style="color:#eab308">Auction Search</a>.</p></div>'

    html = _inject_sidebar(RESULTS_HTML, "results")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("{{JOB_CARDS}}", job_cards)
    return _inject_nav_badge(html)


# ─── Routes: Search ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Show landing page for non-authenticated users
    if not session.get("user_id"):
        return LANDING_HTML
    user = _current_user()
    if not user:
        session.clear()
        return LANDING_HTML
    is_admin = user.get("is_admin", False)
    balance = get_balance(session["user_id"]) if not is_admin else 0

    html = _inject_sidebar(INDEX_HTML, "search")
    html = html.replace("{{IS_ADMIN}}", "true" if is_admin else "false")
    html = html.replace("{{BALANCE_CENTS}}", str(balance))
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    return _inject_nav_badge(html)


@app.route("/api/search", methods=["POST"])
@login_required
def start_search():
    ip = _get_client_ip()
    user_id = session.get("user_id", "anon")
    # 10 search jobs per user per hour
    if _rate_limit(f"search:{user_id}", 10, 3600):
        return jsonify({"error": "Rate limit: max 10 searches per hour. Please wait."}), 429
    # 20 search jobs per IP per hour (catches abuse across accounts)
    if _rate_limit(f"search_ip:{ip}", 20, 3600):
        return jsonify({"error": "Rate limit exceeded. Please try again later."}), 429
    data = request.get_json()
    raw_input = data.get("nonprofits", "")
    complete_only = bool(data.get("complete_only", False))
    nonprofits = parse_input(raw_input)

    if not nonprofits:
        return jsonify({"error": "No nonprofits provided"}), 400

    if len(nonprofits) > MAX_NONPROFITS:
        return jsonify({"error": f"Maximum {MAX_NONPROFITS} nonprofits allowed"}), 400

    user_id = session["user_id"]
    is_admin = session.get("is_admin", False)
    is_trial = session.get("is_trial", False)

    # Pre-search balance check for non-admin
    # Estimates TOTAL cost including research fees + estimated lead fees
    # Conservative hit rate: 55%, avg lead price: $1.40 (most leads are "full" at $1.50)
    ESTIMATED_HIT_RATE = 0.55
    ESTIMATED_AVG_LEAD_CENTS = 140  # weighted average across tiers

    if not is_admin:
        balance = get_balance(user_id)
        count = len(nonprofits)
        fee_per = get_research_fee_cents(count)

        if is_trial:
            # Trial users only pay for real results — estimate lead fees only
            estimated_lead_cost = int(count * ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
            if balance <= 0:
                return jsonify({
                    "error": "Trial balance exhausted. Top up your wallet to continue searching."
                }), 402
            if balance < estimated_lead_cost:
                cost_per_np = int(ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
                max_affordable = max(1, balance // cost_per_np) if cost_per_np > 0 else count
                return jsonify({
                    "error": f"Your balance of ${balance/100:.2f} can cover approximately {max_affordable} nonprofits. Please reduce your list or add funds.",
                    "affordable_count": max_affordable,
                    "balance_cents": balance,
                    "estimated_cost_cents": estimated_lead_cost,
                }), 402
        else:
            research_cost = count * fee_per
            estimated_lead_cost = int(count * ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
            estimated_total = research_cost + estimated_lead_cost

            if balance < estimated_total:
                cost_per_np = fee_per + int(ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
                max_affordable = max(1, balance // cost_per_np) if cost_per_np > 0 else count
                return jsonify({
                    "error": f"Your balance of ${balance/100:.2f} can cover approximately {max_affordable} nonprofits (estimated cost: ${estimated_total/100:.2f}). Please reduce your list or add funds.",
                    "affordable_count": max_affordable,
                    "balance_cents": balance,
                    "estimated_cost_cents": estimated_total,
                }), 402

    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
    progress_q: queue.Queue = queue.Queue()

    jobs[job_id] = {
        "status": "running",
        "nonprofits": nonprofits,
        "progress_queue": progress_q,
        "results": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
    }

    # Persist job to SQLite
    create_search_job(user_id, job_id, len(nonprofits))

    thread = threading.Thread(
        target=_job_worker,
        args=(nonprofits, job_id, progress_q),
        kwargs={"user_id": user_id, "is_admin": is_admin, "is_trial": is_trial, "complete_only": complete_only},
        daemon=True,
    )
    thread.start()

    # Build estimated cost for frontend display
    _count = len(nonprofits)
    _fee_per = get_research_fee_cents(_count)
    _research_total = _count * _fee_per
    _lead_est = int(_count * ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
    _est_total = _research_total + _lead_est

    return jsonify({
        "job_id": job_id,
        "total": _count,
        "research_fee_each": _fee_per,
        "estimated_cost_cents": _est_total if not is_admin else 0,
    })


@app.route("/api/progress/<job_id>")
@login_required
def stream_progress(job_id):
    if job_id not in jobs:
        return Response("Job not found", status=404)

    progress_q = jobs[job_id]["progress_queue"]

    def generate():
        while True:
            try:
                event = progress_q.get(timeout=120)
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/download/<job_id>/<fmt>")
@login_required
def download_result(job_id, fmt):
    if fmt not in ("csv", "json", "xlsx"):
        return Response("Invalid format", status=400)

    # Try disk first, then fall back to database (survives Railway redeploys)
    filepath = os.path.join(RESULTS_DIR, f"{job_id}.{fmt}")
    if os.path.exists(filepath):
        return send_file(
            filepath,
            as_attachment=True,
            download_name=f"auction_results_{job_id}.{fmt}",
        )

    # File not on disk — try database
    content = get_result_file(job_id, fmt)
    if content is None:
        return Response("File not found", status=404)

    mime_types = {
        "csv": "text/csv",
        "json": "application/json",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return Response(
        content,
        mimetype=mime_types.get(fmt, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="auction_results_{job_id}.{fmt}"'
        },
    )


@app.route("/api/results/<job_id>")
@login_required
def view_results(job_id):
    if job_id not in jobs or jobs[job_id]["status"] != "complete":
        return jsonify({"error": "Job not found or not complete"}), 404
    return jsonify(jobs[job_id]["results"])


# ─── Exclusive Leads ─────────────────────────────────────────────────────────

@app.route("/api/exclusive", methods=["POST"])
@login_required
def make_exclusive():
    data = request.get_json()
    job_id = data.get("job_id", "")
    nonprofit_name = data.get("nonprofit_name", "")
    event_title = data.get("event_title", "")
    event_url = data.get("event_url", "")

    if not event_url or not event_title:
        return jsonify({"error": "Missing event details"}), 400

    user_id = session["user_id"]

    # Check if already exclusive
    owner = is_lead_exclusive(event_url, event_title)
    if owner:
        if owner == user_id:
            return jsonify({"error": "You already own this exclusive lead"}), 409
        return jsonify({"error": "This event lead is already exclusive"}), 409

    # Check balance
    balance = get_balance(user_id)
    if balance < EXCLUSIVE_LEAD_PRICE_CENTS:
        return jsonify({"error": f"Insufficient balance. Need ${EXCLUSIVE_LEAD_PRICE_CENTS/100:.2f}, have ${balance/100:.2f}."}), 402

    success = purchase_exclusive_lead(user_id, job_id, nonprofit_name, event_title, event_url)
    if not success:
        return jsonify({"error": "Failed to purchase exclusive lead"}), 500

    return jsonify({
        "success": True,
        "message": "This event lead has been marked exclusive and will not be included in any future search results sold to other customers.",
        "balance": get_balance(user_id),
    })


# ─── IRS Database Search ─────────────────────────────────────────────────────

@app.route("/database")
@login_required
def database_page():
    user = _current_user()
    html = _inject_sidebar(DATABASE_HTML, "database")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("{{IS_ADMIN}}", "true" if user and user.get("is_admin") else "false")
    return _inject_nav_badge(html)


REGIONS = {
    "northeast": ["CT", "DE", "DC", "ME", "MD", "MA", "NH", "NJ", "NY", "PA", "RI", "VT"],
    "southeast": ["AL", "AR", "FL", "GA", "KY", "LA", "MS", "NC", "SC", "TN", "VA", "WV"],
    "midwest": ["IL", "IN", "IA", "KS", "MI", "MN", "MO", "NE", "ND", "OH", "SD", "WI"],
    "southwest": ["AZ", "NM", "OK", "TX"],
    "west": ["AK", "CA", "CO", "HI", "ID", "MT", "NV", "OR", "UT", "WA", "WY"],
}

AMOUNT_RANGES = {
    "1-100k": (1, 100000),
    "100k-500k": (100000, 500000),
    "500k-1m": (500000, 1000000),
    "1m-2m": (1000000, 2000000),
    "2m-5m": (2000000, 5000000),
    "5m+": (5000000, None),
}


def _add_amount_filter(conditions, params, field, range_key):
    """Add a min/max amount filter from a range key."""
    if not range_key or range_key not in AMOUNT_RANGES:
        return
    lo, hi = AMOUNT_RANGES[range_key]
    conditions.append(f"{field} >= %s")
    params.append(lo)
    if hi is not None:
        conditions.append(f"{field} <= %s")
        params.append(hi)


@app.route("/api/irs/search", methods=["POST"])
@login_required
def irs_search():
    data = request.get_json()
    limit = min(int(data.get("limit", 100)), 10000)

    conditions = []
    params = []

    name = data.get("name", "").strip()
    if name:
        conditions.append("OrganizationName LIKE %s")
        params.append(f"{name}%")

    city = data.get("city", "").strip()
    if city:
        conditions.append("PhysicalCity LIKE %s")
        params.append(f"{city}%")

    state = data.get("state", "").strip()
    region = data.get("region", "").strip()
    if state:
        conditions.append("PhysicalState = %s")
        params.append(state.upper())
    elif region:
        conditions.append("LOWER(region5) = LOWER(%s)")
        params.append(region)

    event_keyword = data.get("event_keyword", "").strip()
    if event_keyword:
        conditions.append("(Event1Name LIKE %s OR Event2Name LIKE %s)")
        params.append(f"%{event_keyword}%")
        params.append(f"%{event_keyword}%")

    mission_keyword = data.get("mission_keyword", "").strip()
    if mission_keyword:
        conditions.append("MissionDescriptionShort LIKE %s")
        params.append(f"%{mission_keyword}%")

    primary_event_type = data.get("primary_event_type", "").strip()
    if primary_event_type:
        conditions.append("PrimaryEventType = %s")
        params.append(primary_event_type.upper())

    prospect_tier = data.get("prospect_tier", "").strip()
    if prospect_tier:
        conditions.append("ProspectTier = %s")
        params.append(prospect_tier)

    event_type_keywords = {
        "has_auction": "AUCTION", "has_gala": "GALA", "has_raffle": "RAFFLE",
        "has_ball": "BALL", "has_dinner": "DINNER", "has_benefit": "BENEFIT",
        "has_tournament": "TOURNAMENT", "has_golf": "GOLF", "has_fundraiser": "FUNDRAISER",
        "has_festival": "FESTIVAL", "has_run": "RUN", "has_art": "ART",
        "has_casino": "CASINO", "has_show": "SHOW", "has_night": "NIGHT",
    }
    active_keywords = [kw for key, kw in event_type_keywords.items() if data.get(key)]
    if active_keywords:
        kw_parts = []
        for kw in active_keywords:
            kw_parts.append("(Event1Keyword = %s OR Event2Keyword = %s)")
            params.append(kw)
            params.append(kw)
        conditions.append("(" + " OR ".join(kw_parts) + ")")

    if data.get("has_website"):
        conditions.append("Website IS NOT NULL AND Website != ''")

    amount_fields = {
        "total_revenue": "TotalRevenue",
        "gross_receipts": "GrossReceipts",
        "net_income": "NetIncome",
        "total_assets": "TotalAssets",
        "contributions": "ContributionsReceived",
        "program_revenue": "ProgramServiceRevenue",
        "fundraising_income": "FundraisingGrossIncome",
        "fundraising_expenses": "FundraisingDirectExpenses",
        "event1_receipts": "Event1GrossReceipts",
        "event1_contributions": "Event1CharitableContributions",
        "event1_revenue": "Event1GrossRevenue",
        "event1_rent": "Event1RentCosts",
        "event1_food": "Event1FoodBeverage",
        "event1_expenses": "Event1OtherExpenses",
        "event1_net": "Event1NetIncome",
        "event2_receipts": "Event2GrossReceipts",
        "event2_contributions": "Event2CharitableContributions",
        "event2_revenue": "Event2GrossRevenue",
    }
    for key, col in amount_fields.items():
        _add_amount_filter(conditions, params, col, data.get(key, ""))

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"""
        SELECT
            ein AS "EIN", organizationname AS "OrganizationName", website AS "Website",
            physicaladdress AS "PhysicalAddress", physicalcity AS "PhysicalCity",
            physicalstate AS "PhysicalState", physicalzip AS "PhysicalZIP",
            businessofficerphone AS "BusinessOfficerPhone", principalofficername AS "PrincipalOfficerName",
            totalrevenue AS "TotalRevenue", grossreceipts AS "GrossReceipts",
            netincome AS "NetIncome", totalassets AS "TotalAssets",
            contributionsreceived AS "ContributionsReceived",
            programservicerevenue AS "ProgramServiceRevenue",
            fundraisinggrossincome AS "FundraisingGrossIncome",
            fundraisingdirectexpenses AS "FundraisingDirectExpenses",
            event1name AS "Event1Name", event1grossreceipts AS "Event1GrossReceipts",
            event1grossrevenue AS "Event1GrossRevenue", event1netincome AS "Event1NetIncome",
            event2name AS "Event2Name", event2grossreceipts AS "Event2GrossReceipts",
            event2grossrevenue AS "Event2GrossRevenue",
            event1keyword AS "Event1Keyword", event2keyword AS "Event2Keyword",
            primaryeventtype AS "PrimaryEventType", prospecttier AS "ProspectTier",
            region5 AS "Region5", missiondescriptionshort AS "MissionDescriptionShort"
        FROM tax_year_2019_search
        WHERE {where}
        ORDER BY totalrevenue DESC
        LIMIT {limit}
    """

    try:
        conn = get_irs_db()
        cursor = conn.cursor()
        cursor.execute(query, tuple(params))
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"count": len(rows), "results": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","VI","GU","AS","MP",
]

@app.route("/api/irs/states")
@login_required
def irs_states():
    return jsonify(US_STATES)


@app.route("/api/test-poe")
@login_required
def test_poe():
    """Debug endpoint: test a single Poe bot call with full diagnostics."""
    user = _current_user()
    if not user or not user.get("is_admin"):
        return jsonify({"error": "Admin only"}), 403
    try:
        import traceback
        import fastapi_poe as fp
        poe_key = os.environ.get("POE_API_KEY", "")
        bot_name = POE_BOT_NAME
        domain = request.args.get("domain", "redcross.org")
        message = fp.ProtocolMessage(role="user", content=domain)
        chunks = []
        for partial in fp.get_bot_response_sync(
            messages=[message],
            bot_name=bot_name,
            api_key=poe_key,
        ):
            chunks.append(repr(partial.text))
        full = "".join(c.strip("'\"") for c in chunks)
        return jsonify({
            "status": "ok", "bot": bot_name, "domain": domain,
            "api_key_set": bool(poe_key), "api_key_prefix": poe_key[:8] + "..." if poe_key else "MISSING",
            "chunk_count": len(chunks), "response_length": len(full),
            "preview": full[:500],
        })
    except Exception as e:
        import traceback
        return jsonify({"status": "error", "bot": POE_BOT_NAME, "error": str(e), "traceback": traceback.format_exc()}), 500


# ─── Routes: Tools ───────────────────────────────────────────────────────────

@app.route("/tools/merge")
@login_required
def tools_merge_page():
    user = _current_user()
    html = _inject_sidebar(MERGE_TOOL_HTML, "tools")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    return _inject_nav_badge(html)


@app.route("/tools/analyzer")
@login_required
def tools_analyzer_page():
    if not _is_admin():
        return redirect("/")
    user = _current_user()
    html = _inject_sidebar(ANALYZER_TOOL_HTML, "analyzer")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    return _inject_nav_badge(html)


# ─── Routes: Getting Started ─────────────────────────────────────────────────

@app.route("/getting-started")
@login_required
def getting_started_page():
    user = _current_user()
    html = _inject_sidebar(GETTING_STARTED_HTML, "getting-started")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    return _inject_nav_badge(html)


# ─── Routes: Support / Tickets ───────────────────────────────────────────────

@app.route("/support")
@login_required
def support_page():
    user = _current_user()
    is_admin = user and user.get("is_admin", False)

    if is_admin:
        tickets = get_all_tickets()
    else:
        tickets = get_tickets_for_user(session["user_id"])

    ticket_rows = ""
    for t in tickets:
        status = t["status"]
        sc = {"open": "#4ade80", "pending": "#eab308", "urgent": "#f87171", "resolved": "#a3a3a3"}.get(status, "#a3a3a3")
        unread_badge = f' <span style="background:#f87171;color:#fff;border-radius:50%;padding:1px 6px;font-size:10px;font-weight:700;">{t["unread"]}</span>' if t.get("unread", 0) > 0 else ""
        user_col = f'<td>{html_escape(t.get("user_email", ""))}</td>' if is_admin else ""
        ticket_rows += (
            f'<tr>'
            f'<td><a href="/support/{t["id"]}" style="color:#eab308;text-decoration:none;">#{t["id"]}</a></td>'
            f'<td><a href="/support/{t["id"]}" style="color:#f5f5f5;text-decoration:none;">{html_escape(t["subject"])}{unread_badge}</a></td>'
            f'{user_col}'
            f'<td style="color:{sc};font-weight:600;">{status.upper()}</td>'
            f'<td>{t["updated_at"]}</td>'
            f'</tr>'
        )

    user_col_header = "<th>User</th>" if is_admin else ""
    html = _inject_sidebar(SUPPORT_HTML, "support")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("{{TICKET_ROWS}}", ticket_rows)
    html = html.replace("{{USER_COL_HEADER}}", user_col_header)
    return _inject_nav_badge(html)


@app.route("/support/new", methods=["GET", "POST"])
@login_required
def support_new():
    user = _current_user()

    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()
        priority = request.form.get("priority", "normal").strip()

        if not subject or not message:
            html = _inject_sidebar(SUPPORT_NEW_HTML, "support")
            html = html.replace("{{EMAIL}}", user["email"] if user else "")
            html = html.replace("<!-- error -->", '<p style="color:#f87171;margin-bottom:16px;">Subject and message are required.</p>')
            return _inject_nav_badge(html)

        ticket_id = create_ticket(session["user_id"], subject, message, priority)

        try:
            emails.send_ticket_created(ticket_id, subject, user["email"], message)
        except Exception:
            pass

        return redirect(url_for("support_ticket", ticket_id=ticket_id))

    html = _inject_sidebar(SUPPORT_NEW_HTML, "support")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("<!-- error -->", "")
    return _inject_nav_badge(html)


@app.route("/support/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def support_ticket(ticket_id):
    user = _current_user()
    is_admin = user and user.get("is_admin", False)
    ticket = get_ticket(ticket_id)

    if not ticket:
        return redirect(url_for("support_page"))

    # Non-admin users can only view their own tickets
    if not is_admin and ticket["user_id"] != session["user_id"]:
        return redirect(url_for("support_page"))

    if request.method == "POST":
        reply = request.form.get("message", "").strip()
        if reply:
            add_ticket_message(ticket_id, session["user_id"], reply, is_admin=is_admin)
            if is_admin:
                try:
                    emails.send_ticket_reply_to_user(ticket["user_email"], ticket_id, ticket["subject"], reply)
                except Exception:
                    pass
            return redirect(url_for("support_ticket", ticket_id=ticket_id))

    # Mark messages as read
    if is_admin:
        mark_messages_read_by_admin(ticket_id)
    else:
        mark_messages_read_by_user(ticket_id)

    messages = get_ticket_messages(ticket_id)
    msg_html = ""
    for m in messages:
        is_sender_admin = m["is_admin"]
        align = "right" if is_sender_admin else "left"
        bg = "#1a1500" if is_sender_admin else "#1a1a1a"
        border_color = "#eab308" if is_sender_admin else "#333"
        label = "Admin" if is_sender_admin else html_escape(m["sender_email"])
        msg_html += (
            f'<div style="display:flex;justify-content:flex-{align};margin-bottom:12px;">'
            f'<div style="max-width:70%;background:{bg};border:1px solid {border_color};border-radius:12px;padding:12px 16px;">'
            f'<div style="font-size:11px;color:#a3a3a3;margin-bottom:4px;">{label} &middot; {m["created_at"]}</div>'
            f'<div style="font-size:14px;color:#e0e0e0;white-space:pre-wrap;">{html_escape(m["message"])}</div>'
            f'</div></div>'
        )

    status = ticket["status"]
    sc = {"open": "#4ade80", "pending": "#eab308", "urgent": "#f87171", "resolved": "#a3a3a3"}.get(status, "#a3a3a3")

    status_form = ""
    if is_admin:
        status_options = ""
        for s in ["open", "pending", "urgent", "resolved"]:
            sel = ' selected' if s == status else ''
            status_options += f'<option value="{s}"{sel}>{s.upper()}</option>'
        status_form = (
            f'<form method="POST" action="/support/{ticket_id}/status" style="display:flex;gap:8px;align-items:center;">'
            f'<select name="status" style="padding:6px 10px;background:#000;border:1px solid #333;border-radius:6px;color:#f5f5f5;font-family:inherit;font-size:12px;">{status_options}</select>'
            f'<button type="submit" style="padding:6px 14px;background:#262626;color:#a3a3a3;border:1px solid #333;border-radius:6px;cursor:pointer;font-family:inherit;font-size:12px;">Update</button>'
            f'</form>'
        )

    html = _inject_sidebar(SUPPORT_TICKET_HTML, "support")
    html = html.replace("{{EMAIL}}", user["email"] if user else "")
    html = html.replace("{{TICKET_ID}}", str(ticket_id))
    html = html.replace("{{SUBJECT}}", html_escape(ticket["subject"]))
    html = html.replace("{{STATUS}}", status.upper())
    html = html.replace("{{STATUS_COLOR}}", sc)
    html = html.replace("{{MESSAGES}}", msg_html)
    html = html.replace("{{STATUS_FORM}}", status_form)
    return _inject_nav_badge(html)


@app.route("/support/<int:ticket_id>/status", methods=["POST"])
@login_required
def support_update_status(ticket_id):
    if not _is_admin():
        return redirect(url_for("support_page"))
    new_status = request.form.get("status", "")
    update_ticket_status(ticket_id, new_status)
    return redirect(url_for("support_ticket", ticket_id=ticket_id))


# ─── HTML Templates ──────────────────────────────────────────────────────────

_BASE_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #000000; color: #f5f5f5; }
  .auth-box { background: #000000; border: 1px solid #262626; border-radius: 12px; padding: 40px; width: 420px; text-align: center; }
  .auth-box p.sub { color: #a3a3a3; margin-bottom: 24px; font-size: 14px; }
  .auth-box input { width: 100%; padding: 12px 16px; background: #000000; border: 1px solid #333333; border-radius: 8px; color: #f5f5f5; font-size: 16px; margin-bottom: 12px; outline: none; }
  .auth-box input:focus { border-color: #eab308; }
  .auth-box button { width: 100%; padding: 12px; background: #ffd900; color: #000000; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; font-weight: 600; margin-top: 4px; }
  .auth-box button:hover { background: #ca8a04; }
  .error { color: #f87171; margin-bottom: 16px; font-size: 14px; }
  .success { color: #4ade80; margin-bottom: 16px; font-size: 14px; }
  .auth-box .link { color: #a3a3a3; font-size: 13px; margin-top: 16px; }
  .auth-box .link a { color: #eab308; text-decoration: none; }
  .auth-box .link a:hover { text-decoration: underline; }
"""

REGISTER_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Register</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
  .auth-box {{ width: 460px; }}
  .promo-section {{ background: #0a0a0a; border: 1px dashed #333; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }}
  .promo-section label {{ font-size: 12px; color: #eab308; display: block; margin-bottom: 6px; font-weight: 600; }}
  .promo-section input {{ margin-bottom: 0; }}
  .promo-hint {{ font-size: 11px; color: #737373; margin-top: 6px; }}
  .field-hint {{ font-size: 11px; color: #525252; margin-top: -8px; margin-bottom: 12px; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/register">
  <img src="/static/logo_dark.png" alt="Auction Intel" style="height:48px;margin-bottom:16px;">
  <p class="sub">Create your account</p>
  <!-- error -->
  <input type="text" name="company" placeholder="Company name" required>
  <input type="email" name="email" placeholder="Work email address" autofocus required>
  <p class="field-hint">Work email required (no Gmail, Yahoo, etc.)</p>
  <input type="tel" name="phone" placeholder="Phone number" required>
  <input type="password" name="password" placeholder="Password (min 6 chars)" required>
  <input type="password" name="confirm" placeholder="Confirm password" required>
  <div class="promo-section">
    <label>Have a promo code?</label>
    <input type="text" name="promo_code" placeholder="Enter promo code for free trial" style="text-transform:uppercase;">
    <p class="promo-hint">Have a promo code? Enter it to activate your trial.</p>
  </div>
  <button type="submit">Create Account</button>
  <p class="link">Already have an account? <a href="/login">Log in</a></p>
</form>
<script>
(function() {{
  var params = new URLSearchParams(window.location.search);
  var promo = params.get('promo');
  if (promo) {{
    var input = document.querySelector('input[name="promo_code"]');
    if (input) input.value = promo.toUpperCase();
  }}
}})();
</script>
</body>
</html>"""

LOGIN_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Login</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/login">
  <img src="/static/logo_dark.png" alt="Auction Intel" style="height:48px;margin-bottom:16px;">
  <p class="sub">Nonprofit Auction Event Finder</p>
  <!-- error -->
  <input type="email" name="email" placeholder="Email address" autofocus required>
  <input type="password" name="password" placeholder="Password" required>
  <button type="submit">Log In</button>
  <p class="link"><a href="/forgot-password">Forgot password?</a></p>
  <p class="link">Don't have an account? <a href="/register">Create New Account</a></p>
</form>
</body>
</html>"""

FORGOT_PASSWORD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Forgot Password</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/forgot-password">
  <img src="/static/logo_dark.png" alt="Auction Intel" style="height:48px;margin-bottom:16px;">
  <p class="sub">Reset your password</p>
  <!-- message -->
  <input type="email" name="email" placeholder="Email address" autofocus required>
  <button type="submit">Send Reset Link</button>
  <p class="link"><a href="/login">Back to login</a></p>
</form>
</body>
</html>"""

RESET_PASSWORD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Reset Password</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/reset-password">
  <img src="/static/logo_dark.png" alt="Auction Intel" style="height:48px;margin-bottom:16px;">
  <p class="sub">Set a new password</p>
  <!-- error -->
  <input type="hidden" name="token" value="{{{{TOKEN}}}}">
  <input type="password" name="password" placeholder="New password (min 6 chars)" autofocus required>
  <input type="password" name="confirm" placeholder="Confirm new password" required>
  <button type="submit">Reset Password</button>
  <p class="link"><a href="/login">Back to login</a></p>
</form>
</body>
</html>"""

WALLET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Wallet</title>
<script src="https://js.stripe.com/v3/"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 800px; margin: 0 auto; padding: 24px; }
  .balance-card { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 32px; text-align: center; margin-bottom: 24px; }
  .balance-card .amount { font-size: 48px; font-weight: 700; color: #4ade80; margin: 16px 0; }
  .balance-card .label { color: #a3a3a3; font-size: 14px; text-transform: uppercase; }
  .topup-section { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .topup-section h3 { font-size: 16px; margin-bottom: 12px; color: #d4d4d4; }
  .amount-row { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
  .amount-row input { flex: 1; padding: 12px; background: #000000; border: 1px solid #333; border-radius: 8px; color: #f5f5f5; font-size: 16px; font-family: inherit; outline: none; }
  .amount-row input:focus { border-color: #eab308; }
  .topup-hint { color: #737373; font-size: 12px; margin-bottom: 16px; }
  #payment-element { margin-bottom: 16px; }
  #payment-message { color: #f87171; font-size: 13px; margin-bottom: 12px; display: none; }
  #payment-success { color: #4ade80; font-size: 13px; margin-bottom: 12px; display: none; }
  .pay-btn { width: 100%; padding: 14px 24px; background: #ffd900; color: #000; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .pay-btn:hover { background: #ca8a04; }
  .pay-btn:disabled { background: #555; color: #999; cursor: not-allowed; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #000; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .txn-section { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; }
  .txn-section h3 { font-size: 16px; margin-bottom: 12px; color: #d4d4d4; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { color: #a3a3a3; text-transform: uppercase; font-size: 10px; padding: 8px 6px; text-align: left; border-bottom: 1px solid #262626; }
  td { padding: 8px 6px; border-bottom: 1px solid #1a1a1a; color: #d4d4d4; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <div class="balance-card">
    <div class="label">Wallet Balance</div>
    <div class="amount" id="balanceDisplay">{{BALANCE}}</div>
  </div>

  <div class="topup-section">
    <h3>Add Funds</h3>
    <div class="amount-row">
      <input type="number" id="topupAmount" placeholder="Amount in USD" min="250" max="9999" step="1" value="500">
    </div>
    <p class="topup-hint">Minimum $250, maximum $9,999 per top-up</p>
    <div id="payment-element"></div>
    <div id="payment-message"></div>
    <div id="payment-success"></div>
    <button id="payBtn" class="pay-btn" onclick="handlePayment()">Add Funds</button>
  </div>

  <div class="txn-section">
    <h3>Transaction History</h3>
    <table>
      <thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Description</th></tr></thead>
      <tbody>{{TXN_ROWS}}</tbody>
    </table>
  </div>
</div>

<script>
const stripe = Stripe('{{STRIPE_PK}}');
const appearance = {
  theme: 'night',
  variables: {
    colorPrimary: '#eab308',
    colorBackground: '#111111',
    colorText: '#f5f5f5',
    colorDanger: '#f87171',
    fontFamily: "'SF Mono', 'Consolas', monospace",
    borderRadius: '8px',
  }
};

let elements = null;
let clientSecret = null;
let processing = false;

async function initPayment() {
  const amount = parseFloat(document.getElementById('topupAmount').value);
  if (!amount || amount < 250 || amount > 9999) {
    showError('Amount must be between $250 and $9,999');
    return false;
  }
  const btn = document.getElementById('payBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Preparing payment...';
  hideMessages();

  try {
    const res = await fetch('/api/wallet/topup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount }),
    });
    const data = await res.json();
    if (data.error) {
      showError(data.error);
      btn.disabled = false;
      btn.textContent = 'Add Funds';
      return false;
    }
    clientSecret = data.client_secret;
    elements = stripe.elements({ appearance, clientSecret });
    const paymentElement = elements.create('payment');
    paymentElement.mount('#payment-element');
    btn.disabled = false;
    btn.textContent = 'Pay $' + amount.toLocaleString();
    return true;
  } catch (err) {
    showError('Error: ' + err.message);
    btn.disabled = false;
    btn.textContent = 'Add Funds';
    return false;
  }
}

async function handlePayment() {
  if (processing) return;
  if (!clientSecret) {
    const ok = await initPayment();
    if (!ok) return;
    // After mounting, user fills in card — next click confirms
    return;
  }
  processing = true;
  const btn = document.getElementById('payBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Processing payment...';
  hideMessages();

  const { error } = await stripe.confirmPayment({
    elements,
    confirmParams: { return_url: window.location.origin + '/wallet' },
    redirect: 'if_required',
  });

  if (error) {
    showError(error.message);
    btn.disabled = false;
    btn.textContent = 'Retry Payment';
    processing = false;
  } else {
    showSuccess('Payment successful! Updating balance...');
    btn.disabled = true;
    btn.textContent = 'Payment Complete';
    // Give webhook a moment to process, then reload
    setTimeout(() => window.location.reload(), 2500);
  }
}

// Re-init when amount changes
document.getElementById('topupAmount').addEventListener('change', function() {
  if (elements) {
    document.getElementById('payment-element').innerHTML = '';
    elements = null;
    clientSecret = null;
    processing = false;
    document.getElementById('payBtn').textContent = 'Add Funds';
    document.getElementById('payBtn').disabled = false;
    hideMessages();
  }
});

function showError(msg) {
  const el = document.getElementById('payment-message');
  el.textContent = msg;
  el.style.display = 'block';
}
function showSuccess(msg) {
  const el = document.getElementById('payment-success');
  el.textContent = msg;
  el.style.display = 'block';
}
function hideMessages() {
  document.getElementById('payment-message').style.display = 'none';
  document.getElementById('payment-success').style.display = 'none';
}
</script>
</div>
</body>
</html>"""

PROFILE_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Profile</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }}
  {{{{SIDEBAR_CSS}}}}
  .container {{ max-width: 800px; margin: 0 auto; padding: 24px; }}
  .card {{ background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 24px; }}
  .card h3 {{ font-size: 16px; margin-bottom: 16px; color: #d4d4d4; }}
  .info-row {{ display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #1a1a1a; }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-row .label {{ color: #a3a3a3; font-size: 13px; }}
  .info-row .value {{ font-size: 13px; font-weight: 600; }}
  .info-row .value.admin {{ color: #eab308; }}
  .info-row .value.balance {{ color: #4ade80; }}
  input {{ width: 100%; padding: 10px 14px; background: #000000; border: 1px solid #333; border-radius: 8px; color: #f5f5f5; font-size: 14px; font-family: inherit; outline: none; margin-bottom: 12px; }}
  input:focus {{ border-color: #eab308; }}
  button {{ padding: 10px 24px; background: #ffd900; color: #000; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }}
  button:hover {{ background: #ca8a04; }}
  .alert {{ padding: 12px 16px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; }}
  .alert.error {{ background: #f8717122; color: #f87171; border: 1px solid #f8717144; }}
  .alert.success {{ background: #4ade8022; color: #4ade80; border: 1px solid #4ade8044; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
  .stat-item {{ background: #000000; border-radius: 8px; padding: 16px; text-align: center; }}
  .stat-item .num {{ font-size: 24px; font-weight: 700; color: #eab308; }}
  .stat-item .lbl {{ font-size: 11px; color: #a3a3a3; text-transform: uppercase; margin-top: 4px; }}
</style>
</head>
<body>
{{{{SIDEBAR_HTML}}}}
<div class="main-content">
<div class="container">
  <div id="alerts"></div>

  <div class="card">
    <h3>Account Information</h3>
    <div class="info-row"><span class="label">Email</span><span class="value">{{{{EMAIL}}}}</span></div>
    <div class="info-row"><span class="label">Account Type</span><span class="value admin">{{{{ACCOUNT_TYPE}}}}</span></div>
    <div class="info-row"><span class="label">Member Since</span><span class="value">{{{{CREATED_AT}}}}</span></div>
    <div class="info-row"><span class="label">Current Balance</span><span class="value balance">{{{{BALANCE}}}}</span></div>
  </div>

  <div class="card">
    <h3>Account Stats</h3>
    <div class="stats-grid">
      <div class="stat-item"><div class="num">{{{{JOB_COUNT}}}}</div><div class="lbl">Total Searches</div></div>
      <div class="stat-item"><div class="num">{{{{TOTAL_SPENT}}}}</div><div class="lbl">Total Spent</div></div>
      <div class="stat-item"><div class="num">{{{{TOTAL_TOPUPS}}}}</div><div class="lbl">Total Funded</div></div>
      <div class="stat-item"><div class="num">{{{{BALANCE}}}}</div><div class="lbl">Current Balance</div></div>
    </div>
  </div>

  <div class="card">
    <h3>Change Password</h3>
    <form method="POST" action="/profile/password">
      <input type="password" name="current_password" placeholder="Current password" required>
      <input type="password" name="new_password" placeholder="New password (min 6 chars)" required>
      <input type="password" name="confirm_password" placeholder="Confirm new password" required>
      <button type="submit">Update Password</button>
    </form>
  </div>
</div>
<script>
(function() {{
  const params = new URLSearchParams(window.location.search);
  const alerts = document.getElementById('alerts');
  if (params.get('error') === 'current') alerts.innerHTML = '<div class="alert error">Current password is incorrect.</div>';
  else if (params.get('error') === 'length') alerts.innerHTML = '<div class="alert error">New password must be at least 6 characters.</div>';
  else if (params.get('error') === 'match') alerts.innerHTML = '<div class="alert error">New passwords do not match.</div>';
  else if (params.get('success') === '1') alerts.innerHTML = '<div class="alert success">Password updated successfully.</div>';
}})();
</script>
</div>
</body>
</html>"""

BILLING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Billing</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
  .summary-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px; }
  .summary-card { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 20px; text-align: center; }
  .summary-card .amount { font-size: 28px; font-weight: 700; margin: 8px 0; }
  .summary-card .amount.green { color: #4ade80; }
  .summary-card .amount.red { color: #f87171; }
  .summary-card .amount.gold { color: #eab308; }
  .summary-card .amount.blue { color: #60a5fa; }
  .summary-card .label { font-size: 11px; color: #a3a3a3; text-transform: uppercase; }
  .section { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .section h3 { font-size: 16px; margin-bottom: 16px; color: #d4d4d4; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { color: #a3a3a3; text-transform: uppercase; font-size: 10px; padding: 8px 6px; text-align: left; border-bottom: 1px solid #262626; }
  td { padding: 8px 6px; border-bottom: 1px solid #1a1a1a; color: #d4d4d4; }
  .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
  .tabs button { padding: 8px 16px; background: #1a1a1a; border: 1px solid #333; color: #a3a3a3; border-radius: 6px; cursor: pointer; font-family: inherit; font-size: 12px; }
  .tabs button.active { background: #262626; color: #f5f5f5; border-color: #eab308; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <div class="summary-grid">
    <div class="summary-card">
      <div class="label">Current Balance</div>
      <div class="amount green">{{BALANCE}}</div>
    </div>
    <div class="summary-card">
      <div class="label">Total Spent</div>
      <div class="amount red">{{TOTAL_SPENT}}</div>
    </div>
    <div class="summary-card">
      <div class="label">Research Fees</div>
      <div class="amount gold">{{RESEARCH_FEES}}</div>
    </div>
    <div class="summary-card">
      <div class="label">Lead Fees</div>
      <div class="amount gold">{{LEAD_FEES}}</div>
    </div>
    <div class="summary-card">
      <div class="label">Total Top-Ups</div>
      <div class="amount blue">{{TOTAL_TOPUPS}}</div>
    </div>
  </div>

  <div class="section">
    <div class="tabs">
      <button class="active" onclick="showTab('jobs')">Job History ({{JOB_COUNT}})</button>
      <button onclick="showTab('txns')">All Transactions</button>
    </div>

    <div id="tab-jobs" class="tab-content active">
      <table>
        <thead><tr><th>Date</th><th>Status</th><th>Searched</th><th>Found</th><th>Billable</th><th>Cost</th><th>Downloads</th></tr></thead>
        <tbody>{{JOB_ROWS}}</tbody>
      </table>
    </div>

    <div id="tab-txns" class="tab-content">
      <table>
        <thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Description</th><th>Job ID</th></tr></thead>
        <tbody>{{TXN_ROWS}}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}
</script>
</div>
</body>
</html>"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}

  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }

  .balance-bar { background: #111111; border: 1px solid #262626; border-radius: 8px; padding: 12px 20px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
  .balance-bar .bal { color: #4ade80; font-weight: 700; }
  .balance-bar .fee-est { color: #a3a3a3; }
  .balance-bar a { color: #eab308; text-decoration: none; font-size: 12px; }

  .input-section { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .input-section h2 { font-size: 16px; margin-bottom: 12px; color: #d4d4d4; }
  .input-section textarea { width: 100%; height: 160px; background: #000000; border: 1px solid #333333; border-radius: 8px; color: #f5f5f5; padding: 12px; font-family: inherit; font-size: 13px; resize: vertical; outline: none; }
  .input-section textarea:focus { border-color: #eab308; }
  .input-section textarea::placeholder { color: #737373; }

  .controls { display: flex; gap: 12px; margin-top: 16px; align-items: center; }
  .controls button { padding: 10px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .btn-primary { background: #ffd900; color: #000000; }
  .btn-primary:hover { background: #ca8a04; }
  .btn-primary:disabled { background: #333333; cursor: not-allowed; }
  .btn-secondary { background: #1a1a1a; color: #f5f5f5; }
  .btn-secondary:hover { background: #333333; }
  .count-label { color: #a3a3a3; font-size: 13px; margin-left: auto; }

  .progress-section { display: none; background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .progress-bar-container { background: #000000; border-radius: 8px; height: 32px; overflow: hidden; margin-bottom: 16px; position: relative; }
  .progress-bar { height: 100%; background: linear-gradient(90deg, #ca8a04, #eab308); border-radius: 8px; transition: width 0.3s ease; width: 0%; }
  .progress-text { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); font-size: 13px; font-weight: 600; color: white; text-shadow: 0 1px 2px rgba(0,0,0,0.5); }

  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat { background: #000000; border-radius: 8px; padding: 12px; text-align: center; }
  .stat .num { font-size: 24px; font-weight: 700; }
  .stat .label { font-size: 11px; color: #a3a3a3; text-transform: uppercase; margin-top: 4px; }
  .stat.found .num { color: #4ade80; }
  .stat.external .num { color: #eab308; }
  .stat.notfound .num { color: #f87171; }
  .stat.uncertain .num { color: #fbbf24; }

  .terminal { background: #000000; border: 1px solid #262626; border-radius: 8px; padding: 16px; max-height: 400px; overflow-y: auto; font-size: 13px; line-height: 1.6; }
  .terminal .line { padding: 2px 0; }
  .terminal .line.processing { color: #a3a3a3; }
  .terminal .line.found { color: #4ade80; }
  .terminal .line.thirdpty_found { color: #eab308; }
  .terminal .line.not_found { color: #f87171; }
  .terminal .line.uncertain { color: #fbbf24; }
  .terminal .line.error { color: #f87171; }
  .terminal .line.info { color: #eab308; }
  .terminal .line.complete { color: #4ade80; font-weight: 700; }
  .terminal .timestamp { color: #404040; margin-right: 8px; }

  .billing-summary { background: #0a0a0a; border: 1px solid #262626; border-radius: 8px; padding: 16px; margin-top: 16px; font-size: 13px; line-height: 1.8; display: none; }
  .billing-summary .row { display: flex; justify-content: space-between; }
  .billing-summary .total { border-top: 1px solid #333; padding-top: 8px; margin-top: 8px; font-weight: 700; color: #eab308; }

  .download-section { display: none; margin-top: 16px; padding-top: 16px; border-top: 1px solid #262626; }
  .download-section .dl-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .download-section a, .download-section button { display: inline-block; padding: 10px 20px; background: #ffd900; color: #000000; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px; border: none; cursor: pointer; font-family: inherit; }
  .download-section a:hover, .download-section button:hover { filter: brightness(0.9); }
  .download-section a.json-btn { background: #7c3aed; }
  .download-section a.xlsx-btn { background: #2563eb; }
  .download-section button.view-btn { background: #0891b2; }

  .json-viewer { display: none; margin-top: 16px; background: #000000; border: 1px solid #262626; border-radius: 8px; padding: 16px; max-height: 500px; overflow: auto; }
  .json-viewer pre { font-size: 12px; line-height: 1.5; color: #f5f5f5; white-space: pre-wrap; word-break: break-word; }
  .json-viewer .key { color: #eab308; }
  .json-viewer .string { color: #4ade80; }
  .json-viewer .number { color: #fbbf24; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <div class="balance-bar" id="balanceBar" style="display:none;">
    <span>Balance: <span class="bal" id="balDisplay">$0.00</span></span>
    <span class="fee-est" id="feeEstimate"></span>
    <a href="/wallet">Top Up</a>
  </div>

  <div class="input-section">
    <h2>Enter Nonprofit Domains or Names</h2>
    <textarea id="input" placeholder="Paste nonprofit domains or names here, one per line or comma-separated...&#10;&#10;Example:&#10;National Museum of Mexican Art&#10;Radio Milwaukee&#10;driveagainstdiabetes.org"></textarea>
    <div class="toggle-row" style="margin:12px 0;display:flex;align-items:center;gap:10px;">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#d4d4d4;" title="When enabled, only Complete Contact leads are returned — each includes event title, event date, verified event page link, auction type, contact name, and contact email.">
        <input type="checkbox" id="completeOnly" style="accent-color:#eab308;width:16px;height:16px;">
        <span style="font-weight:600;">Complete Contacts Only</span>
      </label>
      <span style="font-size:12px;color:#737373;">Fewer results, but every lead includes a contact name and email.</span>
    </div>
    <div class="controls">
      <button class="btn-primary" id="searchBtn" onclick="startSearch()">Search for Auctions</button>
      <button class="btn-secondary" onclick="document.getElementById('input').value='';updateCount()">Clear</button>
      <span class="count-label" id="countLabel">0 nonprofits</span>
    </div>
  </div>

  <div class="progress-section" id="progressSection">
    <div class="progress-bar-container">
      <div class="progress-bar" id="progressBar"></div>
      <div class="progress-text" id="progressText">0 / 0</div>
    </div>
    <div id="etaDisplay" style="display:none;text-align:center;padding:6px 0 2px;font-size:13px;color:#a3a3a3;font-family:monospace;">
      <span id="etaElapsed"></span> &nbsp;&bull;&nbsp; <span id="etaRemaining" style="color:#eab308;"></span>
    </div>

    <div class="stats">
      <div class="stat found"><div class="num" id="statFound">0</div><div class="label">Found</div></div>
      <div class="stat external"><div class="num" id="statExternal">0</div><div class="label">3rd Party</div></div>
      <div class="stat notfound"><div class="num" id="statNotFound">0</div><div class="label">Not Found</div></div>
      <div class="stat uncertain"><div class="num" id="statUncertain">0</div><div class="label">Uncertain</div></div>
    </div>

    <div class="terminal" id="terminal"></div>

    <div class="billing-summary" id="billingSummary"></div>

    <div class="download-section" id="downloadSection">
      <div class="dl-row">
        <a href="#" id="downloadCsv">Download CSV</a>
        <a href="#" id="downloadJson" class="json-btn">Download JSON</a>
        <a href="#" id="downloadXlsx" class="xlsx-btn">Download XLSX</a>
        <button class="view-btn" onclick="toggleJsonViewer()">View Results (JSON)</button>
      </div>
      <div class="json-viewer" id="jsonViewer"><pre id="jsonContent"></pre></div>
    </div>
  </div>
</div>

<script>
const IS_ADMIN = {{IS_ADMIN}};
let balanceCents = {{BALANCE_CENTS}};

const inputEl = document.getElementById('input');
const countLabel = document.getElementById('countLabel');
const searchBtn = document.getElementById('searchBtn');
const progressSection = document.getElementById('progressSection');
const progressBar = document.getElementById('progressBar');
const progressText = document.getElementById('progressText');
const terminal = document.getElementById('terminal');
const downloadSection = document.getElementById('downloadSection');
const balanceBar = document.getElementById('balanceBar');
const balDisplay = document.getElementById('balDisplay');
const feeEstimate = document.getElementById('feeEstimate');

if (!IS_ADMIN) {
  balanceBar.style.display = 'flex';
  balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
}

let counts = { found: 0, '3rdpty_found': 0, not_found: 0, uncertain: 0, error: 0 };
let processed = 0;
let totalNonprofits = 0;
let currentJobId = null;
let currentEvtSource = null;

// Auto-fill from IRS database selection
const irsData = sessionStorage.getItem('irs_nonprofits');
if (irsData) {
  inputEl.value = irsData;
  sessionStorage.removeItem('irs_nonprofits');
  updateCount();
}

function updateCount() {
  const items = inputEl.value.replace(/,/g, '\\n').split('\\n').filter(s => s.trim());
  const n = items.length;
  countLabel.textContent = n + ' nonprofit' + (n !== 1 ? 's' : '');
  if (!IS_ADMIN && n > 0) {
    const fee = n <= 10000 ? 8 : (n <= 50000 ? 7 : 6);
    const est = (n * fee / 100).toFixed(2);
    feeEstimate.textContent = '~$' + est + ' research fee for ' + n + ' nonprofits';
  } else {
    feeEstimate.textContent = '';
  }
}

inputEl.addEventListener('input', updateCount);

function timestamp() {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}

function log(msg, cls) {
  cls = cls || '';
  // CSS classes can't start with a digit — remap 3rdpty_found
  const cssClass = cls === '3rdpty_found' ? 'thirdpty_found' : cls;
  const line = document.createElement('div');
  line.className = 'line ' + cssClass;
  line.innerHTML = '<span class="timestamp">[' + timestamp() + ']</span> ' + escapeHtml(msg);
  terminal.appendChild(line);
  terminal.scrollTop = terminal.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function updateStats() {
  document.getElementById('statFound').textContent = counts.found;
  document.getElementById('statExternal').textContent = counts['3rdpty_found'];
  document.getElementById('statNotFound').textContent = counts.not_found;
  document.getElementById('statUncertain').textContent = counts.uncertain + counts.error;
}

function updateProgress() {
  const pct = totalNonprofits > 0 ? Math.round((processed / totalNonprofits) * 100) : 0;
  progressBar.style.width = pct + '%';
  progressText.textContent = processed + ' / ' + totalNonprofits;
}

async function startSearch() {
  const raw = inputEl.value.trim();
  if (!raw) return;

  // Close any previous EventSource
  if (currentEvtSource) {
    currentEvtSource.close();
    currentEvtSource = null;
  }

  searchBtn.disabled = true;
  progressSection.style.display = 'block';
  downloadSection.style.display = 'none';
  document.getElementById('billingSummary').style.display = 'none';
  terminal.innerHTML = '';
  counts = { found: 0, '3rdpty_found': 0, not_found: 0, uncertain: 0, error: 0 };
  processed = 0;
  totalNonprofits = 0;
  updateStats();
  updateProgress();

  log('Starting search...', 'info');

  try {
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nonprofits: raw, complete_only: document.getElementById('completeOnly').checked }),
    });

    if (!res.ok) {
      const err = await res.json();
      if (res.status === 402 && err.affordable_count) {
        log('Insufficient balance: ' + err.error, 'error');
        log('Tip: Reduce your list to ~' + err.affordable_count + ' nonprofits, or top up your wallet.', 'info');
      } else {
        log('Error: ' + (err.error || 'Unknown error'), 'error');
      }
      searchBtn.disabled = false;
      return;
    }

    const { job_id, total, research_fee_each, estimated_cost_cents } = await res.json();
    totalNonprofits = total;
    currentJobId = job_id;
    if (!IS_ADMIN) {
      log('Research fee: $' + (total * research_fee_each / 100).toFixed(2) + ' (' + total + ' x $' + (research_fee_each/100).toFixed(2) + ')', 'info');
      if (estimated_cost_cents > 0) {
        log('Estimated total cost (incl. lead fees): ~$' + (estimated_cost_cents / 100).toFixed(2), 'info');
      }
    }
    log('Job started: ' + job_id + ' (' + total + ' nonprofits)', 'info');
    updateProgress();

    currentEvtSource = new EventSource('/api/progress/' + job_id);

    currentEvtSource.onmessage = (e) => {
      const data = JSON.parse(e.data);

      switch (data.type) {
        case 'started':
          log('Processing ' + data.total + ' nonprofits in ' + data.batches + ' batch(es)', 'info');
          break;

        case 'processing':
          log('[' + data.index + '/' + data.total + '] Researching: ' + data.nonprofit, 'processing');
          break;

        case 'balance':
          if (!IS_ADMIN) {
            balanceCents = data.balance;
            balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
          }
          break;

        case 'result':
          processed++;
          const status = data.status || 'uncertain';
          if (counts.hasOwnProperty(status)) counts[status]++;
          else counts.uncertain++;

          let msg = '[' + data.index + '/' + data.total + '] ' + status.toUpperCase() + ': ' + data.nonprofit;
          if (IS_ADMIN && data.event_title) msg += ' -> ' + data.event_title;
          if (data.tier && data.tier !== 'not_billable') msg += ' [' + data.tier + ']';

          log(msg, status);

          // Add Make Exclusive button for billable leads
          if (!IS_ADMIN && data.tier && data.tier !== 'not_billable' && data.event_url && data.event_title) {
            const btnRow = document.createElement('div');
            btnRow.style.cssText = 'margin:-4px 0 6px 48px;';
            const eBtn = document.createElement('button');
            eBtn.textContent = 'Lock Lead';
            eBtn.title = 'Lock this lead so it stops being sold to new customers — $2.50';
            eBtn.style.cssText = 'background:#1a1500;color:#eab308;border:1px solid #332d00;border-radius:4px;padding:2px 10px;font-size:11px;cursor:pointer;font-family:inherit;';
            eBtn.onclick = function() { makeExclusive(eBtn, data.nonprofit, data.event_title, data.event_url); };
            btnRow.appendChild(eBtn);
            terminal.appendChild(btnRow);
          }

          updateStats();
          updateProgress();
          break;

        case 'balance_warning':
          log('BALANCE WARNING: ' + data.message, 'error');
          break;

        case 'batch_done':
          log('--- Batch complete: ' + data.processed + '/' + data.total + ' processed ---', 'info');
          break;

        case 'complete':
          log('', '');
          // Update ETA to show final elapsed time
          const cEtaDiv = document.getElementById('etaDisplay');
          const cM = Math.floor(data.elapsed / 60);
          const cS = Math.round(data.elapsed % 60);
          cEtaDiv.style.display = 'block';
          document.getElementById('etaElapsed').textContent = 'Total time: ' + cM + 'm ' + (cS < 10 ? '0' : '') + cS + 's';
          document.getElementById('etaRemaining').textContent = 'Complete!';
          document.getElementById('etaRemaining').style.color = '#4ade80';
          log('SEARCH COMPLETE in ' + data.elapsed + 's', 'complete');
          log('Found: ' + data.summary.found + ' | 3rd Party: ' + data.summary['3rdpty_found'] +
              ' | Not Found: ' + data.summary.not_found + ' | Uncertain: ' + data.summary.uncertain, 'complete');

          // Show billing summary for non-admin
          if (!IS_ADMIN && data.billing) {
            const bs = document.getElementById('billingSummary');
            const b = data.billing;
            let html = '<div class="row"><span>Searched: ' + totalNonprofits + ' nonprofits</span><span>$' + (b.research_fees / 100).toFixed(2) + '</span></div>';
            if (b.lead_fees) {
              for (const [tier, info] of Object.entries(b.lead_fees)) {
                html += '<div class="row"><span>' + info.count + ' ' + tier + ' leads x $' + (info.price_each / 100).toFixed(2) + '</span><span>$' + (info.total / 100).toFixed(2) + '</span></div>';
              }
            }
            html += '<div class="row total"><span>Total charged</span><span>$' + (b.total_charged / 100).toFixed(2) + '</span></div>';
            html += '<div class="row"><span>Remaining balance</span><span style="color:#4ade80">$' + (data.balance / 100).toFixed(2) + '</span></div>';
            if (data.billable_count !== undefined) {
              html += '<div class="row" style="margin-top:8px;color:#a3a3a3;"><span>Billable leads in download: ' + data.billable_count + '</span></div>';
            }
            bs.innerHTML = html;
            bs.style.display = 'block';
            balanceCents = data.balance;
            balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
          }

          const completedJobId = data.job_id || currentJobId;
          downloadSection.style.display = 'block';
          document.getElementById('downloadCsv').href = '/api/download/' + completedJobId + '/csv';
          document.getElementById('downloadJson').href = '/api/download/' + completedJobId + '/json';
          document.getElementById('downloadXlsx').href = '/api/download/' + completedJobId + '/xlsx';

          searchBtn.disabled = false;
          currentEvtSource.close();
          currentEvtSource = null;
          break;

        case 'error':
          log('ERROR: ' + data.message, 'error');
          searchBtn.disabled = false;
          currentEvtSource.close();
          currentEvtSource = null;
          break;

        case 'eta':
          const etaDiv = document.getElementById('etaDisplay');
          etaDiv.style.display = 'block';
          const eM = Math.floor(data.elapsed / 60);
          const eS = data.elapsed % 60;
          const rM = Math.floor(data.remaining / 60);
          const rS = data.remaining % 60;
          document.getElementById('etaElapsed').textContent = 'Elapsed: ' + eM + 'm ' + (eS < 10 ? '0' : '') + eS + 's';
          document.getElementById('etaRemaining').textContent = '~' + rM + 'm ' + (rS < 10 ? '0' : '') + rS + 's remaining';
          break;

        case 'heartbeat':
          break;
      }
    };

    currentEvtSource.onerror = () => {
      log('Connection lost. Check results below if available.', 'error');
      searchBtn.disabled = false;
      if (currentEvtSource) { currentEvtSource.close(); currentEvtSource = null; }
    };

  } catch (err) {
    log('Request failed: ' + err.message, 'error');
    searchBtn.disabled = false;
  }
}

function syntaxHighlight(json) {
  json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return json.replace(/("(\\\\u[a-zA-Z0-9]{4}|\\\\[^u]|[^\\\\"])*"(\\s*:)?|\\b(true|false|null)\\b|-?\\d+(?:\\.\\d*)?(?:[eE][+\\-]?\\d+)?)/g, function (match) {
    let cls = 'number';
    if (/^"/.test(match)) {
      if (/:$/.test(match)) { cls = 'key'; }
      else { cls = 'string'; }
    }
    return '<span class="' + cls + '">' + match + '</span>';
  });
}

async function toggleJsonViewer() {
  const viewer = document.getElementById('jsonViewer');
  const content = document.getElementById('jsonContent');

  if (viewer.style.display === 'block') {
    viewer.style.display = 'none';
    return;
  }

  if (!currentJobId) return;

  content.textContent = 'Loading results...';
  viewer.style.display = 'block';

  try {
    const res = await fetch('/api/results/' + currentJobId);
    const data = await res.json();
    content.innerHTML = syntaxHighlight(JSON.stringify(data, null, 2));
  } catch (err) {
    content.textContent = 'Failed to load results: ' + err.message;
  }
}

async function makeExclusive(btn, nonprofit, title, url) {
  if (!confirm('Lock this lead for $2.50? It will stop being sold to new customers going forward.\\n\\nApplies to this specific event lead (not the entire organization). Previous buyers retain access.')) return;
  btn.disabled = true;
  btn.textContent = 'Processing...';
  try {
    const res = await fetch('/api/exclusive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: currentJobId, nonprofit_name: nonprofit, event_title: title, event_url: url }),
    });
    const data = await res.json();
    if (data.success) {
      btn.textContent = 'Locked';
      btn.style.background = '#4ade80';
      btn.style.color = '#000';
      btn.style.cursor = 'default';
      if (data.balance !== undefined) {
        balanceCents = data.balance;
        balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
      }
      log('Lead locked: ' + title, 'info');
    } else {
      alert(data.error || 'Failed to purchase exclusive lead');
      btn.disabled = false;
      btn.textContent = 'Lock Lead';
    }
  } catch (err) {
    alert('Request failed: ' + err.message);
    btn.disabled = false;
    btn.textContent = 'Make Exclusive';
  }
}
</script>
</div>
</body>
</html>"""

DATABASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Search Nonprofit Database</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }

  .filters { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .filters h2 { font-size: 16px; margin-bottom: 4px; color: #d4d4d4; }
  .filters .subtitle { font-size: 12px; color: #737373; margin-bottom: 16px; }
  .section-title { font-size: 12px; color: #eab308; text-transform: uppercase; font-weight: 700; margin: 16px 0 8px; padding-top: 12px; border-top: 1px solid #262626; }
  .section-title:first-of-type { margin-top: 0; padding-top: 0; border-top: none; }
  .filter-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .fg { display: flex; flex-direction: column; gap: 3px; }
  .fg label { font-size: 10px; color: #a3a3a3; text-transform: uppercase; letter-spacing: 0.5px; }
  .fg input, .fg select { padding: 7px 10px; background: #000000; border: 1px solid #333333; border-radius: 6px; color: #f5f5f5; font-family: inherit; font-size: 12px; outline: none; }
  .fg input:focus, .fg select:focus { border-color: #eab308; }

  .checkboxes { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
  .checkboxes label { font-size: 12px; display: flex; align-items: center; gap: 5px; cursor: pointer; color: #d4d4d4; }
  .checkboxes input[type="checkbox"] { accent-color: #eab308; }

  .controls { display: flex; gap: 12px; margin-top: 16px; align-items: center; padding-top: 12px; border-top: 1px solid #262626; }
  .controls button { padding: 10px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .btn-primary { background: #ffd900; color: #000000; }
  .btn-primary:hover { background: #ca8a04; }
  .btn-send { background: #ffd900; color: #000000; }
  .btn-send:hover { background: #047857; }
  .btn-send:disabled { background: #333333; cursor: not-allowed; }
  .result-count { color: #a3a3a3; font-size: 13px; margin-left: auto; }

  table { width: 100%; border-collapse: collapse; font-size: 11px; }
  th { background: #111111; color: #a3a3a3; text-transform: uppercase; font-size: 9px; padding: 8px 6px; text-align: left; position: sticky; top: 0; border-bottom: 2px solid #262626; }
  td { padding: 6px; border-bottom: 1px solid #1a1a1a; color: #d4d4d4; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tr:hover td { background: #111111; }
  td.ck { width: 28px; text-align: center; }
  td.ck input { accent-color: #eab308; }
  .tag { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 9px; font-weight: 600; margin-right: 2px; }
  .tag.gala { background: #7c3aed33; color: #eab308; }
  .tag.auction { background: #05966933; color: #34d399; }
  .tag.golf { background: #0891b233; color: #67e8f9; }
  .tag.dinner { background: #b4560633; color: #fdba74; }
  .tag.ball { background: #be185d33; color: #f472b6; }
  .tag.raffle { background: #d9770633; color: #fbbf24; }
  .tag.run { background: #16a34a33; color: #86efac; }
  .tag.art { background: #9333ea33; color: #c084fc; }
  .tag.other { background: #47556933; color: #a3a3a3; }
  .tier { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; }
  .tier.aplus { background: #05966933; color: #34d399; }
  .tier.a { background: #0891b233; color: #67e8f9; }
  .tier.bplus { background: #ca8a0433; color: #fbbf24; }
  .tier.c { background: #47556933; color: #a3a3a3; }
  .table-wrap { max-height: 600px; overflow: auto; border: 1px solid #262626; border-radius: 8px; }
  a.ws { color: #eab308; text-decoration: none; }
  a.ws:hover { text-decoration: underline; }
  .amount-select { min-width: 0; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <div class="filters">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div>
        <h2>Search Nonprofit Database</h2>
        <p class="subtitle">276,000 nonprofit organizations with pre-extracted event keywords | Filter and send to Auction Finder</p>
      </div>
      <button onclick="toggleFilters()" id="toggleBtn" style="padding:6px 14px;background:#262626;color:#a3a3a3;border:1px solid #333;border-radius:6px;cursor:pointer;font-family:inherit;font-size:12px;white-space:nowrap;">Hide Filters</button>
    </div>

    <div id="filterBody">
    <div class="section-title">Organization & Location</div>
    <div class="filter-grid">
      <div class="fg"><label>Organization Name</label><input type="text" id="fName" placeholder="e.g. Museum of Art"></div>
      <div class="fg"><label>State</label><select id="fState"><option value="">All States</option></select></div>
      <div class="fg"><label>Region</label>
        <select id="fRegion">
          <option value="">All Regions</option>
          <option value="northeast">Northeast (CT,DE,DC,ME,MD,MA,NH,NJ,NY,PA,RI,VT)</option>
          <option value="southeast">Southeast (AL,AR,FL,GA,KY,LA,MS,NC,SC,TN,VA,WV)</option>
          <option value="midwest">Midwest (IL,IN,IA,KS,MI,MN,MO,NE,ND,OH,SD,WI)</option>
          <option value="southwest">Southwest (AZ,NM,OK,TX)</option>
          <option value="west">West (AK,CA,CO,HI,ID,MT,NV,OR,UT,WA,WY)</option>
        </select>
      </div>
      <div class="fg"><label>City</label><input type="text" id="fCity" placeholder="e.g. Chicago"></div>
    </div>

    <div class="section-title">Event Type & Classification</div>
    <div class="filter-grid">
      <div class="fg"><label>Primary Event Type</label>
        <select id="fPrimaryType">
          <option value="">All Types</option>
          <option value="GALA">Gala</option>
          <option value="GOLF">Golf</option>
          <option value="AUCTION">Auction</option>
          <option value="EVENT">Event</option>
          <option value="DINNER">Dinner</option>
          <option value="ART">Art</option>
          <option value="RUN">Run/Race</option>
          <option value="FUNDRAISER">Fundraiser</option>
          <option value="BALL">Ball</option>
          <option value="SALE">Sale</option>
          <option value="FAIR">Fair</option>
          <option value="FESTIVAL">Festival</option>
          <option value="LUNCHEON">Luncheon</option>
          <option value="WALK">Walk</option>
          <option value="RAFFLE">Raffle</option>
          <option value="CELEBRATION">Celebration</option>
          <option value="NIGHT">Night</option>
          <option value="SHOW">Show</option>
          <option value="ANNUAL">Annual</option>
          <option value="BANQUET">Banquet</option>
          <option value="BENEFIT">Benefit</option>
          <option value="BREAKFAST">Breakfast</option>
          <option value="CASINO">Casino</option>
          <option value="TOURNAMENT">Tournament</option>
          <option value="RACE">Race</option>
        </select>
      </div>
      <div class="fg"><label>Prospect Tier</label>
        <select id="fTier">
          <option value="">All Tiers</option>
          <option value="A+">A+ (Top Prospects)</option>
          <option value="A">A (High Value)</option>
          <option value="B+">B+ (Good Prospects)</option>
          <option value="C">C (Standard)</option>
        </select>
      </div>
      <div class="fg"><label>Event Name Search</label><input type="text" id="fEventKw" placeholder="Free text in Event1/Event2 name"></div>
      <div class="fg"><label>Mission/Activity Keyword</label><input type="text" id="fMissionKw" placeholder="e.g. children, arts, health"></div>
    </div>
    <p style="font-size:10px;color:#64748b;margin-top:6px;">Quick filters (matches Event1Keyword / Event2Keyword):</p>
    <div class="checkboxes">
      <label><input type="checkbox" id="fAuction"> Auction</label>
      <label><input type="checkbox" id="fGala"> Gala</label>
      <label><input type="checkbox" id="fGolf"> Golf</label>
      <label><input type="checkbox" id="fDinner"> Dinner</label>
      <label><input type="checkbox" id="fBall"> Ball</label>
      <label><input type="checkbox" id="fRaffle"> Raffle</label>
      <label><input type="checkbox" id="fBenefit"> Benefit</label>
      <label><input type="checkbox" id="fFundraiser"> Fundraiser</label>
      <label><input type="checkbox" id="fFestival"> Festival</label>
      <label><input type="checkbox" id="fRun"> Run/Race</label>
      <label><input type="checkbox" id="fArt"> Art</label>
      <label><input type="checkbox" id="fTournament"> Tournament</label>
      <label><input type="checkbox" id="fCasino"> Casino</label>
      <label><input type="checkbox" id="fShow"> Show</label>
      <label><input type="checkbox" id="fNight"> Night</label>
    </div>
    <div class="checkboxes" style="margin-top:8px;">
      <label><input type="checkbox" id="fWebsite" checked> Has Website</label>
    </div>

    <div class="section-title">Settings</div>
    <div class="filter-grid">
      <div class="fg"><label>Max Results</label>
        <select id="fLimit">
          <option value="50">50</option><option value="100" selected>100</option>
          <option value="200">200</option><option value="500">500</option>
          <option value="1000">1,000</option><option value="2000">2,000</option>
          <option value="5000">5,000</option><option value="10000">10,000</option>
        </select>
      </div>
      <div class="fg"></div><div class="fg"></div><div class="fg"></div>
    </div>

    <div class="section-title" style="cursor:pointer;" onclick="document.getElementById('financialFilters').style.display=document.getElementById('financialFilters').style.display==='none'?'':'none';this.querySelector('span').textContent=document.getElementById('financialFilters').style.display==='none'?'+ Show':'- Hide';">Financial Filters <span style="font-size:10px;color:#ffd900;">+ Show</span></div>
    <div id="financialFilters" style="display:none;">
    <div class="section-title" style="border-top:none;margin-top:0;padding-top:0;">Organization</div>
    <div class="filter-grid">
      <div class="fg"><label>Total Revenue</label><select id="fTotalRevenue" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Gross Receipts</label><select id="fGrossReceipts" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Net Income</label><select id="fNetIncome" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Total Assets</label><select id="fTotalAssets" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Contributions Received</label><select id="fContributions" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Program Service Revenue</label><select id="fProgramRevenue" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Fundraising Gross Income</label><select id="fFundraisingIncome" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Fundraising Expenses</label><select id="fFundraisingExpenses" class="amount-select"><option value="">Any</option></select></div>
    </div>

    <div class="section-title">Financial Filters (Event 1)</div>
    <div class="filter-grid">
      <div class="fg"><label>Event 1 Gross Receipts</label><select id="fE1Receipts" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Event 1 Contributions</label><select id="fE1Contrib" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Event 1 Gross Revenue</label><select id="fE1Revenue" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Event 1 Net Income</label><select id="fE1Net" class="amount-select"><option value="">Any</option></select></div>
    </div>

    <div class="section-title">Financial Filters (Event 2)</div>
    <div class="filter-grid">
      <div class="fg"><label>Event 2 Gross Receipts</label><select id="fE2Receipts" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Event 2 Contributions</label><select id="fE2Contrib" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"><label>Event 2 Gross Revenue</label><select id="fE2Revenue" class="amount-select"><option value="">Any</option></select></div>
      <div class="fg"></div>
    </div>
    </div><!-- /financialFilters -->

    </div><!-- /filterBody -->

    <div class="controls">
      <button class="btn-primary" onclick="searchIRS()">Search Database</button>
      <button class="btn-send" id="sendBtn" disabled onclick="sendToFinder()">Add Selected to Queue</button>
      <button class="btn-send" id="goBtn" style="display:none;" onclick="window.location.href='/'">Go to Auction Finder</button>
      <span class="result-count" id="resultCount"></span>
      <span class="result-count" id="queueCount" style="color:#ffd900;"></span>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead><tr>
        <th><input type="checkbox" id="selectAll" onchange="toggleAll()"></th>
        <th>Organization</th><th>Website</th><th>City, State</th><th>Region</th>
        <th>Revenue</th><th>Fundraising</th>
        <th>Type</th><th>Tier</th><th>Event 1</th><th>Event 2</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<script>
const RANGES = [
  {v:'1-100k',l:'$1 - $100K'},
  {v:'100k-500k',l:'$100K - $500K'},
  {v:'500k-1m',l:'$500K - $1M'},
  {v:'1m-2m',l:'$1M - $2M'},
  {v:'2m-5m',l:'$2M - $5M'},
  {v:'5m+',l:'$5M+'}
];

const TAG_COLORS = {
  GALA:'gala', AUCTION:'auction', GOLF:'golf', DINNER:'dinner',
  BALL:'ball', RAFFLE:'raffle', RUN:'run', ART:'art',
  FESTIVAL:'other', FUNDRAISER:'other', BENEFIT:'other', SALE:'other',
  SHOW:'other', NIGHT:'other', CASINO:'other', TOURNAMENT:'other',
  WALK:'run', RACE:'run', LUNCHEON:'dinner', BANQUET:'dinner',
  BREAKFAST:'dinner', CELEBRATION:'gala', EVENT:'other', ANNUAL:'other',
  FAIR:'other', BAZAAR:'other', CONCERT:'art', EXHIBIT:'art', MARKET:'other'
};

document.querySelectorAll('.amount-select').forEach(sel => {
  RANGES.forEach(r => { const o=document.createElement('option'); o.value=r.v; o.textContent=r.l; sel.appendChild(o); });
});

fetch('/api/irs/states').then(r=>r.json()).then(states => {
  const sel=document.getElementById('fState');
  states.forEach(s => { const o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); });
});

// Show existing queue count on load
(function(){
  const q=sessionStorage.getItem('irs_nonprofits');
  if(q){
    const n=q.split('\\n').filter(s=>s.trim()).length;
    if(n>0){
      document.getElementById('queueCount').textContent=n+' in queue';
      document.getElementById('goBtn').style.display='';
    }
  }
})();

function fmt(n) { return (n===null||n===undefined)?'-':'$'+Number(n).toLocaleString(); }

function tagHtml(kw) {
  if(!kw) return '';
  const cls=TAG_COLORS[kw.toUpperCase()]||'other';
  return '<span class="tag '+cls+'">'+kw+'</span>';
}

function tierHtml(t) {
  if(!t) return '-';
  const cls = t==='A+'?'aplus':t==='A'?'a':t==='B+'?'bplus':'c';
  return '<span class="tier '+cls+'">'+t+'</span>';
}

function searchIRS() {
  const body = {
    name: document.getElementById('fName').value,
    state: document.getElementById('fState').value,
    region: document.getElementById('fRegion').value,
    city: document.getElementById('fCity').value,
    primary_event_type: document.getElementById('fPrimaryType').value,
    prospect_tier: document.getElementById('fTier').value,
    event_keyword: document.getElementById('fEventKw').value,
    mission_keyword: document.getElementById('fMissionKw').value,
    limit: document.getElementById('fLimit').value,
    has_auction: document.getElementById('fAuction').checked,
    has_gala: document.getElementById('fGala').checked,
    has_raffle: document.getElementById('fRaffle').checked,
    has_ball: document.getElementById('fBall').checked,
    has_dinner: document.getElementById('fDinner').checked,
    has_benefit: document.getElementById('fBenefit').checked,
    has_tournament: document.getElementById('fTournament').checked,
    has_golf: document.getElementById('fGolf').checked,
    has_fundraiser: document.getElementById('fFundraiser').checked,
    has_festival: document.getElementById('fFestival').checked,
    has_run: document.getElementById('fRun').checked,
    has_art: document.getElementById('fArt').checked,
    has_casino: document.getElementById('fCasino').checked,
    has_show: document.getElementById('fShow').checked,
    has_night: document.getElementById('fNight').checked,
    has_website: document.getElementById('fWebsite').checked,
    total_revenue: document.getElementById('fTotalRevenue').value,
    gross_receipts: document.getElementById('fGrossReceipts').value,
    net_income: document.getElementById('fNetIncome').value,
    total_assets: document.getElementById('fTotalAssets').value,
    contributions: document.getElementById('fContributions').value,
    program_revenue: document.getElementById('fProgramRevenue').value,
    fundraising_income: document.getElementById('fFundraisingIncome').value,
    fundraising_expenses: document.getElementById('fFundraisingExpenses').value,
    event1_receipts: document.getElementById('fE1Receipts').value,
    event1_contributions: document.getElementById('fE1Contrib').value,
    event1_revenue: document.getElementById('fE1Revenue').value,
    event1_net: document.getElementById('fE1Net').value,
    event2_receipts: document.getElementById('fE2Receipts').value,
    event2_contributions: document.getElementById('fE2Contrib').value,
    event2_revenue: document.getElementById('fE2Revenue').value,
  };

  document.getElementById('resultCount').textContent='Searching...';

  fetch('/api/irs/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json()).then(data => {
      if(data.error){alert(data.error);return;}
      document.getElementById('resultCount').textContent=data.count+' results';
      const tbody=document.getElementById('tbody');
      tbody.innerHTML='';
      data.results.forEach(r => {
        const tr=document.createElement('tr');
        const kw1=r.Event1Keyword||'';
        const kw2=r.Event2Keyword||'';
        const ptype=r.PrimaryEventType||'';
        const tags=[];
        if(ptype) tags.push(tagHtml(ptype));
        if(kw1 && kw1!==ptype) tags.push(tagHtml(kw1));
        if(kw2 && kw2!==ptype && kw2!==kw1) tags.push(tagHtml(kw2));
        const ws=r.Website?r.Website.toLowerCase().replace(/^https?:\\/\\//,'').replace(/\\/$/,''):'';
        const wsUrl=ws?(ws.startsWith('http')?ws:'https://'+ws):'';
        tr.innerHTML=
          '<td class="ck"><input type="checkbox" data-website="'+ws+'" data-name="'+(r.OrganizationName||'').replace(/"/g,'&quot;')+'" onchange="updateSendBtn()"></td>'+
          '<td title="'+(r.OrganizationName||'')+'">'+(r.OrganizationName||'-')+'</td>'+
          '<td>'+(wsUrl?'<a class="ws" href="'+wsUrl+'" target="_blank">'+ws+'</a>':'-')+'</td>'+
          '<td>'+(r.PhysicalCity||'')+', '+(r.PhysicalState||'')+'</td>'+
          '<td>'+(r.Region5||'-')+'</td>'+
          '<td>'+fmt(r.TotalRevenue)+'</td>'+
          '<td>'+fmt(r.FundraisingGrossIncome)+'</td>'+
          '<td>'+(tags.join(' ')||'-')+'</td>'+
          '<td>'+tierHtml(r.ProspectTier)+'</td>'+
          '<td title="'+(r.Event1Name||'')+'">'+(r.Event1Name||'-')+'</td>'+
          '<td title="'+(r.Event2Name||'')+'">'+(r.Event2Name||'-')+'</td>';
        tbody.appendChild(tr);
      });
    });
}

function toggleAll(){
  const c=document.getElementById('selectAll').checked;
  document.querySelectorAll('#tbody input[type=checkbox]').forEach(cb=>{cb.checked=c;});
  updateSendBtn();
}

function updateSendBtn(){
  const n=document.querySelectorAll('#tbody input[type=checkbox]:checked').length;
  const btn=document.getElementById('sendBtn');
  btn.disabled=n===0;
  btn.textContent=n>0?'Send '+n+' to Auction Finder':'Send Selected to Auction Finder';
}

function sendToFinder(){
  const sel=[];
  document.querySelectorAll('#tbody input[type=checkbox]:checked').forEach(cb=>{
    sel.push(cb.dataset.website||cb.dataset.name);
  });
  if(!sel.length)return;
  const existing=sessionStorage.getItem('irs_nonprofits')||'';
  const combined=existing?(existing+'\\n'+sel.join('\\n')):sel.join('\\n');
  const unique=[...new Set(combined.split('\\n').filter(s=>s.trim()))];
  sessionStorage.setItem('irs_nonprofits',unique.join('\\n'));
  document.getElementById('queueCount').textContent=unique.length+' in queue';
  document.getElementById('goBtn').style.display='';
  document.getElementById('selectAll').checked=false;
  document.querySelectorAll('#tbody input[type=checkbox]').forEach(cb=>{cb.checked=false;});
  updateSendBtn();
}

function toggleFilters(){
  const body=document.getElementById('filterBody');
  const btn=document.getElementById('toggleBtn');
  if(body.style.display==='none'){
    body.style.display='';
    btn.textContent='Hide Filters';
  } else {
    body.style.display='none';
    btn.textContent='Show Filters';
  }
}
</script>
</div>
</body>
</html>"""


RESULTS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Results</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 900px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 18px; color: #d4d4d4; margin-bottom: 8px; }
  .subtitle { font-size: 12px; color: #737373; margin-bottom: 24px; }
  .job-card { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .job-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .job-date { font-size: 13px; color: #a3a3a3; }
  .job-status { font-size: 12px; font-weight: 700; text-transform: uppercase; }
  .job-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .js { text-align: center; background: #000000; border-radius: 8px; padding: 10px; }
  .jn { font-size: 20px; font-weight: 700; display: block; }
  .jl { font-size: 10px; color: #a3a3a3; text-transform: uppercase; }
  .dl-btns { display: flex; gap: 8px; }
  .dl-btn { display: inline-block; padding: 8px 20px; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; color: #000; }
  .dl-btn.csv { background: #ffd900; }
  .dl-btn.json { background: #7c3aed; color: #fff; }
  .dl-btn.xlsx { background: #2563eb; color: #fff; }
  .dl-btn:hover { filter: brightness(0.85); }
  .empty-state { text-align: center; padding: 60px 24px; color: #737373; font-size: 14px; }
  .empty-state p { margin-bottom: 8px; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h2>Search Results</h2>
    <a href="/tools/merge" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#eab308;text-decoration:none;font-size:13px;font-weight:600;transition:all 0.2s;">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>
      Merge Files
    </a>
  </div>
  <p class="subtitle">Your past search results are stored for 180 days. Download in CSV, JSON, or XLSX format.</p>
  {{JOB_CARDS}}
</div>
</div>
</body>
</html>"""

MERGE_TOOL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - File Merger</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 760px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 18px; color: #d4d4d4; margin-bottom: 4px; }
  .subtitle { font-size: 12px; color: #737373; margin-bottom: 24px; }

  .mode-tabs { display:flex; background:#1a1a1a; border-radius:10px; padding:3px; margin-bottom:20px; border:1px solid #262626; }
  .mode-tab { flex:1; padding:10px 14px; border:none; background:transparent; font-family:inherit; font-size:13px; font-weight:600; color:#737373; border-radius:8px; cursor:pointer; transition:all 0.2s; display:flex; align-items:center; justify-content:center; gap:6px; }
  .mode-tab.active-csv { background:#eab308; color:#000; }
  .mode-tab.active-json { background:#7c3aed; color:#fff; }
  .mode-tab:not([class*="active-"]):hover { color:#f5f5f5; background:#262626; }

  .drop-zone { border:2px dashed #333; border-radius:12px; padding:36px 20px; text-align:center; cursor:pointer; transition:all 0.2s; background:#1a1a1a; position:relative; }
  .drop-zone:hover, .drop-zone.dragover { border-color:#eab308; background:#1a1a0a; }
  .drop-zone-icon { width:48px; height:48px; border-radius:12px; display:flex; align-items:center; justify-content:center; margin:0 auto 12px; font-size:20px; background:#262626; color:#eab308; }
  .mode-json .drop-zone-icon { color:#7c3aed; }
  .drop-zone h3 { font-size:14px; font-weight:600; margin-bottom:4px; }
  .drop-zone p { font-size:12px; color:#737373; }
  .drop-zone input[type="file"] { position:absolute; inset:0; opacity:0; cursor:pointer; }

  .file-list-section { margin-top:16px; }
  .file-list-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
  .file-list-header h3 { font-size:13px; font-weight:600; color:#a3a3a3; }
  .file-count { font-size:11px; font-weight:600; padding:2px 8px; border-radius:12px; background:#262626; color:#eab308; }
  .file-list { display:flex; flex-direction:column; gap:6px; max-height:280px; overflow-y:auto; }
  .file-list::-webkit-scrollbar { width:4px; }
  .file-list::-webkit-scrollbar-thumb { background:#333; border-radius:4px; }
  .file-item { display:flex; align-items:center; gap:10px; padding:10px 12px; background:#1a1a1a; border:1px solid #262626; border-radius:8px; }
  .file-icon { width:32px; height:32px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:13px; background:#262626; color:#eab308; flex-shrink:0; }
  .mode-json .file-icon { color:#7c3aed; }
  .file-info { flex:1; min-width:0; }
  .file-name { font-size:13px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .file-meta { font-size:11px; color:#737373; }
  .file-remove { width:28px; height:28px; border-radius:6px; border:none; background:transparent; color:#737373; cursor:pointer; display:flex; align-items:center; justify-content:center; font-size:12px; flex-shrink:0; }
  .file-remove:hover { background:#3a1a1a; color:#f87171; }

  .actions { display:flex; gap:8px; margin-top:16px; }
  .btn { flex:1; padding:12px 16px; border:none; border-radius:8px; font-family:inherit; font-size:14px; font-weight:600; cursor:pointer; transition:all 0.2s; display:flex; align-items:center; justify-content:center; gap:6px; }
  .btn:disabled { opacity:0.4; cursor:not-allowed; }
  .btn-merge-csv { background:#eab308; color:#000; }
  .btn-merge-csv:not(:disabled):hover { filter:brightness(1.1); }
  .btn-merge-json { background:#7c3aed; color:#fff; }
  .btn-merge-json:not(:disabled):hover { filter:brightness(1.1); }
  .btn-clear { background:#1a1a1a; color:#a3a3a3; border:1px solid #333; flex:0 0 auto; padding:12px 14px; }
  .btn-clear:not(:disabled):hover { background:#3a1a1a; color:#f87171; border-color:#f87171; }

  .status-bar { margin-top:14px; padding:12px 14px; border-radius:8px; font-size:13px; font-weight:500; display:none; align-items:center; gap:8px; }
  .status-bar.success { display:flex; background:#1a2a1a; color:#4ade80; border:1px solid #2a3a2a; }
  .status-bar.error { display:flex; background:#3a1a1a; color:#f87171; border:1px solid #4a2a2a; }

  .download-area { margin-top:14px; display:none; }
  .download-area.visible { display:block; }
  .btn-download { width:100%; padding:14px 16px; border:2px dashed #eab308; border-radius:8px; background:#1a1a0a; color:#eab308; font-family:inherit; font-size:14px; font-weight:600; cursor:pointer; transition:all 0.2s; display:flex; align-items:center; justify-content:center; gap:8px; text-decoration:none; }
  .btn-download:hover { background:#eab308; color:#000; border-style:solid; }

  .preview-section { margin-top:16px; display:none; }
  .preview-section.visible { display:block; }
  .preview-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
  .preview-header h3 { font-size:13px; font-weight:600; color:#a3a3a3; }
  .preview-toggle { font-size:12px; color:#737373; background:none; border:none; cursor:pointer; font-family:inherit; font-weight:500; }
  .preview-toggle:hover { color:#f5f5f5; }
  .preview-box { background:#0a0a0a; border:1px solid #262626; border-radius:8px; padding:12px; max-height:240px; overflow:auto; font-size:11px; line-height:1.6; color:#a3a3a3; white-space:pre; word-break:break-all; }
  .preview-box::-webkit-scrollbar { width:4px; height:4px; }
  .preview-box::-webkit-scrollbar-thumb { background:#333; border-radius:4px; }

  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.7); display:flex; align-items:center; justify-content:center; z-index:1000; padding:20px; }
  .modal-box { background:#1a1a1a; border:1px solid #333; border-radius:12px; padding:20px; max-width:360px; width:100%; }
  .modal-box h3 { font-size:15px; font-weight:600; margin-bottom:6px; }
  .modal-box p { font-size:13px; color:#a3a3a3; margin-bottom:16px; line-height:1.5; }
  .modal-actions { display:flex; gap:8px; justify-content:flex-end; }
  .modal-btn { padding:8px 16px; border-radius:6px; border:none; font-family:inherit; font-size:13px; font-weight:600; cursor:pointer; }
  .modal-btn-cancel { background:#262626; color:#a3a3a3; }
  .modal-btn-cancel:hover { background:#333; }
  .modal-btn-confirm { background:#f87171; color:#fff; }
  .modal-btn-confirm:hover { filter:brightness(1.1); }

  @media (max-width: 480px) {
    .container { padding:16px; }
    .actions { flex-direction:column; }
    .btn-clear { flex:1; }
  }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h2>File Merger</h2>
  <p class="subtitle">Upload multiple CSV or JSON result files and merge them into a single download.</p>

  <div class="mode-tabs">
    <button class="mode-tab active-csv" id="tabCsv" onclick="switchMode('csv')">CSV Merger</button>
    <button class="mode-tab" id="tabJson" onclick="switchMode('json')">JSON Merger</button>
  </div>

  <div class="drop-zone mode-csv" id="dropZone">
    <input type="file" id="fileInput" multiple accept=".csv">
    <div class="drop-zone-icon">&#8593;</div>
    <h3 id="dropTitle">Drop CSV files here or click to browse</h3>
    <p id="dropHint">Up to 20 files &middot; Same column headers required</p>
  </div>

  <div class="file-list-section" id="fileListSection" style="display:none;">
    <div class="file-list-header">
      <h3>Uploaded Files</h3>
      <span class="file-count" id="fileCount">0 / 20</span>
    </div>
    <div class="file-list" id="fileList"></div>
  </div>

  <div class="actions" id="actionsArea" style="display:none;">
    <button class="btn btn-merge-csv" id="btnMerge" onclick="mergeFiles()" disabled>
      <span id="mergeLabel">Merge CSV Files</span>
    </button>
    <button class="btn btn-clear" id="btnClear" onclick="confirmClear()">Clear</button>
  </div>

  <div class="status-bar" id="statusBar">
    <span id="statusIcon"></span>
    <span class="status-text" id="statusText"></span>
  </div>

  <div class="download-area" id="downloadArea">
    <a class="btn-download" id="downloadLink" href="#" download="merged.csv">
      <span id="downloadLabel">Download Merged CSV</span>
    </a>
  </div>

  <div class="preview-section" id="previewSection">
    <div class="preview-header">
      <h3 id="previewTitle">Preview (first 50 rows)</h3>
      <button class="preview-toggle" id="previewToggle" onclick="togglePreview()">Hide</button>
    </div>
    <div class="preview-box" id="previewBox"></div>
  </div>
</div>
</div>

<script>
(function() {
  var currentMode = 'csv';
  var csvFiles = [];
  var jsonFiles = [];
  var mergedBlobUrl = null;

  var dropZone = document.getElementById('dropZone');
  var fileInput = document.getElementById('fileInput');
  var fileListSection = document.getElementById('fileListSection');
  var fileList = document.getElementById('fileList');
  var fileCount = document.getElementById('fileCount');
  var actionsArea = document.getElementById('actionsArea');
  var btnMerge = document.getElementById('btnMerge');
  var mergeLabel = document.getElementById('mergeLabel');
  var statusBar = document.getElementById('statusBar');
  var statusIcon = document.getElementById('statusIcon');
  var statusText = document.getElementById('statusText');
  var downloadArea = document.getElementById('downloadArea');
  var downloadLink = document.getElementById('downloadLink');
  var downloadLabel = document.getElementById('downloadLabel');
  var previewSection = document.getElementById('previewSection');
  var previewBox = document.getElementById('previewBox');
  var previewToggle = document.getElementById('previewToggle');
  var tabCsv = document.getElementById('tabCsv');
  var tabJson = document.getElementById('tabJson');
  var dropTitle = document.getElementById('dropTitle');
  var dropHint = document.getElementById('dropHint');

  window.switchMode = function(mode) {
    currentMode = mode;
    clearResults();
    if (mode === 'csv') {
      tabCsv.className = 'mode-tab active-csv';
      tabJson.className = 'mode-tab';
      dropZone.className = 'drop-zone mode-csv';
      fileInput.accept = '.csv';
      dropTitle.textContent = 'Drop CSV files here or click to browse';
      dropHint.textContent = 'Up to 20 files \\u00B7 Same column headers required';
      mergeLabel.textContent = 'Merge CSV Files';
      btnMerge.className = 'btn btn-merge-csv';
    } else {
      tabCsv.className = 'mode-tab';
      tabJson.className = 'mode-tab active-json';
      dropZone.className = 'drop-zone mode-json';
      fileInput.accept = '.json';
      dropTitle.textContent = 'Drop JSON files here or click to browse';
      dropHint.textContent = 'Up to 20 files \\u00B7 Identical JSON structure required';
      mergeLabel.textContent = 'Merge JSON Files';
      btnMerge.className = 'btn btn-merge-json';
    }
    renderFileList();
  };

  function getFiles() { return currentMode === 'csv' ? csvFiles : jsonFiles; }
  function setFiles(f) { if (currentMode === 'csv') csvFiles = f; else jsonFiles = f; }

  function addFiles(newFiles) {
    var current = getFiles();
    var remaining = 20 - current.length;
    if (remaining <= 0) { showStatus('error', 'Maximum 20 files. Remove some first.'); return; }
    var ext = currentMode === 'csv' ? '.csv' : '.json';
    var accepted = [], rejected = 0;
    for (var i = 0; i < newFiles.length && accepted.length < remaining; i++) {
      var f = newFiles[i];
      if (f.name.toLowerCase().endsWith(ext)) {
        var dup = false;
        for (var j = 0; j < current.length; j++) { if (current[j].name === f.name) { dup = true; break; } }
        if (!dup) accepted.push(f); else rejected++;
      } else { rejected++; }
    }
    if (rejected > 0) showStatus('error', rejected + ' file(s) skipped (wrong type or duplicate).');
    if (accepted.length > 0) { setFiles(current.concat(accepted)); clearResults(); renderFileList(); }
  }

  fileInput.addEventListener('change', function(e) { if (e.target.files.length > 0) { addFiles(Array.from(e.target.files)); e.target.value = ''; } });
  dropZone.addEventListener('dragover', function(e) { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', function(e) { e.preventDefault(); dropZone.classList.remove('dragover'); });
  dropZone.addEventListener('drop', function(e) { e.preventDefault(); dropZone.classList.remove('dragover'); if (e.dataTransfer.files.length > 0) addFiles(Array.from(e.dataTransfer.files)); });

  function fmtSize(b) { if (b < 1024) return b+' B'; if (b < 1048576) return (b/1024).toFixed(1)+' KB'; return (b/1048576).toFixed(1)+' MB'; }

  function renderFileList() {
    var files = getFiles();
    if (files.length === 0) { fileListSection.style.display='none'; actionsArea.style.display='none'; return; }
    fileListSection.style.display = 'block';
    actionsArea.style.display = 'flex';
    fileCount.textContent = files.length + ' / 20';
    btnMerge.disabled = files.length < 2;
    fileListSection.className = 'file-list-section ' + (currentMode==='csv'?'mode-csv':'mode-json');
    var html = '';
    for (var i = 0; i < files.length; i++) {
      html += '<div class="file-item"><div class="file-icon">'+(currentMode==='csv'?'CSV':'{ }')+'</div><div class="file-info"><div class="file-name">'+escapeHtml(files[i].name)+'</div><div class="file-meta">'+fmtSize(files[i].size)+'</div></div><button class="file-remove" onclick="removeFile('+i+')" title="Remove">&times;</button></div>';
    }
    fileList.innerHTML = html;
  }

  window.removeFile = function(i) { var f=getFiles(); f.splice(i,1); setFiles(f); clearResults(); renderFileList(); };

  window.confirmClear = function() {
    var files = getFiles(); if (files.length===0) return;
    var ov = document.createElement('div'); ov.className='modal-overlay';
    ov.innerHTML='<div class="modal-box"><h3>Clear All Files?</h3><p>Remove all '+files.length+' '+currentMode.toUpperCase()+' file(s) and results.</p><div class="modal-actions"><button class="modal-btn modal-btn-cancel" id="mc">Cancel</button><button class="modal-btn modal-btn-confirm" id="mk">Clear All</button></div></div>';
    document.body.appendChild(ov);
    document.getElementById('mc').onclick=function(){ov.remove();};
    document.getElementById('mk').onclick=function(){ov.remove();setFiles([]);clearResults();renderFileList();};
    ov.addEventListener('click',function(e){if(e.target===ov)ov.remove();});
  };

  function readFile(f) { return new Promise(function(ok,err){ var r=new FileReader(); r.onload=function(){ok(r.result);}; r.onerror=function(){err(new Error('Failed: '+f.name));}; r.readAsText(f); }); }

  function parseCSV(text) {
    var rows=[],row=[],field='',inQ=false,i=0;
    while(i<text.length){var c=text[i];if(inQ){if(c==='"'){if(i+1<text.length&&text[i+1]==='"'){field+='"';i+=2;}else{inQ=false;i++;}}else{field+=c;i++;}}else{if(c==='"'){inQ=true;i++;}else if(c===','){row.push(field);field='';i++;}else if(c==='\\r'){if(i+1<text.length&&text[i+1]==='\\n')i++;row.push(field);field='';rows.push(row);row=[];i++;}else if(c==='\\n'){row.push(field);field='';rows.push(row);row=[];i++;}else{field+=c;i++;}}}
    if(field.length>0||row.length>0){row.push(field);rows.push(row);} return rows;
  }
  function rowToCSV(row){return row.map(function(c){if(c.indexOf(',')!==-1||c.indexOf('"')!==-1||c.indexOf('\\n')!==-1)return '"'+c.replace(/"/g,'""')+'"';return c;}).join(',');}

  window.mergeFiles = async function() {
    var files=getFiles(); if(files.length<2)return;
    clearResults(); btnMerge.disabled=true; mergeLabel.textContent='Merging...';
    try { if(currentMode==='csv') await mergeCSV(files); else await mergeJSON(files); } catch(e) { showStatus('error',e.message||'Merge error.'); }
    mergeLabel.textContent=currentMode==='csv'?'Merge CSV Files':'Merge JSON Files'; btnMerge.disabled=false;
  };

  async function mergeCSV(files) {
    var all=[]; for(var i=0;i<files.length;i++){var t=await readFile(files[i]);all.push({name:files[i].name,text:t});}
    var first=parseCSV(all[0].text); if(first.length===0)throw new Error('First file is empty.');
    var hdr=first[0],hdrStr=hdr.join(',').toLowerCase().trim();
    var merged=first.slice(1).filter(function(r){return r.length>1||(r.length===1&&r[0].trim()!=='');});
    for(var j=1;j<all.length;j++){var rows=parseCSV(all[j].text);if(rows.length===0)continue;if(rows[0].join(',').toLowerCase().trim()!==hdrStr)throw new Error('Header mismatch in "'+all[j].name+'"');var dr=rows.slice(1).filter(function(r){return r.length>1||(r.length===1&&r[0].trim()!=='');});merged=merged.concat(dr);}
    var lines=[rowToCSV(hdr)];for(var k=0;k<merged.length;k++)lines.push(rowToCSV(merged[k]));var out=lines.join('\\n');
    showStatus('success','Merged '+files.length+' files \\u2014 '+merged.length+' total rows.');
    createDownload(out,'merged.csv','text/csv','Download Merged CSV ('+merged.length+' rows)');
    showPreview(out,'csv',merged.length);
  }

  async function mergeJSON(files) {
    var all=[];for(var i=0;i<files.length;i++){var t=await readFile(files[i]);var p;try{p=JSON.parse(t);}catch(e){throw new Error('Invalid JSON in "'+files[i].name+'"');}if(Array.isArray(p))all=all.concat(p);else if(typeof p==='object'&&p!==null)all.push(p);else throw new Error('"'+files[i].name+'" unsupported JSON.');}
    var out=JSON.stringify(all,null,2);
    showStatus('success','Merged '+files.length+' files \\u2014 '+all.length+' total items.');
    createDownload(out,'merged.json','application/json','Download Merged JSON ('+all.length+' items)');
    showPreview(out,'json',all.length);
  }

  function createDownload(content,filename,mime,label){if(mergedBlobUrl)URL.revokeObjectURL(mergedBlobUrl);var blob=new Blob([content],{type:mime});mergedBlobUrl=URL.createObjectURL(blob);downloadLink.href=mergedBlobUrl;downloadLink.download=filename;downloadLabel.textContent=label;downloadArea.className='download-area visible';}

  function showPreview(content,type,total){
    var lines,previewTitle=document.getElementById('previewTitle');
    if(type==='csv'){lines=content.split('\\n');var s=Math.min(lines.length,51);previewBox.textContent=lines.slice(0,s).join('\\n');previewTitle.textContent='Preview (first '+Math.min(50,total)+' of '+total+' rows)';}
    else{lines=content.split('\\n');if(lines.length>200){previewBox.textContent=lines.slice(0,200).join('\\n')+'\\n... ('+(lines.length-200)+' more lines)';}else{previewBox.textContent=content;}previewTitle.textContent='Preview ('+total+' items)';}
    previewSection.className='preview-section visible';previewToggle.textContent='Hide';
  }

  window.togglePreview=function(){if(previewBox.style.display==='none'){previewBox.style.display='';previewToggle.textContent='Hide';}else{previewBox.style.display='none';previewToggle.textContent='Show';}};
  function showStatus(t,m){statusBar.className='status-bar '+t;statusIcon.textContent=t==='success'?'\\u2713':'\\u2717';statusText.textContent=m;}
  function clearResults(){statusBar.className='status-bar';statusBar.style.display='';downloadArea.className='download-area';previewSection.className='preview-section';if(mergedBlobUrl){URL.revokeObjectURL(mergedBlobUrl);mergedBlobUrl=null;}}
  function escapeHtml(s){var d=document.createElement('div');d.appendChild(document.createTextNode(s));return d.innerHTML;}
})();
</script>
</body>
</html>"""

ANALYZER_TOOL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - JSON Field Analyzer</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: 'SF Mono','Consolas',monospace; background:#121212; color:#f5f5f5; min-height:100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width:960px; margin:0 auto; padding:24px 14px 80px; }
  .page-header { text-align:center; margin-bottom:28px; }
  .page-header h2 { font-size:20px; color:#d4d4d4; }
  .page-header h2 .hl { color:#eab308; }
  .page-header p { color:#737373; font-size:13px; margin-top:4px; }
  .dropzone { border:2px dashed #333; border-radius:12px; padding:42px 24px; text-align:center; cursor:pointer; transition:all .25s; background:#1a1a1a; position:relative; }
  .dropzone:hover, .dropzone.over { border-color:#eab308; background:rgba(234,179,8,0.04); }
  .dz-icon { width:50px; height:50px; margin:0 auto 12px; background:#262626; border-radius:13px; display:flex; align-items:center; justify-content:center; font-size:20px; color:#eab308; }
  .dropzone h3 { font-weight:600; font-size:14px; color:#d4d4d4; }
  .dropzone .sub { color:#737373; font-size:12px; margin-top:3px; }
  .dropzone .cap { display:inline-block; margin-top:10px; font-family:'IBM Plex Mono',monospace; font-size:11px; color:#737373; background:#262626; padding:3px 10px; border-radius:6px; }
  .dropzone input { position:absolute; inset:0; opacity:0; cursor:pointer; }
  .file-tabs { display:flex; gap:6px; margin:16px 0 14px; overflow-x:auto; padding-bottom:4px; }
  .file-tab { display:flex; align-items:center; gap:6px; padding:7px 12px; border:1px solid #333; border-radius:8px; background:#1a1a1a; color:#a3a3a3; font-family:'IBM Plex Mono',monospace; font-size:12px; cursor:pointer; white-space:nowrap; transition:all .2s; flex-shrink:0; }
  .file-tab:hover { border-color:#eab308; color:#f5f5f5; }
  .file-tab.active { background:#eab308; color:#000; border-color:#eab308; }
  .file-tab .xtab { display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; font-size:11px; cursor:pointer; }
  .file-tab .xtab:hover { background:rgba(255,255,255,.2); }
  .file-tab:not(.active) .xtab:hover { background:#2E1010; color:#F87171; }
  .section { margin-bottom:18px; }
  .section-title { font-family:'IBM Plex Mono',monospace; font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:#737373; font-weight:600; margin-bottom:8px; }
  .stats-row { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
  .stat-card { background:#1a1a1a; border:1px solid #333; border-radius:10px; padding:14px 16px; }
  .stat-card .sc-label { font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:#737373; font-family:'IBM Plex Mono',monospace; font-weight:500; }
  .stat-card .sc-val { font-size:24px; font-weight:700; font-family:'IBM Plex Mono',monospace; margin-top:2px; }
  .sc-total .sc-val { color:#f5f5f5; }
  .sc-fields .sc-val { color:#eab308; }
  .sc-combos .sc-val { color:#A78BFA; }
  .sc-errors .sc-val { color:#F87171; }
  .ftable-wrap, .combo-wrap { background:#1a1a1a; border:1px solid #333; border-radius:12px; overflow:hidden; }
  .ftable-header, .combo-header { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border-bottom:1px solid #333; background:#0d0d0d; flex-wrap:wrap; gap:6px; }
  .ftable-header span, .combo-header span { font-family:'IBM Plex Mono',monospace; font-size:12px; color:#737373; }
  .sort-btns { display:flex; gap:3px; }
  .sort-btn { padding:4px 10px; border:1px solid #333; border-radius:6px; background:#262626; color:#737373; font-size:12px; font-weight:500; cursor:pointer; transition:all .2s; }
  .sort-btn:hover { color:#a3a3a3; border-color:#555; }
  .sort-btn.active { background:#eab308; color:#000; border-color:#eab308; }
  .ftable-body, .combo-body { max-height:55vh; overflow-y:auto; }
  .frow { display:grid; grid-template-columns:minmax(140px,1.2fr) 80px 80px 1fr; align-items:center; gap:10px; padding:10px 16px; border-bottom:1px solid #222; transition:background .12s; }
  .frow:last-child { border-bottom:none; }
  .frow:hover { background:#0d0d0d; }
  .frow-name { font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:500; color:#f5f5f5; word-break:break-all; }
  .frow-pop { font-family:'IBM Plex Mono',monospace; font-size:12px; color:#4ADE80; font-weight:600; }
  .frow-miss { font-family:'IBM Plex Mono',monospace; font-size:12px; color:#FB923C; font-weight:600; }
  .frow-bar { height:8px; border-radius:4px; background:#262626; overflow:hidden; }
  .frow-bar-fill { height:100%; border-radius:4px; transition:width .6s ease; }
  .frow-bar-fill.full { background:#4ADE80; }
  .frow-bar-fill.high { background:#eab308; }
  .frow-bar-fill.mid { background:#FBBF24; }
  .frow-bar-fill.low { background:#FB923C; }
  .frow-bar-fill.crit { background:#F87171; }
  .frow-head { background:#0d0d0d; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:#737373; font-weight:600; font-family:'IBM Plex Mono',monospace; padding:8px 16px; position:sticky; top:0; z-index:2; border-bottom:1px solid #333; }
  .combo-row { display:grid; grid-template-columns:1fr 70px 80px; gap:10px; align-items:center; padding:10px 16px; border-bottom:1px solid #222; transition:background .12s; }
  .combo-row:last-child { border-bottom:none; }
  .combo-row:hover { background:#0d0d0d; }
  .combo-row-head { background:#0d0d0d; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:#737373; font-weight:600; font-family:'IBM Plex Mono',monospace; padding:8px 16px; position:sticky; top:0; z-index:2; border-bottom:1px solid #333; }
  .combo-fields { display:flex; flex-wrap:wrap; gap:4px; }
  .combo-tag { font-family:'IBM Plex Mono',monospace; font-size:11px; padding:2px 7px; border-radius:5px; background:#2E1E0C; color:#FB923C; white-space:nowrap; }
  .combo-tag.full { background:#0E2818; color:#4ADE80; }
  .combo-count { font-family:'IBM Plex Mono',monospace; font-size:13px; font-weight:600; color:#f5f5f5; text-align:right; }
  .combo-pct { font-family:'IBM Plex Mono',monospace; font-size:12px; color:#737373; text-align:right; }
  .export-row { display:flex; gap:8px; margin-top:16px; flex-wrap:wrap; }
  .ebtn { padding:8px 16px; border:1px solid #333; border-radius:8px; background:#262626; color:#a3a3a3; font-size:13px; font-weight:500; cursor:pointer; transition:all .2s; display:inline-flex; align-items:center; gap:6px; }
  .ebtn:hover { border-color:#eab308; color:#eab308; }
  .ebtn.primary { background:#eab308; border-color:#eab308; color:#000; }
  .ebtn.primary:hover { filter:brightness(1.1); }
  .hidden { display:none !important; }
  @media(max-width:600px) {
    .stats-row { grid-template-columns:repeat(2,1fr); }
    .frow { grid-template-columns:minmax(100px,1fr) 60px 60px 80px; gap:6px; padding:8px 12px; }
    .combo-row { grid-template-columns:1fr 50px 60px; gap:6px; padding:8px 12px; }
    .export-row { flex-direction:column; }
    .ebtn { justify-content:center; }
  }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<main class="main-content">
<div class="container">
  <div class="page-header">
    <h2>JSON Field <span class="hl">Analyzer</span></h2>
    <p>Analyze field completeness across all records</p>
  </div>

  <div class="dropzone" id="dropzone">
    <div class="dz-icon">&#128269;</div>
    <h3>Drop JSON files here</h3>
    <p class="sub">or click to browse &mdash; up to 5 MB per file</p>
    <span class="cap">accepts .json arrays or objects</span>
    <input type="file" id="fileInput" accept=".json" multiple>
  </div>

  <div id="dashboard" class="hidden">
    <div class="file-tabs" id="fileTabs"></div>
    <div class="section">
      <div class="section-title">Overview</div>
      <div class="stats-row">
        <div class="stat-card sc-total"><div class="sc-label">Records</div><div class="sc-val" id="stRecords">0</div></div>
        <div class="stat-card sc-fields"><div class="sc-label">Unique Fields</div><div class="sc-val" id="stFields">0</div></div>
        <div class="stat-card sc-combos"><div class="sc-label">Missing Combos</div><div class="sc-val" id="stCombos">0</div></div>
        <div class="stat-card sc-errors"><div class="sc-label">Discarded Errors</div><div class="sc-val" id="stErrors">0</div></div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">Field Completeness <span id="fieldCountLabel" style="color:#a3a3a3;font-weight:400;"></span></div>
      <div class="ftable-wrap">
        <div class="ftable-header">
          <span>Populated vs Missing per field</span>
          <div class="sort-btns">
            <button class="sort-btn active" data-sort="name">A-Z</button>
            <button class="sort-btn" data-sort="pop-desc">Most filled</button>
            <button class="sort-btn" data-sort="miss-desc">Most empty</button>
          </div>
        </div>
        <div class="ftable-body" id="ftableBody"></div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">Missing Field Combinations <span id="comboCountLabel" style="color:#a3a3a3;font-weight:400;"></span></div>
      <div class="combo-wrap">
        <div class="combo-header"><span>Records grouped by which fields are empty</span></div>
        <div class="combo-body" id="comboBody"></div>
      </div>
    </div>
    <div class="export-row">
      <button class="ebtn" id="exportJsonBtn">Export Report JSON</button>
      <button class="ebtn" id="exportCsvBtn">Export Report CSV</button>
      <button class="ebtn primary" id="exportCompleteBtn">Download Complete Records</button>
    </div>
  </div>
</div>
</main>
<script>
(function(){
  "use strict";
  var MAX_SIZE=5*1024*1024;var files={};var activeFile=null;var currentSort="name";
  var dropzone=document.getElementById("dropzone");var fileInput=document.getElementById("fileInput");
  var dashboard=document.getElementById("dashboard");var fileTabs=document.getElementById("fileTabs");

  dropzone.addEventListener("dragover",function(e){e.preventDefault();dropzone.classList.add("over");});
  dropzone.addEventListener("dragleave",function(){dropzone.classList.remove("over");});
  dropzone.addEventListener("drop",function(e){e.preventDefault();dropzone.classList.remove("over");handleFiles(e.dataTransfer.files);});
  fileInput.addEventListener("change",function(e){handleFiles(e.target.files);e.target.value="";});

  function handleFiles(fl){
    for(var i=0;i<fl.length;i++){
      var f=fl[i];
      if(!f.name.endsWith(".json")){showToast(f.name+" is not a .json file");continue;}
      if(f.size>MAX_SIZE){showToast(f.name+" exceeds 5 MB limit");continue;}
      (function(file){
        var reader=new FileReader();
        reader.onload=function(ev){
          try{var raw=JSON.parse(ev.target.result);var arr=normalizeToArray(raw);
            if(arr.length===0){showToast(file.name+": no records found");return;}
            processFile(file.name,arr);
          }catch(err){showToast("Failed to parse "+file.name);}
        };reader.readAsText(file);
      })(f);
    }
  }

  function normalizeToArray(data){
    if(Array.isArray(data))return flattenObjects(data);
    if(data&&typeof data==="object"){return flattenObjects(Object.values(data));}
    return[];
  }
  function flattenObjects(arr){
    var out=[];for(var i=0;i<arr.length;i++){
      if(Array.isArray(arr[i])){for(var j=0;j<arr[i].length;j++){if(arr[i][j]&&typeof arr[i][j]==="object"&&!Array.isArray(arr[i][j]))out.push(arr[i][j]);}}
      else if(arr[i]&&typeof arr[i]==="object"){out.push(arr[i]);}
    }return out;
  }
  function isEmpty(val){
    if(val===undefined||val===null)return true;
    if(typeof val==="number")return false;
    if(typeof val==="boolean")return false;
    if(typeof val==="string"&&val.trim()==="")return true;
    return false;
  }
  var ERROR_MARKER="Error during research: poe.BotError:";
  function isErrorRecord(rec){if(!rec||!rec.event_summary)return false;return String(rec.event_summary).indexOf(ERROR_MARKER)!==-1;}

  function analyze(records){
    var allFields={};var i,j,key;
    for(i=0;i<records.length;i++){var keys=Object.keys(records[i]);for(j=0;j<keys.length;j++){allFields[keys[j]]=true;}}
    var fieldNames=Object.keys(allFields).sort();
    var fieldStats=[];
    for(i=0;i<fieldNames.length;i++){key=fieldNames[i];var populated=0;var missing=0;
      for(j=0;j<records.length;j++){if(isEmpty(records[j][key]))missing++;else populated++;}
      fieldStats.push({name:key,populated:populated,missing:missing});
    }
    var combos={};
    for(i=0;i<records.length;i++){var missingKeys=[];
      for(j=0;j<fieldNames.length;j++){if(isEmpty(records[i][fieldNames[j]]))missingKeys.push(fieldNames[j]);}
      var comboKey=missingKeys.length===0?"__ALL_COMPLETE__":missingKeys.join(" + ");
      if(!combos[comboKey])combos[comboKey]={fields:missingKeys,count:0};combos[comboKey].count++;
    }
    var comboList=[];for(key in combos){comboList.push(combos[key]);}
    comboList.sort(function(a,b){return b.count-a.count;});
    return{totalRecords:records.length,fieldNames:fieldNames,fieldStats:fieldStats,combos:comboList};
  }

  function processFile(name,records){
    var filtered=[];var errorCount=0;
    for(var i=0;i<records.length;i++){if(isErrorRecord(records[i])){errorCount++;}else{filtered.push(records[i]);}}
    if(errorCount>0){showToast("Discarded "+errorCount+" BotError record"+(errorCount!==1?"s":"")+" from "+name);}
    if(filtered.length===0){showToast(name+": no valid records after filtering");return;}
    var result=analyze(filtered);result.records=filtered;result.discardedErrors=errorCount;
    files[name]=result;activeFile=name;dashboard.classList.remove("hidden");renderTabs();renderDashboard();
  }

  function renderTabs(){
    fileTabs.innerHTML="";var names=Object.keys(files);
    for(var i=0;i<names.length;i++){(function(name){
      var tab=document.createElement("div");tab.className="file-tab"+(name===activeFile?" active":"");
      var lbl=document.createElement("span");lbl.textContent=name;tab.appendChild(lbl);
      var x=document.createElement("span");x.className="xtab";x.textContent="x";
      x.addEventListener("click",function(e){e.stopPropagation();removeFile(name);});tab.appendChild(x);
      tab.addEventListener("click",function(){activeFile=name;renderTabs();renderDashboard();});
      fileTabs.appendChild(tab);
    })(names[i]);}
  }
  function removeFile(name){delete files[name];var rem=Object.keys(files);
    if(rem.length===0){activeFile=null;dashboard.classList.add("hidden");return;}
    if(activeFile===name)activeFile=rem[0];renderTabs();renderDashboard();
  }

  function renderDashboard(){
    var data=files[activeFile];if(!data)return;
    document.getElementById("stRecords").textContent=data.totalRecords;
    document.getElementById("stFields").textContent=data.fieldNames.length;
    document.getElementById("stCombos").textContent=data.combos.length;
    document.getElementById("stErrors").textContent=data.discardedErrors||0;
    document.getElementById("fieldCountLabel").textContent="("+data.fieldNames.length+" fields)";
    document.getElementById("comboCountLabel").textContent="("+data.combos.length+" patterns)";
    renderFieldTable();renderCombos();
  }

  function renderFieldTable(){
    var data=files[activeFile];var stats=data.fieldStats.slice();var total=data.totalRecords;
    if(currentSort==="name")stats.sort(function(a,b){return a.name.localeCompare(b.name);});
    else if(currentSort==="pop-desc")stats.sort(function(a,b){return b.populated-a.populated||a.name.localeCompare(b.name);});
    else if(currentSort==="miss-desc")stats.sort(function(a,b){return b.missing-a.missing||a.name.localeCompare(b.name);});
    var body=document.getElementById("ftableBody");body.innerHTML="";
    var head=document.createElement("div");head.className="frow-head frow";
    head.innerHTML="<span>Field Name</span><span>Filled</span><span>Empty</span><span>Coverage</span>";body.appendChild(head);
    for(var i=0;i<stats.length;i++){
      var s=stats[i];var pct=total>0?Math.round((s.populated/total)*100):0;
      var row=document.createElement("div");row.className="frow";
      var n=document.createElement("div");n.className="frow-name";n.textContent=s.name;row.appendChild(n);
      var p=document.createElement("div");p.className="frow-pop";p.textContent=s.populated;row.appendChild(p);
      var m=document.createElement("div");m.className="frow-miss";m.textContent=s.missing;row.appendChild(m);
      var bw=document.createElement("div");bw.style.cssText="display:flex;align-items:center;gap:8px;";
      var bar=document.createElement("div");bar.className="frow-bar";bar.style.flex="1";
      var fill=document.createElement("div");fill.className="frow-bar-fill";fill.style.width=pct+"%";
      if(pct===100)fill.className+=" full";else if(pct>=80)fill.className+=" high";
      else if(pct>=50)fill.className+=" mid";else if(pct>=20)fill.className+=" low";
      else fill.className+=" crit";
      bar.appendChild(fill);bw.appendChild(bar);
      var pe=document.createElement("span");pe.style.cssText="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#737373;min-width:36px;text-align:right;";
      pe.textContent=pct+"%";bw.appendChild(pe);row.appendChild(bw);body.appendChild(row);
    }
  }

  var sortBtns=document.querySelectorAll(".sort-btn");
  for(var si=0;si<sortBtns.length;si++){sortBtns[si].addEventListener("click",function(){
    for(var sj=0;sj<sortBtns.length;sj++)sortBtns[sj].classList.remove("active");
    this.classList.add("active");currentSort=this.getAttribute("data-sort");renderFieldTable();
  });}

  function renderCombos(){
    var data=files[activeFile];var total=data.totalRecords;var combos=data.combos;
    var body=document.getElementById("comboBody");body.innerHTML="";
    var head=document.createElement("div");head.className="combo-row-head combo-row";
    head.innerHTML="<span>Missing Fields Pattern</span><span>Count</span><span>% of Total</span>";body.appendChild(head);
    for(var i=0;i<combos.length;i++){
      var c=combos[i];var pct=total>0?((c.count/total)*100).toFixed(1):"0.0";
      var row=document.createElement("div");row.className="combo-row";
      var fe=document.createElement("div");fe.className="combo-fields";
      if(c.fields.length===0){var tag=document.createElement("span");tag.className="combo-tag full";tag.textContent="All fields complete";fe.appendChild(tag);}
      else{for(var j=0;j<c.fields.length;j++){var t2=document.createElement("span");t2.className="combo-tag";t2.textContent=c.fields[j];fe.appendChild(t2);}}
      row.appendChild(fe);
      var ce=document.createElement("div");ce.className="combo-count";ce.textContent=c.count;row.appendChild(ce);
      var pce=document.createElement("div");pce.className="combo-pct";pce.textContent=pct+"%";row.appendChild(pce);
      body.appendChild(row);
    }
  }

  function downloadBlob(content,filename,mime){var blob=new Blob([content],{type:mime});var url=URL.createObjectURL(blob);
    var a=document.createElement("a");a.href=url;a.download=filename;document.body.appendChild(a);a.click();
    document.body.removeChild(a);URL.revokeObjectURL(url);
  }

  document.getElementById("exportJsonBtn").addEventListener("click",function(){
    var data=files[activeFile];if(!data)return;
    var report={file:activeFile,totalRecords:data.totalRecords,uniqueFields:data.fieldNames.length,
      fieldCompleteness:data.fieldStats.map(function(f){return{field:f.name,populated:f.populated,missing:f.missing,pct:data.totalRecords>0?Math.round(f.populated/data.totalRecords*100):0};}),
      missingFieldCombinations:data.combos.map(function(c){return{missingFields:c.fields.length===0?["(none)"]:c.fields,count:c.count,pct:data.totalRecords>0?+((c.count/data.totalRecords)*100).toFixed(1):0};})
    };
    downloadBlob(JSON.stringify(report,null,2),(activeFile||"report").replace(".json","")+"_analysis.json","application/json");
  });

  document.getElementById("exportCsvBtn").addEventListener("click",function(){
    var data=files[activeFile];if(!data)return;
    var lines=["Section,Field,Populated,Missing,Coverage %"];
    for(var i=0;i<data.fieldStats.length;i++){var f=data.fieldStats[i];var pct=data.totalRecords>0?Math.round(f.populated/data.totalRecords*100):0;
      lines.push("Field Completeness,"+escCSV(f.name)+","+f.populated+","+f.missing+","+pct);}
    lines.push("");lines.push("Section,Missing Fields,Count,% of Total");
    for(var j=0;j<data.combos.length;j++){var c=data.combos[j];var cpct=data.totalRecords>0?((c.count/data.totalRecords)*100).toFixed(1):"0.0";
      var label=c.fields.length===0?"(all complete)":c.fields.join(" + ");
      lines.push("Missing Combination,"+escCSV(label)+","+c.count+","+cpct);}
    downloadBlob(lines.join("\\n"),(activeFile||"report").replace(".json","")+"_analysis.csv","text/csv");
  });

  document.getElementById("exportCompleteBtn").addEventListener("click",function(){
    var data=files[activeFile];if(!data)return;var complete=[];
    for(var i=0;i<data.records.length;i++){var rec=data.records[i];var allFilled=true;
      for(var j=0;j<data.fieldNames.length;j++){if(isEmpty(rec[data.fieldNames[j]])){allFilled=false;break;}}
      if(allFilled)complete.push(rec);}
    if(complete.length===0){showToast("No fully complete records found");return;}
    downloadBlob(JSON.stringify(complete,null,2),(activeFile||"data").replace(".json","")+"_complete.json","application/json");
    showToast("Downloaded "+complete.length+" complete records");
  });

  function escCSV(s){var v=String(s);if(v.indexOf('"')!==-1||v.indexOf(",")!==-1||v.indexOf("\\n")!==-1){return '"'+v.replace(/"/g,'""')+'"';}return v;}

  function showToast(msg){
    var t=document.createElement("div");
    t.style.cssText="position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#262626;color:#f5f5f5;border:1px solid #333;padding:10px 22px;border-radius:10px;font-size:13px;z-index:200;font-family:'IBM Plex Mono',monospace;max-width:90vw;text-align:center;";
    t.textContent=msg;document.body.appendChild(t);
    setTimeout(function(){t.style.opacity="0";t.style.transition="opacity .3s";
      setTimeout(function(){if(t.parentNode)document.body.removeChild(t);},300);},2800);
  }
})();
</script>
</body>
</html>"""

GETTING_STARTED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Getting Started</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 800px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 20px; color: #eab308; margin-bottom: 8px; }
  .subtitle { font-size: 13px; color: #a3a3a3; margin-bottom: 32px; }
  .step { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 16px; display: flex; gap: 20px; align-items: flex-start; }
  .step-num { min-width: 40px; height: 40px; background: #1a1500; border: 2px solid #eab308; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 800; color: #eab308; flex-shrink: 0; }
  .step-content h3 { font-size: 16px; margin-bottom: 8px; color: #f5f5f5; }
  .step-content p { font-size: 13px; color: #a3a3a3; line-height: 1.7; }
  .step-content a { color: #eab308; text-decoration: none; }
  .step-content a:hover { text-decoration: underline; }
  .tip { background: #1a1500; border: 1px solid #332d00; border-radius: 8px; padding: 16px; margin-top: 24px; }
  .tip h4 { font-size: 13px; color: #eab308; margin-bottom: 6px; }
  .tip p { font-size: 12px; color: #a3a3a3; line-height: 1.6; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h2>Getting Started with Auction Intel</h2>
  <p class="subtitle">Follow these 4 steps to find nonprofit auction events and download verified leads.</p>

  <div class="step">
    <div class="step-num">1</div>
    <div class="step-content">
      <h3>Search the Nonprofit Database</h3>
      <p>Go to the <a href="/database">Database</a> page to search over 300,000 nonprofits. Filter by <strong>state, city, region, event type</strong> (gala, auction, golf, etc.), and <strong>prospect tier</strong> (A+, A, B+, C). Use the financial filters to narrow by revenue, fundraising income, or event gross receipts.</p>
      <p style="margin-top:8px;">Select the organizations you want to research and click <strong>"Send to Auction Finder"</strong> to add them to your search queue.</p>
      <p style="margin-top:8px;color:#737373;font-size:13px;"><strong style="color:#eab308;">Search tip:</strong> If you are not getting the number of results you require, simply edit or remove a filter.</p>
    </div>
  </div>

  <div class="step">
    <div class="step-num">2</div>
    <div class="step-content">
      <h3>Run Powerful Research Engine</h3>
      <p>Go to <a href="/">Auction Search</a> where your selected nonprofits will be pre-loaded. You can also paste names or domains directly. Click <strong>"Search for Auctions"</strong> and watch results stream in real-time.</p>
      <p style="margin-top:8px;">The engine uses 3 phases: quick scan, deep research (visiting actual event pages), and targeted follow-up for missing fields like contact email or auction type.</p>
    </div>
  </div>

  <div class="step">
    <div class="step-num">3</div>
    <div class="step-content">
      <h3>Understand Your Results</h3>
      <p>Each lead includes up to <strong>16 fields</strong>: event title, date, URL, auction type, confidence score, contact name, email, role, address, phone, and evidence text. Leads are classified into tiers:</p>
      <p style="margin-top:8px;"><strong style="color:#4ade80;">Complete Contact ($1.50)</strong> &mdash; all fields + verified URL &nbsp;|&nbsp; <strong style="color:#eab308;">Email + Auction Type ($1.25)</strong> &mdash; event + auction type + email &nbsp;|&nbsp; <strong style="color:#fbbf24;">Email Only ($1.00)</strong> &mdash; event + email &nbsp;|&nbsp; <strong style="color:#a3a3a3;">Event Only ($0.75)</strong> &mdash; event + URL only</p>
    </div>
  </div>

  <div class="step">
    <div class="step-num">4</div>
    <div class="step-content">
      <h3>Download & Use Your Leads</h3>
      <p>Once your search completes, download results as <strong>CSV, JSON, or XLSX</strong> right from the search page. All results are also saved on the <a href="/results">Results</a> page for 180 days, so you can re-download anytime.</p>
      <p style="margin-top:8px;">View your charges on the <a href="/billing">Billing</a> page, and top up your balance on the <a href="/wallet">Wallet</a> page ($250 &ndash; $9,999 via Stripe).</p>
    </div>
  </div>

  <div class="tip">
    <h4>Pro Tips</h4>
    <p>Use the <strong>financial filters</strong> on the Database page to find high-value nonprofits (e.g., Total Revenue > $1M, Fundraising Income > $100K). These organizations are more likely to hold large auction events with bigger budgets.</p>
    <p style="margin-top:6px;">Need help? Visit <a href="/support" style="color:#eab308;text-decoration:none;">Support</a> to open a ticket.</p>
  </div>
</div>
</div>
</body>
</html>"""

SUPPORT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Support</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 900px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 20px; color: #d4d4d4; margin-bottom: 8px; }
  .subtitle { font-size: 12px; color: #737373; margin-bottom: 24px; }
  .new-btn { display: inline-block; padding: 10px 20px; background: #ffd900; color: #000; border-radius: 8px; font-size: 13px; font-weight: 600; text-decoration: none; margin-bottom: 24px; }
  .new-btn:hover { background: #ca8a04; }
  .section { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { color: #a3a3a3; text-transform: uppercase; font-size: 10px; padding: 8px 6px; text-align: left; border-bottom: 1px solid #262626; }
  td { padding: 10px 6px; border-bottom: 1px solid #1a1a1a; color: #d4d4d4; }
  tr:hover td { background: #1a1a1a; }
  .empty { text-align: center; padding: 40px; color: #737373; font-size: 13px; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h2>Support Tickets</h2>
  <p class="subtitle">Submit a ticket and we'll get back to you as soon as possible.</p>
  <a href="/support/new" class="new-btn">New Ticket</a>
  <div class="section">
    <table>
      <thead><tr><th>#</th><th>Subject</th>{{USER_COL_HEADER}}<th>Status</th><th>Last Updated</th></tr></thead>
      <tbody>{{TICKET_ROWS}}</tbody>
    </table>
  </div>
</div>
</div>
</body>
</html>"""

SUPPORT_NEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - New Support Ticket</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 700px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 20px; color: #d4d4d4; margin-bottom: 24px; }
  .card { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; }
  label { font-size: 12px; color: #a3a3a3; text-transform: uppercase; display: block; margin-bottom: 6px; }
  input, select, textarea { width: 100%; padding: 10px 14px; background: #000; border: 1px solid #333; border-radius: 8px; color: #f5f5f5; font-family: inherit; font-size: 14px; outline: none; margin-bottom: 16px; }
  input:focus, select:focus, textarea:focus { border-color: #eab308; }
  textarea { height: 160px; resize: vertical; }
  button { padding: 12px 24px; background: #ffd900; color: #000; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }
  button:hover { background: #ca8a04; }
  .back { color: #a3a3a3; text-decoration: none; font-size: 13px; display: inline-block; margin-bottom: 16px; }
  .back:hover { color: #eab308; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <a href="/support" class="back">&larr; Back to tickets</a>
  <h2>New Support Ticket</h2>
  <!-- error -->
  <div class="card">
    <form method="POST">
      <label>Subject</label>
      <input type="text" name="subject" placeholder="Brief description of your issue" required>
      <label>Priority</label>
      <select name="priority">
        <option value="low">Low</option>
        <option value="normal" selected>Normal</option>
        <option value="high">High</option>
        <option value="urgent">Urgent</option>
      </select>
      <label>Message</label>
      <textarea name="message" placeholder="Describe your issue in detail..." required></textarea>
      <button type="submit">Submit Ticket</button>
    </form>
  </div>
</div>
</div>
</body>
</html>"""

SUPPORT_TICKET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUCTIONFINDER - Ticket #{{TICKET_ID}}</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 800px; margin: 0 auto; padding: 24px; }
  .ticket-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
  .ticket-header h2 { font-size: 18px; color: #d4d4d4; }
  .messages { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 20px; max-height: 500px; overflow-y: auto; }
  .reply-box { background: #111111; border: 1px solid #262626; border-radius: 12px; padding: 24px; }
  .reply-box textarea { width: 100%; height: 100px; padding: 10px 14px; background: #000; border: 1px solid #333; border-radius: 8px; color: #f5f5f5; font-family: inherit; font-size: 14px; outline: none; resize: vertical; margin-bottom: 12px; }
  .reply-box textarea:focus { border-color: #eab308; }
  .reply-box button { padding: 10px 20px; background: #ffd900; color: #000; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; }
  .reply-box button:hover { background: #ca8a04; }
  .back { color: #a3a3a3; text-decoration: none; font-size: 13px; display: inline-block; margin-bottom: 16px; }
  .back:hover { color: #eab308; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <a href="/support" class="back">&larr; Back to tickets</a>
  <div class="ticket-header">
    <h2>Ticket #{{TICKET_ID}}: {{SUBJECT}}</h2>
    <div style="display:flex;gap:12px;align-items:center;">
      <span style="color:{{STATUS_COLOR}};font-weight:700;font-size:13px;">{{STATUS}}</span>
      {{STATUS_FORM}}
    </div>
  </div>
  <div class="messages">
    {{MESSAGES}}
  </div>
  <div class="reply-box">
    <form method="POST">
      <textarea name="message" placeholder="Type your reply..." required></textarea>
      <button type="submit">Send Reply</button>
    </form>
  </div>
</div>
</div>
</body>
</html>"""

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auction Intel - Find Nonprofit Auction Events at Scale</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #000000; color: #f5f5f5; }
  a { color: inherit; text-decoration: none; }

  /* Nav */
  .topnav { position: fixed; top: 0; width: 100%; background: rgba(0,0,0,0.85); backdrop-filter: blur(12px); border-bottom: 1px solid #1a1a1a; padding: 16px 40px; display: flex; justify-content: space-between; align-items: center; z-index: 100; }
  .topnav .logo { display: flex; align-items: center; gap: 12px; }
  .topnav .logo img { height: 48px; }
  .topnav .logo span { font-size: 14px; font-weight: 600; color: #a3a3a3; }
  .topnav .nav-links { display: flex; gap: 32px; align-items: center; }
  .topnav .nav-links a { font-size: 14px; color: #a3a3a3; transition: color 0.2s; }
  .topnav .nav-links a:hover { color: #f5f5f5; }
  .topnav .btn-login { padding: 8px 20px; border: 1px solid #333; border-radius: 8px; font-size: 14px; color: #f5f5f5; }
  .topnav .btn-login:hover { border-color: #eab308; color: #eab308; }
  .topnav .btn-cta { padding: 8px 20px; background: #ffd900; color: #000 !important; border-radius: 8px; font-size: 14px; font-weight: 600; }
  .topnav .btn-cta:hover { background: #eab308; }

  /* Hero */
  .hero { padding: 160px 40px 100px; text-align: center; background: radial-gradient(ellipse at 50% 0%, #1a1500 0%, #000000 70%); }
  .hero h1 { font-size: 44px; font-weight: 800; line-height: 1.15; max-width: 900px; margin: 0 auto 24px; letter-spacing: -0.5px; }
  .hero h1 .gold { color: #eab308; }
  .hero .subtitle { font-size: 18px; color: #a3a3a3; max-width: 720px; margin: 0 auto 40px; line-height: 1.7; }
  .hero .cta-row { display: flex; gap: 16px; justify-content: center; align-items: center; }
  .hero .btn-primary { padding: 16px 36px; background: #ffd900; color: #000; border-radius: 10px; font-size: 17px; font-weight: 700; display: inline-block; }
  .hero .btn-primary:hover { background: #eab308; transform: translateY(-1px); }
  .hero .btn-secondary { padding: 16px 36px; border: 1px solid #333; border-radius: 10px; font-size: 17px; color: #d4d4d4; display: inline-block; }
  .hero .btn-secondary:hover { border-color: #eab308; color: #eab308; }
  .hero .trust { margin-top: 40px; font-size: 13px; color: #525252; }
  .hero .trust span { color: #737373; }

  /* Stats bar */
  .stats-bar { display: flex; justify-content: center; gap: 60px; padding: 48px 40px; border-top: 1px solid #1a1a1a; border-bottom: 1px solid #1a1a1a; }
  .stats-bar .stat { text-align: center; }
  .stats-bar .stat .num { font-size: 36px; font-weight: 800; color: #eab308; }
  .stats-bar .stat .lbl { font-size: 14px; color: #737373; margin-top: 4px; text-transform: uppercase; letter-spacing: 1px; }

  /* Sections */
  section { padding: 100px 40px; }
  section.alt { background: #0a0a0a; }
  .section-header { text-align: center; max-width: 800px; margin: 0 auto 60px; }
  .section-header .tag { display: inline-block; padding: 4px 14px; background: #1a1500; border: 1px solid #332d00; border-radius: 20px; font-size: 12px; color: #eab308; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px; }
  .section-header h2 { font-size: 28px; font-weight: 700; margin-bottom: 16px; letter-spacing: -0.3px; }
  .section-header p { font-size: 16px; color: #a3a3a3; line-height: 1.6; }

  /* Features grid */
  .features-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 24px; max-width: 900px; margin: 0 auto; }
  .feature-card { background: #111111; border: 1px solid #1a1a1a; border-radius: 16px; padding: 32px; transition: border-color 0.2s; }
  .feature-card:hover { border-color: #332d00; }
  .feature-card .icon { width: 48px; height: 48px; background: #1a1500; border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 22px; margin-bottom: 20px; }
  .feature-card h3 { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
  .feature-card p { font-size: 15px; color: #a3a3a3; line-height: 1.6; }

  /* How it works */
  .steps { display: grid; grid-template-columns: repeat(2, 1fr); gap: 24px; max-width: 900px; margin: 0 auto; }
  .step { background: #111111; border: 1px solid #1a1a1a; border-radius: 16px; padding: 32px; text-align: left; transition: border-color 0.2s; position: relative; }
  .step:hover { border-color: #332d00; }
  .step .step-num { width: 44px; height: 44px; background: #1a1500; border: 2px solid #eab308; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 800; color: #eab308; margin-bottom: 16px; }
  .step h3 { font-size: 17px; font-weight: 700; margin-bottom: 8px; color: #f5f5f5; }
  .step p { font-size: 15px; color: #a3a3a3; line-height: 1.6; margin: 0; }

  /* Pricing */
  .pricing-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 24px; max-width: 900px; margin: 0 auto; }
  .price-card { background: #111111; border: 1px solid #1a1a1a; border-radius: 16px; padding: 36px; }
  .price-card.highlight { border-color: #eab308; position: relative; }
  .price-card.highlight::before { content: "MOST COMPLETE"; position: absolute; top: -12px; left: 50%; transform: translateX(-50%); background: #eab308; color: #000; font-size: 11px; font-weight: 700; padding: 4px 16px; border-radius: 20px; letter-spacing: 1px; }
  .price-card h3 { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
  .price-card .price-tag { font-size: 14px; color: #a3a3a3; margin-bottom: 20px; }
  .price-card .price-tag b { font-size: 36px; color: #f5f5f5; font-weight: 800; }
  .price-card ul { list-style: none; }
  .price-card ul li { padding: 8px 0; font-size: 15px; color: #d4d4d4; border-bottom: 1px solid #1a1a1a; }
  .price-card ul li::before { content: "\\2713"; color: #8b5cf6; margin-right: 10px; font-weight: 700; }
  .price-card ul li:last-child { border-bottom: none; }
  .price-card .signup-btn { display: block; text-align: center; margin-top: 24px; padding: 14px; background: #ffd900; color: #000; border-radius: 10px; font-size: 15px; font-weight: 700; }
  .price-card .signup-btn:hover { background: #eab308; }
  .price-card.std .signup-btn { background: #1a1a1a; color: #f5f5f5; border: 1px solid #333; }
  .price-card.std .signup-btn:hover { border-color: #eab308; color: #eab308; }
  .pricing-note { text-align: center; margin-top: 32px; font-size: 15px; color: #a3a3a3; }
  .pricing-note strong { color: #eab308; }
  .pricing-bonus { text-align: center; margin-top: 16px; font-size: 15px; color: #737373; }

  /* FAQ */
  .faq-list { max-width: 720px; margin: 0 auto; }
  .faq-item { border-bottom: 1px solid #1a1a1a; }
  .faq-q { padding: 20px 0; font-size: 16px; font-weight: 600; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
  .faq-q:hover { color: #eab308; }
  .faq-q .arrow { font-size: 20px; color: #525252; transition: transform 0.2s; }
  .faq-a { display: none; padding: 0 0 20px; font-size: 15px; color: #a3a3a3; line-height: 1.7; }
  .faq-item.open .faq-a { display: block; }
  .faq-item.open .arrow { transform: rotate(45deg); color: #eab308; }

  /* CTA */
  .final-cta { text-align: center; padding: 100px 40px; background: radial-gradient(ellipse at 50% 100%, #1a1500 0%, #000000 70%); }
  .final-cta h2 { font-size: 28px; font-weight: 700; margin-bottom: 16px; }
  .final-cta p { font-size: 17px; color: #a3a3a3; margin-bottom: 36px; max-width: 500px; margin-left: auto; margin-right: auto; }
  .final-cta .btn-primary { padding: 16px 40px; background: #ffd900; color: #000; border-radius: 10px; font-size: 17px; font-weight: 700; display: inline-block; }
  .final-cta .btn-primary:hover { background: #eab308; }

  /* Footer */
  .footer { padding: 60px 40px 40px; border-top: 1px solid #1a1a1a; }
  .footer-inner { max-width: 900px; margin: 0 auto; display: flex; justify-content: space-between; align-items: flex-start; gap: 40px; }
  .footer-brand { max-width: 400px; }
  .footer-brand .footer-logo { font-size: 16px; font-weight: 700; color: #f5f5f5; margin-bottom: 10px; }
  .footer-brand p { font-size: 13px; color: #737373; line-height: 1.6; }
  .footer-contact { text-align: right; }
  .footer-contact .label { font-size: 12px; color: #525252; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; font-weight: 600; }
  .footer-contact a { display: block; font-size: 13px; color: #a3a3a3; margin-bottom: 4px; }
  .footer-contact a:hover { color: #eab308; }
  .footer-copy { text-align: center; margin-top: 32px; padding-top: 20px; border-top: 1px solid #1a1a1a; font-size: 12px; color: #525252; }

  /* Hamburger — hidden on desktop */
  .hamburger { display: none; background: none; border: none; cursor: pointer; padding: 8px; }
  .hamburger span { display: block; width: 24px; height: 2px; background: #f5f5f5; margin: 5px 0; border-radius: 2px; transition: 0.3s; }

  /* ── Mobile ─────────────────────────────────────── */
  @media (max-width: 768px) {
    .topnav { padding: 12px 20px; }
    .hamburger { display: block; }
    .topnav .nav-links { display: none; flex-direction: column; position: absolute; top: 100%; left: 0; right: 0; background: rgba(0,0,0,0.95); backdrop-filter: blur(12px); padding: 16px 20px; gap: 0; border-bottom: 1px solid #1a1a1a; }
    .topnav .nav-links.open { display: flex; }
    .topnav .nav-links a { padding: 12px 0; border-bottom: 1px solid #1a1a1a; font-size: 16px; }
    .topnav .nav-links a:last-child { border-bottom: none; }
    .topnav .btn-login, .topnav .btn-cta { text-align: center; margin-top: 4px; }
    .hero { padding: 100px 20px 60px; }
    .hero h1 { font-size: 28px; }
    .hero h1 span[style*="font-size:38px"] { font-size: 22px !important; }
    .hero .subtitle { font-size: 16px; margin-bottom: 28px; }
    .hero .cta-row { flex-direction: column; gap: 12px; }
    .hero .cta-row a { width: 100%; text-align: center; }
    .hero .trust { font-size: 12px; }
    .stats-bar { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; padding: 32px 20px; }
    .stats-bar .stat .num { font-size: 28px; }
    section { padding: 60px 20px; }
    .section-header { margin-bottom: 36px; }
    .section-header h2 { font-size: 22px; }
    .section-header p { font-size: 15px; }
    .features-grid { grid-template-columns: 1fr; gap: 16px; }
    .feature-card { padding: 24px; }
    .steps { grid-template-columns: 1fr; gap: 24px; }
    section[style*="padding:60px"] { padding: 40px 20px !important; }
    section[style*="padding:60px"] h2 { font-size: 26px !important; }
    section[style*="padding:60px"] a[style*="padding:16px 40px"] { display: block !important; padding: 14px 20px !important; }
    .pricing-grid { grid-template-columns: 1fr; gap: 16px; }
    .price-card { padding: 28px; }
    .faq-list { max-width: 100%; }
    .faq-q { font-size: 15px; }
    .final-cta { padding: 60px 20px; }
    .final-cta h2 { font-size: 28px; }
    .final-cta p { font-size: 15px; }
    .final-cta .btn-primary { display: block; width: 100%; text-align: center; }
    .footer { padding: 40px 20px 32px; }
    .footer-inner { flex-direction: column; gap: 24px; }
    .footer-contact { text-align: left; }
  }
</style>
</head>
<body>

<div class="topnav">
  <div class="logo">
    <img src="/static/logo_dark.png" alt="Auction Intel">
  </div>
  <button class="hamburger" onclick="document.querySelector('.nav-links').classList.toggle('open')" aria-label="Menu">
    <span></span><span></span><span></span>
  </button>
  <div class="nav-links">
    <a href="#how-it-works">How It Works</a>
    <a href="#features">Features</a>
    <a href="#pricing">Pricing</a>
    <a href="#faq">FAQ</a>
    <a href="/login" class="btn-login">Log In</a>
    <a href="/register" class="btn-cta">Start Free</a>
  </div>
</div>

<!-- Hero -->
<div class="hero">
  <h1><span class="gold">Find nonprofit auction events.</span><br><span style="font-size:38px;">Verified. Exportable. Ready to contact.</span></h1>
  <p class="subtitle">Auction Intel's new Auction Finder Research Engine scans nonprofit websites to find upcoming fundraising events with a live auction, a silent auction, or both. Get high-quality leads with a verified event page link, the event date, event-level contacts (email/phone), the auction type, and more.</p>
  <div class="cta-row">
    <a href="/register" class="btn-primary">Get Started Free</a>
    <a href="#how-it-works" class="btn-secondary">See How It Works</a>
  </div>
  <p class="trust">LLM-enhanced real-time web grounding <span>&bull;</span> Results verified against actual event pages</p>
</div>

<!-- Stats -->
<div class="stats-bar">
  <div class="stat"><div class="num">300K+</div><div class="lbl">Nonprofits in Database</div></div>
  <div class="stat"><div class="num">Up to 1,000</div><div class="lbl">Nonprofit Domains Per Search</div></div>
  <div class="stat"><div class="num">40+</div><div class="lbl">Filters for Targeted Searches</div></div>
  <div class="stat"><div class="num">7-Day</div><div class="lbl">Free Trial</div></div>
</div>

<!-- How It Works -->
<section class="alt" id="how-it-works">
  <div class="section-header">
    <span class="tag">How It Works</span>
    <h2>How it works</h2>
    <p>Stop manually researching one organization at a time.<br>Auction Intel runs research in batches and returns verified, outreach-ready leads.</p>
  </div>
  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <h3>Build your list</h3>
      <p>Filter nonprofits by location, revenue range, and event category&mdash;or paste your own domains.</p>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <h3>Run Auction Finder</h3>
      <p>We scan nonprofit websites, trusted event platforms, and the broader web to locate upcoming events with auction activity.</p>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <h3>Verify the event page</h3>
      <p>Every billable lead must include a real event page URL plus supporting evidence pulled from that page.</p>
    </div>
    <div class="step">
      <div class="step-num">4</div>
      <h3>Export &amp; start outreach</h3>
      <p>Download results as CSV / XLSX / JSON and start contacting the right people.</p>
    </div>
  </div>
</section>

<!-- Features -->
<section id="features">
  <div class="section-header">
    <span class="tag">Features</span>
    <h2>Built to close more consignments</h2>
    <p>Spend less time researching and more time closing.<br>Auction Intel delivers verified opportunities at scale&mdash;with proof.</p>
  </div>
  <div class="features-grid">
    <div class="feature-card">
      <div class="icon">&#x1F50D;</div>
      <h3>Deep Verification (not snippets)</h3>
      <p>A multi-phase process checks the actual event page and pulls evidence text. If there's no verified event page URL, it's not a billable lead.</p>
    </div>
    <div class="feature-card">
      <div class="icon">&#x1F3E6;</div>
      <h3>Premium Nonprofit Database</h3>
      <p>Search 300K+ nonprofits by state, revenue range, and event category&mdash;including gala, auction, golf, dinner, and 20+ more.</p>
    </div>
    <div class="feature-card">
      <div class="icon">&#x26A1;</div>
      <h3>Batch Research at Scale</h3>
      <p>Run up to 1,000 organizations per search and watch results stream in as each organization is processed.</p>
    </div>
    <div class="feature-card">
      <div class="icon">&#x1F4CA;</div>
      <h3>Rich Lead Records</h3>
      <p>Each lead can include event title, date, event page URL, auction type, confidence score, contact name, email, phone, and supporting evidence from the source page.</p>
    </div>
    <div class="feature-card">
      <div class="icon">&#x1F4E5;</div>
      <h3>Export Anywhere + 180-Day Storage</h3>
      <p>Download CSV/JSON/XLSX anytime. Results stay available for 180 days for re-download.</p>
    </div>
    <div class="feature-card">
      <div class="icon">&#x1F6E1;</div>
      <h3>Verified Tiers + Fair Billing</h3>
      <p>You only pay for leads with a verified event page link. Pricing is tiered by completeness so you never overpay.</p>
    </div>
  </div>
</section>

<!-- Trial Banner -->
<section style="padding:80px 40px;text-align:center;background:linear-gradient(180deg,#0d0a1a 0%,#000 100%);border-top:1px solid #1a1a2a;border-bottom:1px solid #1a1a2a;">
  <div style="max-width:560px;margin:0 auto;">
    <div style="display:inline-block;padding:6px 20px;background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.25);border-radius:24px;font-size:12px;color:#8b5cf6;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:28px;">Free Trial</div>
    <div style="font-size:72px;font-weight:900;color:#8b5cf6;line-height:1;margin-bottom:4px;">150</div>
    <div style="font-size:22px;font-weight:700;color:#f5f5f5;margin-bottom:24px;">nonprofit researches&mdash;on&nbsp;us</div>
    <p style="font-size:15px;color:#a3a3a3;margin-bottom:6px;">Up to $50 in value. No credit card required.</p>
    <p style="font-size:14px;color:#737373;margin-bottom:28px;">Enter your promo code at registration to activate your free trial.</p>
    <a href="/register" style="display:inline-block;padding:14px 36px;background:#8b5cf6;color:#fff;border-radius:10px;font-size:16px;font-weight:700;text-decoration:none;">Start Your Free Trial</a>
    <p style="font-size:12px;color:#525252;margin-top:16px;">7-day trial &middot; No auto-charge &middot; Cancel anytime</p>
  </div>
</section>

<!-- Pricing -->
<section id="pricing">
  <div class="section-header">
    <span class="tag">Pricing</span>
    <h2>Simple, transparent pricing</h2>
    <p>All tiers require a verified event page link.<br>Wallet-based billing&mdash;no subscriptions, no commitments.</p>
  </div>
  <div class="pricing-grid">
    <div class="price-card highlight">
      <h3>Complete Contact</h3>
      <div class="price-tag"><b>$1.50</b> / lead</div>
      <ul>
        <li>Event title &amp; event date</li>
        <li>Verified event page link</li>
        <li>Auction type (live/silent/both)</li>
        <li>Contact name &amp; contact email</li>
      </ul>
      <a href="/register" class="signup-btn">Get Started</a>
    </div>
    <div class="price-card std">
      <h3>Email + Auction Type</h3>
      <div class="price-tag"><b>$1.25</b> / lead</div>
      <ul>
        <li>Event title &amp; event date</li>
        <li>Verified event page link</li>
        <li>Auction type (live/silent/both)</li>
        <li>Contact email (no contact name)</li>
      </ul>
      <a href="/register" class="signup-btn">Create Account</a>
    </div>
    <div class="price-card std">
      <h3>Email Only</h3>
      <div class="price-tag"><b>$1.00</b> / lead</div>
      <ul>
        <li>Event title &amp; event date</li>
        <li>Verified event page link</li>
        <li>Contact email</li>
        <li>Auction type missing</li>
      </ul>
      <a href="/register" class="signup-btn">Create Account</a>
    </div>
    <div class="price-card std">
      <h3>Event Only</h3>
      <div class="price-tag"><b>$0.75</b> / lead</div>
      <ul>
        <li>Event title &amp; event date</li>
        <li>Verified event page link only</li>
        <li>No contact info</li>
      </ul>
      <a href="/register" class="signup-btn">Create Account</a>
    </div>
  </div>
  <p class="pricing-note"><strong>No event page link = no lead charge.</strong></p>
  <p class="pricing-note" style="margin-top:12px;">Research fee: <strong>$0.08</strong>/nonprofit ($0.07 at 10K+, $0.06 at 50K+) &mdash; charged whether or not a lead is found.</p>
  <p class="pricing-bonus">Bonus fields (no extra charge): mailing address + main phone (when available).</p>

  <!-- Complete Contacts Only callout -->
  <div style="max-width:900px;margin:40px auto 0;background:#111;border:1px solid #1a1a1a;border-radius:16px;padding:32px;text-align:left;">
    <h3 style="font-size:20px;font-weight:700;margin-bottom:8px;">Only Want Complete Contacts?</h3>
    <p style="font-size:15px;color:#a3a3a3;line-height:1.6;">Turn on <strong style="color:#eab308;">"Complete Contacts Only"</strong> before running a search. When enabled, your results will only include leads with a verified contact name, contact email, auction type, and event page link.</p>
  </div>

  <!-- Exclusive Event Lead callout -->
  <div style="max-width:900px;margin:24px auto 0;background:#111;border:1px solid #332d00;border-radius:16px;padding:32px;text-align:left;">
    <h3 style="font-size:20px;font-weight:700;margin-bottom:8px;">Priority Lead Lock &mdash; <span style="color:#eab308;">$2.50</span></h3>
    <p style="font-size:15px;color:#a3a3a3;line-height:1.6;">Lock any event lead so it stops being sold to new customers going forward. For $2.50, the lead is pulled from future search results&mdash;giving you a head start over the competition. This is the total price and replaces the standard lead tier fee.</p>
    <p style="font-size:13px;color:#737373;margin-top:12px;">Applies to the specific event lead (not the entire organization). Previous buyers retain access.</p>
  </div>
</section>

<!-- FAQ -->
<section class="alt" id="faq">
  <div class="section-header">
    <span class="tag">FAQ</span>
    <h2>FAQ</h2>
    <p>Quick answers to common questions.<br>Can't find what you need? Open a support ticket.</p>
  </div>
  <div class="faq-list">
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What types of events does Auction Intel find? <span class="arrow">+</span></div>
      <div class="faq-a">We find nonprofit fundraising events with an auction component: silent auctions, live auctions, galas with paddle raises, charity dinners with auction items, golf tournaments with silent auctions, art shows, casino nights, and more. If the event has bidding, we find it.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">How does the research engine verify that an event actually exists? <span class="arrow">+</span></div>
      <div class="faq-a">Our 3-phase research pipeline does not rely on search snippets alone. The research engine visits the actual event page, reads the full page text, extracts evidence quotes showing an auction component, and returns the direct event page link. Every billable lead must include a verified event page link&mdash;no link, no charge.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What if a nonprofit doesn't have an upcoming auction? <span class="arrow">+</span></div>
      <div class="faq-a">You still pay the research fee ($0.08 per nonprofit) because the research engine performed the web research. However, you are not charged a lead fee when no auction was found. The research fee covers the compute cost regardless of outcome.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">How do I add funds to my account? <span class="arrow">+</span></div>
      <div class="faq-a">We use Stripe for secure payments. Go to the Wallet page, enter an amount between $250 and $9,999, and complete checkout. Your balance updates instantly. All funds are non-refundable but never expire.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What's included in the premium nonprofit database? <span class="arrow">+</span></div>
      <div class="faq-a">Our database contains 300,000+ nonprofit organizations in the United States. Each record includes organization name, website, address, revenue figures, fundraising income/expenses, and event keywords. You can filter by these fields before sending organizations to the research engine.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">How long are results stored? <span class="arrow">+</span></div>
      <div class="faq-a">All search results are stored for 180 days. You can re-download CSV, JSON, or XLSX files from the Results page at any time during this period. After 180 days, results are automatically deleted.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">Can I research nonprofits not in the database? <span class="arrow">+</span></div>
      <div class="faq-a">Yes. The database is just one way to build your prospect list. You can also paste nonprofit names or domain URLs directly into the Auction Search page. The engine can research any organization you provide, whether it's in our database or not.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What's the difference between lead tiers? <span class="arrow">+</span></div>
      <div class="faq-a">Leads are priced by completeness, and all tiers require a verified event page link: <b>Complete Contact ($1.50)</b>: Event title/date + event page link + auction type + contact name + contact email. <b>Email + Auction Type ($1.25)</b>: Event title/date + event page link + auction type + contact email (name missing). <b>Email Only ($1.00)</b>: Event title/date + event page link + contact email (auction type missing). <b>Event Only ($0.75)</b>: Event title/date + event page link only. No event page link = no lead charge. Bonus fields (no extra charge): mailing address + main phone (when available).</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">Is mailing address included? <span class="arrow">+</span></div>
      <div class="faq-a">Mailing address is included when available (no extra charge). Some nonprofits use PO Boxes or have incomplete public address data.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What is "Complete Contacts Only" mode? <span class="arrow">+</span></div>
      <div class="faq-a">It's a toggle you can enable before running a search. When turned on, only Complete Contact leads are included in your results&mdash;each one includes event title, event date, verified event page link, auction type, contact name, and contact email. You'll get fewer results, but every lead is contact-ready. Leads that don't meet this standard are excluded from your export and not billed.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What is a Priority Lead Lock? <span class="arrow">+</span></div>
      <div class="faq-a">For $2.50 per lead, you can lock any event lead so it stops being sold to new customers going forward. This gives you a competitive advantage by limiting who else can access that lead. The $2.50 is a flat fee that replaces the standard lead tier price. Previous buyers retain access. Applies to the specific event lead, not the entire organization.</div>
    </div>
  </div>
</section>

<!-- Final CTA -->
<div class="final-cta">
  <h2>Start finding auction leads today</h2>
  <p>Join auction professionals who use Auction Intel to discover<br>nonprofit events before the competition.</p>
  <a href="/register" class="btn-primary">Create Your Free Account</a>
</div>

<div class="footer">
  <div class="footer-inner">
    <div class="footer-brand">
      <div class="footer-logo">Auction Intel</div>
      <p>Event-driven sales intelligence with verified nonprofit fundraising leads. Timing precision that turns research into revenue.</p>
    </div>
    <div class="footer-contact">
      <div class="label">Contact</div>
      <a href="mailto:support@auctionintel.us">support@auctionintel.us</a>
      <a href="tel:3037194851">303-719-4851</a>
    </div>
  </div>
  <div class="footer-copy">&copy; 2026 Auction Intel. All rights reserved.</div>
</div>

</body>
</html>"""

# ─── Run ──────────────────────────────────────────────────────────────────────

# ─── App Initialization (runs under both gunicorn and direct execution) ──────

if not os.environ.get("POE_API_KEY"):
    print("WARNING: POE_API_KEY environment variable is not set.", file=sys.stderr)

init_db()
print("Database initialized.", file=sys.stderr)

cleanup_stale_running_jobs()
cleanup_expired_jobs()
flush_uncertain_cache()

print(f"Stripe configured: {'Yes' if stripe.api_key else 'No (set STRIPE_SECRET_KEY)'}", file=sys.stderr)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"AUCTIONFINDER Web UI starting on http://localhost:{port}", file=sys.stderr)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
