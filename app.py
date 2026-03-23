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
import random
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
    validate_email_emailable, validate_emails_bulk, EMAILABLE_API_KEY,
    POE_API_KEY, POE_API_KEY_2, POE_BOT_NAME_2,
    POE_BOT_NAME_3, POE_BOT_NAME_4,
    POE_BOT_NAME_5, POE_BOT_NAME_6,
)

from db import (
    init_db, create_user, authenticate, get_user, get_user_full,
    get_balance, add_funds, charge_research_fee, charge_lead_fee,
    has_sufficient_balance, get_transactions, get_research_fee_cents,
    update_password, get_spending_summary, get_job_breakdowns,
    create_search_job, complete_search_job, fail_search_job, save_job_checkpoint,
    get_user_jobs, cleanup_expired_jobs, cleanup_stale_running_jobs,
    get_user_by_email, create_reset_token, validate_reset_token,
    consume_reset_token,
    create_verification_token, validate_verification_token, consume_verification_token,
    create_ticket, get_ticket, get_tickets_for_user, get_all_tickets,
    get_ticket_messages, add_ticket_message, update_ticket_status,
    mark_messages_read_by_user, mark_messages_read_by_admin,
    get_unread_ticket_count,
    purchase_exclusive_lead, is_lead_exclusive, get_user_exclusive_leads,
    EXCLUSIVE_LEAD_PRICE_CENTS,
    cache_get, cache_put, flush_uncertain_cache,
    save_result_file, get_result_file,
    get_trial_users_for_drip, get_drips_sent, record_drip_sent,
    get_inactive_users, get_expiring_trial_users,
    save_single_result, get_completed_domains, get_completed_results,
    save_job_input_domains, get_job_input_domains, get_search_job,
    create_api_key, validate_api_key, revoke_api_key, get_user_api_keys,
    update_last_login, admin_ban_user, admin_unban_user, admin_adjust_wallet,
    admin_get_kpis, admin_get_all_users, admin_get_user_detail,
    admin_get_revenue_timeline, admin_get_top_spenders,
    admin_get_recent_activity, admin_get_recent_logins,
    admin_get_cache_stats, admin_get_drip_stats,
    get_user_paid_domains, admin_get_all_cache_results,
    cleanup_expired_cache, cleanup_old_job_results,
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


class EventQueue:
    """Queue that stores all events for SSE replay on reconnect."""
    def __init__(self):
        self.events = []        # append-only event store
        self._q = queue.Queue() # for blocking .get()

    def put(self, event):
        if event is not None:
            self.events.append(event)
        self._q.put(event)

    def get(self, timeout=None):
        return self._q.get(timeout=timeout)


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
        # Check if user is banned
        user = get_user(session["user_id"])
        if user and user.get("is_banned"):
            session.clear()
            return redirect(url_for("login_page"))
        # Block unverified non-admin users
        if not session.get("email_verified") and not session.get("is_admin"):
            return redirect(url_for("verify_email_pending"))
        return f(*args, **kwargs)
    return decorated


def api_auth(f):
    """Decorator: authenticate via X-API-Key header OR session cookie."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key", "")
        if api_key:
            uid = validate_api_key(api_key)
            if not uid:
                return jsonify({"error": "Invalid or revoked API key"}), 401
            user = get_user(uid)
            if not user:
                return jsonify({"error": "User not found"}), 401
            request._api_user_id = uid
            request._api_is_admin = user.get("is_admin", False)
            request._api_is_trial = user.get("is_trial", False)
            request._api_email = user.get("email", "")
            return f(*args, **kwargs)
        # Fall back to session auth
        if not session.get("user_id"):
            return jsonify({"error": "Authentication required. Pass X-API-Key header or login via browser."}), 401
        request._api_user_id = session["user_id"]
        request._api_is_admin = session.get("is_admin", False)
        request._api_is_trial = session.get("is_trial", False)
        request._api_email = session.get("email", "")
        return f(*args, **kwargs)
    return decorated


def _load_new_landing():
    """Load the editorial landing page from disk."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auction_intel_landing_page.html")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[LANDING] Error loading landing page: {e}", flush=True)
        return LANDING_HTML


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
  * { scrollbar-width:thin; scrollbar-color:#eab308 #0a0a0a; }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-track { background:#0a0a0a; }
  ::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }
  ::-webkit-scrollbar-thumb:hover { background:#ffd900; }
  .sidebar { position:fixed; top:0; left:0; width:260px; height:100vh; background:#0a0a0a; border-right:1px solid #1a1a1a; display:flex; flex-direction:column; z-index:100; overflow-y:auto; transition:transform 0.3s ease; }
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
  .live-batch-btn { display:none; margin:8px 12px; padding:10px 12px; background:#1a1500; border:1px solid #eab308; border-radius:6px; color:#eab308; font-size:13px; font-weight:700; text-decoration:none; text-align:center; cursor:pointer; animation:livePulse 1.5s ease-in-out infinite; }
  .live-batch-btn:hover { background:#332d00; }
  .live-batch-btn .dot { display:inline-block; width:8px; height:8px; background:#eab308; border-radius:50%; margin-right:8px; animation:dotBlink 1s step-end infinite; }
  @keyframes livePulse { 0%,100%{opacity:1;} 50%{opacity:0.6;} }
  @keyframes dotBlink { 0%,100%{opacity:1;} 50%{opacity:0.2;} }
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
    "api-keys": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>',
    "api-docs": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>',
    "admin-dashboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    "admin-users": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    "admin-revenue": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
    "admin-activity": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    "admin-system": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    "admin-results": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
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
        ("api-keys", "/settings/api-keys", "API Keys"),
        ("api-docs", "/settings/api-docs", "API Docs"),
    ]),
    ("Admin", [
        ("admin-dashboard", "/admin/", "Dashboard", True),
        ("admin-users", "/admin/users", "Users", True),
        ("admin-revenue", "/admin/revenue", "Revenue", True),
        ("admin-activity", "/admin/activity", "Activity", True),
        ("admin-results", "/admin/results", "Results", True),
        ("admin-batch-runner", "/admin/batch-runner", "Batch Runner", True),
        ("admin-tickets", "/admin/tickets", "Tickets", True),
        ("admin-system", "/admin/system", "System", True),
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
        '  <div class="sidebar-logo"><a href="/"><img src="/static/logo_dark.png" alt="Auction Finder" style="height:44px;"></a></div>\n'
        f'{sections}'
        '  <a href="#" class="live-batch-btn" id="liveBatchBtn" onclick="return false;"><span class="dot"></span>Return to Live Batch</a>\n'
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

_LIVE_BATCH_JS = """
<script>
(function(){
  var btn = document.getElementById('liveBatchBtn');
  if (!btn) return;
  var checkActive = function(){
    fetch('/api/active-job').then(function(r){return r.json()}).then(function(d){
      if (d.job_id) {
        btn.style.display = 'block';
        btn.href = '/?rejoin=' + d.job_id;
        btn.onclick = function(){ window.location.href = '/?rejoin=' + d.job_id; return false; };
      } else {
        btn.style.display = 'none';
      }
    }).catch(function(){});
  };
  checkActive();
  setInterval(checkActive, 10000);
})();
</script>
"""

def _inject_sidebar(html, active):
    """Replace {{SIDEBAR_HTML}} placeholder with built sidebar, and {{SIDEBAR_CSS}} with CSS."""
    html = html.replace("{{SIDEBAR_CSS}}", _SIDEBAR_CSS)
    html = html.replace("{{SIDEBAR_HTML}}", _build_sidebar_html(active))
    # Inject favicon into all sidebar pages
    if _FAVICON_TAG not in html:
        html = html.replace("</head>", _FAVICON_TAG + "\n</head>", 1)
    # Inject live batch polling JS before </body>
    if "</body>" in html:
        html = html.replace("</body>", _LIVE_BATCH_JS + "</body>", 1)
    return html


# ─── Research Worker (runs in background thread with its own event loop) ─────

def _research_one(
    nonprofit: str, index: int, total: int, progress_q,
    user_id: Optional[int] = None, job_id: str = "", is_admin: bool = False,
    is_trial: bool = False, balance_exhausted: list = None,
    selected_tiers: list = None,
    paid_domains: set = None,
    poe_bot_name: str = None, poe_api_key: str = None,
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
        cached["_source"] = "auctionintel.app db"
        cached["_api_calls"] = 0
        cached["query_domain"] = nonprofit

        status = cached.get("status", "uncertain")

        # Data-driven override: if cache has valid URL + title, treat as "found"
        # regardless of stored status — same logic as _poe_result_to_full
        has_real_url = (cached.get("event_url", "").startswith("http://") or
                        cached.get("event_url", "").startswith("https://"))
        has_real_title = bool(cached.get("event_title", "").strip())
        if status in ("not_found", "uncertain") and has_real_url and has_real_title:
            print(f"[CACHE-STATUS-OVERRIDE] {nonprofit}: was '{status}' but has URL+title, overriding to 'found'", flush=True)
            status = "found"
            cached["status"] = "found"

        if status in ("not_found", "uncertain", "error"):
            tier, price = "not_billable", 0
        else:
            tier, price = classify_lead_tier(cached)

        # Email validation deferred to bulk step after research loop
        cached["email_status"] = ""

        title = cached.get("event_title", "")

        # Skip lead fee if tier not in selected_tiers
        _sel = selected_tiers or ["decision_maker", "outreach_ready", "event_verified"]
        if tier != "not_billable" and tier not in _sel:
            price = 0

        # Skip lead fee if user already paid for this domain
        if price > 0 and nonprofit.strip().lower() in (paid_domains or set()):
            print(f"[LEAD-DEDUP] {nonprofit}: already purchased, waiving lead fee", flush=True)
            price = 0
            progress_q.put({"type": "lead_waived", "index": index, "total": total, "nonprofit": nonprofit})

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
            "email_status": cached.get("email_status", ""),
        })
        if job_id:
            save_single_result(job_id, nonprofit, cached)
        return cached

    # ── Call Poe bot (same as AUCTIONINTEL.APP_BOT.PY) ──
    text = call_poe_bot_sync(nonprofit, bot_name=poe_bot_name, api_key=poe_api_key)

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

    status = result.get("status", "uncertain")
    if status in ("not_found", "uncertain", "error"):
        tier, price = "not_billable", 0
    else:
        tier, price = classify_lead_tier(result)

    # Email validation deferred to bulk step after research loop
    result["email_status"] = ""

    # Skip lead fee if tier not in selected_tiers
    _sel = selected_tiers or ["decision_maker", "outreach_ready", "event_verified"]
    if tier != "not_billable" and tier not in _sel:
        price = 0

    # Skip lead fee if user already paid for this domain
    if price > 0 and nonprofit.strip().lower() in (paid_domains or set()):
        print(f"[LEAD-DEDUP] {nonprofit}: already purchased, waiving lead fee", flush=True)
        price = 0
        progress_q.put({"type": "lead_waived", "index": index, "total": total, "nonprofit": nonprofit})

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
        "email_status": result.get("email_status", ""),
    })
    cache_put(nonprofit, result)
    if job_id:
        save_single_result(job_id, nonprofit, result)
    return result


def _run_job(
    nonprofits: List[str], job_id: str, progress_q,
    user_id: Optional[int] = None, is_admin: bool = False, is_trial: bool = False,
    selected_tiers: list = None, user_email: str = "",
):
    """Run research — one domain at a time, no batching, no async.
    Matches AUCTIONINTEL.APP_BOT.PY sequential pattern."""
    if selected_tiers is None:
        selected_tiers = ["decision_maker", "outreach_ready", "event_verified"]
    if len(nonprofits) > MAX_NONPROFITS:
        nonprofits = nonprofits[:MAX_NONPROFITS]

    # Route Poe credentials based on user — each admin uses a separate Poe account
    poe_bot_name = None  # defaults to POE_BOT_NAME in call_poe_bot_sync
    poe_api_key = None
    if user_email == "blake1@auctionintel.us" and POE_API_KEY:
        poe_bot_name = POE_BOT_NAME_2
        poe_api_key = POE_API_KEY
        print(f"[POE-ROUTING] {user_email} -> bot={POE_BOT_NAME_2} (key 1)", flush=True)
    elif user_email == "blake2@auctionintel.us" and POE_API_KEY_2:
        poe_bot_name = POE_BOT_NAME_3
        poe_api_key = POE_API_KEY_2
        print(f"[POE-ROUTING] {user_email} -> bot={POE_BOT_NAME_3} (key 2)", flush=True)
    elif user_email == "blake3@auctionintel.us" and POE_API_KEY_2:
        poe_bot_name = POE_BOT_NAME_4
        poe_api_key = POE_API_KEY_2
        print(f"[POE-ROUTING] {user_email} -> bot={POE_BOT_NAME_4} (key 2)", flush=True)
    elif user_email == "blake4@auctionintel.us" and POE_API_KEY:
        poe_bot_name = POE_BOT_NAME_5
        poe_api_key = POE_API_KEY
        print(f"[POE-ROUTING] {user_email} -> bot={POE_BOT_NAME_5} (key 1)", flush=True)
    elif user_email == "blake5@auctionintel.us" and POE_API_KEY_2:
        poe_bot_name = POE_BOT_NAME_6
        poe_api_key = POE_API_KEY_2
        print(f"[POE-ROUTING] {user_email} -> bot={POE_BOT_NAME_6} (key 2)", flush=True)
    else:
        print(f"[POE-ROUTING] {user_email or 'default'} -> bot={POE_BOT_NAME}", flush=True)

    # Randomize processing order so identical queries yield different result ordering
    random.shuffle(nonprofits)

    total = len(nonprofits)
    progress_q.put({"type": "started", "total": total, "batches": 1})

    # Persist full input domain list for resume capability
    save_job_input_domains(job_id, nonprofits)

    all_results: List[Dict[str, Any]] = []
    billing_summary = {"research_fees": 0, "lead_fees": {}, "total_charged": 0}
    balance_exhausted = [False]
    paid_domains = get_user_paid_domains(user_id) if user_id and not is_admin else set()
    start = time.time()

    for idx, np_name in enumerate(nonprofits, start=1):
        # Stop if user requested stop
        if jobs.get(job_id, {}).get("stop_requested"):
            skipped = total - idx + 1
            progress_q.put({
                "type": "balance_warning",
                "message": f"Search stopped by user. {skipped} nonprofit(s) skipped.",
            })
            break

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
            selected_tiers=selected_tiers,
            paid_domains=paid_domains,
            poe_bot_name=poe_bot_name, poe_api_key=poe_api_key,
        )
        all_results.append(result)

        # Per-result saves happen in _research_one() via save_single_result()
        # Log progress every 100 results
        if idx % 100 == 0:
            print(f"[PROGRESS] {job_id}: {idx}/{total} results saved", flush=True)
            # Generate partial CSV chunk in result_files for large batches
            try:
                partial_buf = io.StringIO()
                writer = csv.DictWriter(partial_buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
                writer.writeheader()
                for r in all_results:
                    writer.writerow({col: r.get(col, "") for col in CSV_COLUMNS})
                save_result_file(job_id, "csv", partial_buf.getvalue().encode("utf-8"))
            except Exception as e:
                print(f"[PARTIAL CSV WARN] {job_id}: {e}", flush=True)

        # Pause between domains (like the working script)
        if idx < total:
            time.sleep(PAUSE_BETWEEN_DOMAINS)

    elapsed = time.time() - start

    # ── Bulk email verification (billable leads only) ──
    billable_emails = {}  # email -> list of result dicts that use it
    for r in all_results:
        tier, _ = classify_lead_tier(r)
        if tier == "not_billable":
            continue
        email = r.get("contact_email", "").strip()
        if email and "@" in email:
            billable_emails.setdefault(email.lower(), []).append(r)

    if billable_emails:
        progress_q.put({
            "type": "processing",
            "index": 0, "total": 0,
            "nonprofit": f"Verifying {len(billable_emails)} emails via Emailable bulk API...",
        })
        print(f"[BULK-VERIFY] Submitting {len(billable_emails)} billable emails", flush=True)

        bulk_results = validate_emails_bulk(list(billable_emails.keys()))

        verified_count = 0
        purged_count = 0
        for email_lower, results_using_it in billable_emails.items():
            state = bulk_results.get(email_lower, "unknown")
            for r in results_using_it:
                r["email_status"] = state
                if state != "deliverable":
                    original_email = r.get("contact_email", "")
                    r["contact_email"] = ""
                    r["contact_name"] = ""
                    r["email_status"] = ""
                    purged_count += 1
                    print(f"[BULK-VERIFY] Purged {original_email} ({state}) from {r.get('query_domain', '')}", flush=True)
                else:
                    verified_count += 1
                # Re-save updated result to DB
                if job_id:
                    save_single_result(job_id, r.get("query_domain", ""), r)

        print(f"[BULK-VERIFY] Done: {verified_count} deliverable, {purged_count} purged", flush=True)
        progress_q.put({
            "type": "processing",
            "index": 0, "total": 0,
            "nonprofit": f"Email verification complete: {verified_count} verified, {purged_count} purged",
        })
    else:
        print(f"[BULK-VERIFY] No billable emails to verify", flush=True)

    # Enrich missing address/phone from IRS database
    _enrich_from_irs(all_results)

    # Compute billing summary (only selected tiers)
    fee_cents = get_research_fee_cents(len(nonprofits))
    billing_summary["research_fees"] = len(nonprofits) * fee_cents
    tier_counts = {}
    for r in all_results:
        tier, price = classify_lead_tier(r)
        if price > 0 and tier in selected_tiers:
            if tier not in tier_counts:
                tier_counts[tier] = {"count": 0, "price_each": price, "total": 0}
            tier_counts[tier]["count"] += 1
            tier_counts[tier]["total"] += price
    billing_summary["lead_fees"] = tier_counts
    billing_summary["billable_lead_count"] = sum(t["count"] for t in tier_counts.values())
    billing_summary["total_charged"] = (
        billing_summary["research_fees"]
        + sum(t["total"] for t in tier_counts.values())
    )

    # Export ALL results with status "found" or "3rdpty_found" (not just billable)
    save_results = []
    for r in all_results:
        st = r.get("status", "")
        if st in ("found", "3rdpty_found"):
            save_results.append(r)
    print(f"[EXPORT] {len(save_results)} of {len(all_results)} found results included in export", flush=True)

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
            "model": "Auctionintel.app",
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
        complete_event["billable_count"] = billing_summary.get("billable_lead_count", 0)
    progress_q.put(complete_event)
    progress_q.put(None)  # sentinel

    jobs[job_id]["status"] = "complete"
    jobs[job_id]["results"] = output

    # Persist job completion to SQLite
    found_count = output["summary"].get("found", 0) + output["summary"].get("3rdpty_found", 0)
    billable_count = billing_summary.get("billable_lead_count", 0) if not is_admin else found_count
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

            # Check if job was stopped by user
            if jobs.get(job_id, {}).get("stop_requested"):
                emails.send_search_stopped(
                    user["email"], job_id,
                    processed=len(all_results), total=total,
                    found=found_count, charged_cents=total_cost_cents,
                )

            # Check balance-related emails
            final_balance = get_balance(user_id)
            if final_balance <= 0:
                emails.send_credit_exhausted(user["email"])
            elif final_balance < 500:  # below $5
                emails.send_low_balance_warning(user["email"], final_balance)


def _job_worker(
    nonprofits: List[str], job_id: str, progress_q,
    user_id: Optional[int] = None, is_admin: bool = False, is_trial: bool = False,
    selected_tiers: list = None, user_email: str = "",
):
    """Thread target that runs the job (synchronous — no async needed)."""
    try:
        _run_job(nonprofits, job_id, progress_q, user_id=user_id, is_admin=is_admin, is_trial=is_trial, selected_tiers=selected_tiers or ["decision_maker", "outreach_ready", "event_verified"], user_email=user_email)
    except Exception as e:
        print(f"[JOB ERROR] {job_id}: {type(e).__name__}: {e}", flush=True)
        progress_q.put({"type": "error", "message": str(e)})
        progress_q.put(None)
        jobs[job_id]["status"] = "error"
        fail_search_job(job_id, str(e))


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
    session["email_verified"] = False

    try:
        token = create_verification_token(user_id)
        verify_url = f"{DOMAIN}/verify-email?token={token}"
        emails.send_verification_email(email, verify_url)
    except Exception as e:
        print(f"[REGISTER] Failed to send verification email: {type(e).__name__}: {e}", flush=True)

    return redirect(url_for("verify_email_pending"))


@app.route("/login", methods=["GET"])
def login_page():
    return LOGIN_HTML


@app.route("/login", methods=["POST"])
def login_submit():
    ip = _get_client_ip()
    # 30 login attempts per IP per 5 minutes (relaxed for multi-account admin use)
    if _rate_limit(f"login:{ip}", 30, 300):
        return LOGIN_HTML.replace("<!-- error -->", '<p class="error">Too many login attempts. Try again in a few minutes.</p>'), 429
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = authenticate(email, password)
    if user:
        if user.get("is_banned"):
            return LOGIN_HTML.replace("<!-- error -->", '<p class="error">Your account has been suspended. Contact support for assistance.</p>')
        session["user_id"] = user["id"]
        session["is_admin"] = user["is_admin"]
        session["is_trial"] = user.get("is_trial", False)
        session["email_verified"] = user.get("email_verified", False)
        update_last_login(user["id"])
        if not user.get("email_verified") and not user.get("is_admin"):
            return redirect(url_for("verify_email_pending"))
        return redirect(url_for("database_page"))
    return LOGIN_HTML.replace("<!-- error -->", '<p class="error">Invalid email or password</p>')


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ─── Routes: Email Verification ──────────────────────────────────────────────

@app.route("/verify-email-pending")
def verify_email_pending():
    if not session.get("user_id"):
        return redirect(url_for("login_page"))
    if session.get("email_verified") or session.get("is_admin"):
        return redirect(url_for("database_page"))
    return VERIFY_EMAIL_PENDING_HTML


@app.route("/resend-verification", methods=["POST"])
def resend_verification():
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("login_page"))
    ip = _get_client_ip()
    # 3 resend attempts per IP per 15 minutes
    if _rate_limit(f"verify_resend:{ip}", 3, 900):
        return VERIFY_EMAIL_PENDING_HTML.replace(
            "<!-- message -->",
            '<p class="error">Too many resend attempts. Please wait a few minutes.</p>'
        ), 429
    user = get_user(uid)
    if user and not user.get("email_verified"):
        try:
            token = create_verification_token(uid)
            verify_url = f"{DOMAIN}/verify-email?token={token}"
            emails.send_verification_email(user["email"], verify_url)
        except Exception as e:
            print(f"[RESEND VERIFY] Failed: {type(e).__name__}: {e}", flush=True)
    return VERIFY_EMAIL_PENDING_HTML.replace(
        "<!-- message -->",
        '<p class="success">Verification email sent! Check your inbox.</p>'
    )


@app.route("/verify-email")
def verify_email():
    token = request.args.get("token", "")
    if not token:
        return FORGOT_PASSWORD_HTML.replace(
            "<!-- message -->",
            '<p class="error">Invalid verification link.</p>'
        )
    user_id = validate_verification_token(token)
    if not user_id:
        return FORGOT_PASSWORD_HTML.replace(
            "<!-- message -->",
            '<p class="error">This verification link has expired or already been used. Please log in and request a new one.</p>'
        )
    consume_verification_token(token)
    # Update session if this is the currently logged-in user
    if session.get("user_id") == user_id:
        session["email_verified"] = True
    # Send welcome email now that they're verified
    user = get_user(user_id)
    if user:
        try:
            is_trial = user.get("is_trial", False)
            emails.send_welcome(user["email"], is_trial=is_trial)
        except Exception as e:
            print(f"[VERIFY] Failed to send welcome email: {type(e).__name__}: {e}", flush=True)
    # Redirect to login so they can start using the app
    return LOGIN_HTML.replace(
        "<!-- error -->",
        '<p class="success">Email verified! You can now log in.</p>'
    )


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

    if amount_dollars < 10 or amount_dollars > 9999:
        return jsonify({"error": "Amount must be between $10 and $9,999"}), 400

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
                    emails.send_admin_wallet_topup(user["email"], amount_cents, new_balance)
                print(f"[STRIPE] Credited ${amount_cents/100:.2f} to user {user_id} (intent: {intent_id})")
            else:
                print(f"[STRIPE] Duplicate intent {intent_id}, skipping")

    elif event["type"] == "payment_intent.payment_failed":
        intent = event["data"]["object"]
        user_id = int(intent["metadata"].get("user_id", 0))
        amount_cents = intent.get("amount", 0)
        failure_msg = intent.get("last_payment_error", {}).get("message", "Card declined by issuer")
        if user_id:
            user = get_user(user_id)
            if user:
                emails.send_payment_failed(user["email"], amount_cents, failure_msg)
            print(f"[STRIPE] Payment failed for user {user_id}: {failure_msg}", flush=True)

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
    html = html.replace("{{EXCLUSIVE_FEES}}", f"${summary['exclusive_fees']/100:.2f}")
    html = html.replace("{{REFUNDS}}", f"${summary['refunds']/100:.2f}")
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
        return _load_new_landing()
    user = _current_user()
    if not user:
        session.clear()
        return _load_new_landing()
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
    valid_tiers = {"decision_maker", "outreach_ready", "event_verified"}
    selected_tiers = [t for t in data.get("selected_tiers", list(valid_tiers)) if t in valid_tiers]
    if not selected_tiers:
        selected_tiers = list(valid_tiers)
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
    # Conservative hit rate: 55%, avg lead price: $1.25 (weighted across 3 tiers)
    ESTIMATED_HIT_RATE = 0.55
    ESTIMATED_AVG_LEAD_CENTS = 125  # weighted average across 3 tiers

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
    progress_q = EventQueue()

    jobs[job_id] = {
        "status": "running",
        "nonprofits": nonprofits,
        "progress_queue": progress_q,
        "results": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "total": len(nonprofits),
    }

    # Persist job to SQLite
    create_search_job(user_id, job_id, len(nonprofits))

    _user_email = session.get("email", "")
    thread = threading.Thread(
        target=_job_worker,
        args=(nonprofits, job_id, progress_q),
        kwargs={"user_id": user_id, "is_admin": is_admin, "is_trial": is_trial, "selected_tiers": selected_tiers, "user_email": _user_email},
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

    # Replay from Last-Event-ID if reconnecting (0 = start from beginning)
    last_id = request.headers.get("Last-Event-ID", type=int, default=0)

    def generate():
        # Tell browser to reconnect after 3s on drop
        yield "retry: 3000\n\n"

        # Use events list as single source of truth (avoids dual-consumer queue race)
        cursor = last_id  # events are 1-indexed: cursor=0 means start, cursor=N means skip first N
        last_heartbeat = time.time()

        while True:
            # Send any new events that have accumulated
            had_data = False
            while cursor < len(progress_q.events):
                evt = progress_q.events[cursor]
                yield f"id: {cursor + 1}\ndata: {json.dumps(evt)}\n\n"
                cursor += 1
                had_data = True

            # Check if job is done
            job_status = jobs.get(job_id, {}).get("status", "")
            if job_status in ("complete", "error"):
                # Final drain of any remaining events
                while cursor < len(progress_q.events):
                    evt = progress_q.events[cursor]
                    yield f"id: {cursor + 1}\ndata: {json.dumps(evt)}\n\n"
                    cursor += 1
                break

            # Heartbeat every ~15s to keep proxy alive
            if not had_data:
                now = time.time()
                if now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                time.sleep(1)
            else:
                last_heartbeat = time.time()

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/stop/<job_id>", methods=["POST"])
@login_required
def stop_job(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    job = jobs[job_id]
    if job.get("status") != "running":
        return jsonify({"error": "Job not running"}), 400
    job["stop_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/active-job")
@login_required
def active_job():
    """Return the user's currently running job (if any)."""
    user_id = session["user_id"]
    for jid, job in jobs.items():
        if job.get("user_id") == user_id and job.get("status") == "running":
            processed = len(job.get("progress_queue", {}).events) if hasattr(job.get("progress_queue", {}), "events") else 0
            return jsonify({"job_id": jid, "status": "running", "total": job.get("total", 0)})
    return jsonify({"job_id": None})


@app.route("/api/job-status/<job_id>")
@login_required
def job_status(job_id):
    """Lightweight polling endpoint — fallback when SSE reconnects fail."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    resp = {"status": job["status"], "job_id": job_id}
    if job["status"] == "complete" and job.get("results"):
        resp["summary"] = job["results"].get("summary", {})
        resp["csv_file"] = f"{job_id}.csv"
        resp["json_file"] = f"{job_id}.json"
        resp["xlsx_file"] = f"{job_id}.xlsx"
        if job.get("user_id"):
            resp["balance"] = get_balance(job["user_id"])
        billing = job["results"].get("billing")
        if billing:
            resp["billing"] = billing
    elif job["status"] == "error":
        resp["error"] = "Job failed"
    return jsonify(resp)


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

    # Send confirmation email
    user = get_user(user_id)
    if user:
        emails.send_exclusive_lead_confirmed(user["email"], nonprofit_name, event_title, EXCLUSIVE_LEAD_PRICE_CENTS)

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
    table = "confirmed_auction_nonprofits"
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
        FROM {table}
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
    """Debug endpoint: test a single Poe bot call."""
    user = _current_user()
    if not user or not user.get("is_admin"):
        return jsonify({"error": "Admin only"}), 403
    try:
        import traceback
        poe_key = os.environ.get("POE_API_KEY", "")
        text = call_poe_bot_sync("redcross.org")
        return jsonify({
            "status": "ok", "bot": POE_BOT_NAME,
            "api_key_set": bool(poe_key), "api_key_prefix": poe_key[:8] + "..." if poe_key else "MISSING",
            "response_length": len(text), "preview": text[:500],
        })
    except Exception as e:
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


# ─── Routes: Legal / Footer Pages ───────────────────────────────────────────

_LEGAL_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; scrollbar-width:thin; scrollbar-color:#eab308 #0a0a0a; }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-track { background:#0a0a0a; }
  ::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }
  ::-webkit-scrollbar-thumb:hover { background:#ffd900; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #000; color: #f5f5f5; }
  .legal-wrap { max-width: 780px; margin: 60px auto; padding: 0 24px 80px; }
  .legal-wrap h1 { font-size: 26px; font-weight: 700; margin-bottom: 8px; }
  .legal-wrap .effective { font-size: 13px; color: #737373; margin-bottom: 32px; }
  .legal-wrap h2 { font-size: 18px; font-weight: 600; margin-top: 32px; margin-bottom: 10px; color: #eab308; }
  .legal-wrap h3 { font-size: 15px; font-weight: 600; margin-top: 20px; margin-bottom: 8px; color: #d4d4d4; }
  .legal-wrap p, .legal-wrap li { font-size: 14px; color: #a3a3a3; line-height: 1.8; }
  .legal-wrap ul { padding-left: 24px; margin-bottom: 12px; }
  .legal-wrap a { color: #eab308; text-decoration: none; }
  .legal-wrap a:hover { text-decoration: underline; }
  .back-link { display: inline-block; margin-bottom: 24px; font-size: 13px; color: #737373; }
  .back-link:hover { color: #eab308; }
"""

_DNS_FORM_STYLE = """
  .dns-form { background: #0a0a0a; border: 1px solid #262626; border-radius: 12px; padding: 28px; margin-top: 28px; }
  .dns-form label { display: block; font-size: 13px; color: #a3a3a3; margin-bottom: 6px; font-weight: 600; }
  .dns-form input, .dns-form textarea, .dns-form select {
    width: 100%; padding: 10px 14px; background: #000; border: 1px solid #333; border-radius: 8px;
    color: #f5f5f5; font-size: 14px; margin-bottom: 14px; outline: none; font-family: inherit;
  }
  .dns-form input:focus, .dns-form textarea:focus, .dns-form select:focus { border-color: #eab308; }
  .dns-form textarea { min-height: 100px; resize: vertical; }
  .dns-form select option { background: #111; }
  .dns-form button {
    padding: 12px 28px; background: #ffd900; color: #000; border: none; border-radius: 8px;
    font-size: 15px; font-weight: 700; cursor: pointer; margin-top: 4px;
  }
  .dns-form button:hover { background: #ca8a04; }
  .dns-form .form-note { font-size: 12px; color: #525252; margin-top: 12px; }
  .dns-form .success-msg { color: #4ade80; font-size: 14px; margin-top: 12px; display: none; }
"""


@app.route("/terms")
def terms_page():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Terms of Service - Auction Finder</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_LEGAL_STYLE}</style></head><body>
<div class="legal-wrap">
<a href="/" class="back-link">&larr; Back to Auction Finder</a>
<h1>Terms of Service</h1>
<p class="effective">Effective Date: February 25, 2026</p>

<h2>1. Acceptance of Terms</h2>
<p>By accessing or using Auction Finder ("Services"), you agree to be bound by these Terms of Service. If you do not agree, do not use the Services.</p>

<h2>2. Description of Services</h2>
<p>Auction Finder provides nonprofit event research tools, including web-based search, lead generation, database access, and related analytics. Results are generated using automated research and may include publicly available information.</p>

<h2>3. Account Registration</h2>
<p>You must create an account to use paid features. You are responsible for maintaining the confidentiality of your credentials and for all activity under your account. You must provide accurate information and promptly update it if it changes.</p>

<h2>4. Wallet, Payments, and Billing</h2>
<p>Auction Finder uses a prepaid wallet system. Funds added to your wallet are <strong>non-refundable</strong> except where required by law. Research fees are charged per nonprofit processed, whether or not a qualifying lead is found. Lead fees are charged according to the tier and billability rules displayed in the product. See our <a href="/refund-policy">Refund Policy</a> for details.</p>

<h2>5. Acceptable Use</h2>
<p>You agree not to:</p>
<ul>
<li>Use the Services for spam, harassment, or unlawful purposes</li>
<li>Scrape, resell, or redistribute data obtained from the Services without authorization</li>
<li>Attempt to reverse-engineer, interfere with, or compromise the platform</li>
<li>Create multiple accounts to abuse free trial credits</li>
<li>Misrepresent your identity or affiliation</li>
</ul>

<h2>6. Intellectual Property</h2>
<p>All content, design, and technology of the Services are owned by Auction Finder or its licensors. You may use results generated for your own internal business purposes. You may not reproduce or redistribute the platform itself.</p>

<h2>7. Data and Privacy</h2>
<p>Your use of the Services is subject to our <a href="/privacy-policy">Privacy Policy</a>. We process data as described therein.</p>

<h2>8. Disclaimers</h2>
<p>The Services are provided "as is" without warranties of any kind, express or implied. We do not guarantee the accuracy, completeness, or timeliness of any results. Event information, contact details, and other data may be outdated or incorrect.</p>

<h2>9. Limitation of Liability</h2>
<p>To the maximum extent permitted by law, Auction Finder shall not be liable for any indirect, incidental, special, consequential, or punitive damages, or any loss of profits or revenues, arising out of or related to your use of the Services.</p>

<h2>10. Termination</h2>
<p>We may suspend or terminate your account at any time for violation of these Terms or for any other reason at our discretion. You may close your account by contacting <a href="mailto:support@auctionintel.us">support@auctionintel.us</a>. Unused wallet credits are non-refundable upon termination except where required by law.</p>

<h2>11. Modifications</h2>
<p>We may update these Terms at any time. Continued use of the Services after changes constitutes acceptance of the revised Terms.</p>

<h2>12. Governing Law</h2>
<p>These Terms are governed by the laws of the State of Colorado, without regard to conflict of law principles.</p>

<h2>13. Contact</h2>
<p>Auction Finder<br>Email: <a href="mailto:support@auctionintel.us">support@auctionintel.us</a><br>Phone: 303-719-4851</p>
</div></body></html>"""


@app.route("/privacy-policy")
def privacy_policy_page():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy - Auction Finder</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_LEGAL_STYLE}</style></head><body>
<div class="legal-wrap">
<a href="/" class="back-link">&larr; Back to Auction Finder</a>
<h1>Privacy Policy</h1>
<p class="effective">Effective Date: February 25, 2026</p>

<p>Auction Finder ("we," "us," or "our") respects your privacy. This Privacy Policy explains how we collect, use, disclose, and protect personal information when you use our website, platform, and services (collectively, the "Services").</p>

<h2>1. Information We Collect</h2>
<h3>1.1 Account and Contact Information</h3>
<p>When you create an account or contact us, we may collect: name, email address, company name, phone number, and login credentials.</p>

<h3>1.2 Payment and Billing Information</h3>
<p>Payment transactions are processed by third-party processors (such as Stripe). We may receive transaction metadata (payment status, amount, transaction IDs, last 4 digits/card brand). We do <strong>not</strong> store full payment card numbers.</p>

<h3>1.3 Usage and Technical Information</h3>
<p>We may collect: IP address, browser type, device info, pages visited, feature usage, search activity, timestamps, log data, and cookies.</p>

<h3>1.4 Inputs and Research Requests</h3>
<p>We collect information you submit, including nonprofit names, domains, search filters, research requests, and export requests.</p>

<h3>1.5 Generated Results and Stored Data</h3>
<p>We may store generated outputs/results and lead records for the retention period described in the product (e.g., up to 180 days).</p>

<h2>2. How We Use Information</h2>
<p>We use information to: provide and operate the Services; process billing; generate search results; provide support; monitor security; improve features; enforce our Terms; and communicate service-related notices.</p>

<h2>3. How We Share Information</h2>
<h3>3.1 Service Providers</h3>
<p>Third-party vendors who help us operate the Services (hosting, analytics, payment processors like Stripe).</p>
<h3>3.2 Legal Compliance</h3>
<p>We may disclose information if required by law or to protect our rights, prevent fraud, or protect user safety.</p>
<h3>3.3 Business Transfers</h3>
<p>Information may be transferred as part of a merger, acquisition, or sale of assets.</p>
<p>We do not sell your personal information for money.</p>

<h2>4. Data Retention</h2>
<p>We retain personal information as long as needed to provide the Services, comply with legal obligations, and enforce agreements. Result data may be deleted after the stated retention period (e.g., 180 days).</p>

<h2>5. Data Security</h2>
<p>We use reasonable safeguards to protect personal information. However, no system is 100% secure.</p>

<h2>6. Your Choices and Rights</h2>
<p>Depending on your location, you may have rights to request access, correction, deletion, or a copy of your information. Contact us at <a href="mailto:support@auctionintel.us">support@auctionintel.us</a> to make a privacy request.</p>

<h2>7. Cookies</h2>
<p>We use cookies to keep you signed in, remember preferences, and analyze usage. You can control cookies through your browser settings.</p>

<h2>8. Third-Party Services</h2>
<p>The Services may link to third-party websites. We are not responsible for their privacy practices.</p>

<h2>9. Children's Privacy</h2>
<p>The Services are not directed to children under 13. We do not knowingly collect information from children under 13.</p>

<h2>10. International Users</h2>
<p>If you access the Services from outside the United States, your information may be transferred to and processed in the United States.</p>

<h2>11. Changes</h2>
<p>We may update this Privacy Policy from time to time. Material changes will be posted with a revised date.</p>

<h2>12. Contact</h2>
<p>Auction Finder<br>Email: <a href="mailto:support@auctionintel.us">support@auctionintel.us</a><br>Phone: 303-719-4851</p>
</div></body></html>"""


@app.route("/refund-policy")
def refund_policy_page():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Refund Policy - Auction Finder</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_LEGAL_STYLE}</style></head><body>
<div class="legal-wrap">
<a href="/" class="back-link">&larr; Back to Auction Finder</a>
<h1>Refund Policy</h1>
<p class="effective">Effective Date: February 25, 2026</p>

<h2>1. Wallet Credits Are Non-Refundable</h2>
<p>All funds added to your Auction Finder wallet are <strong>non-refundable</strong>, except where required by applicable law. This includes wallet top-ups used for research fees, lead fees, and other in-platform charges.</p>

<h2>2. No Expiration of Wallet Credits</h2>
<p>Wallet credits do <strong>not</strong> expire unless otherwise stated in writing by Auction Finder.</p>

<h2>3. Research Fees Are Charged for Work Performed</h2>
<p>Research fees are charged per nonprofit researched, whether or not a qualifying event lead is found. This is because Auction Finder performs compute- and web-research work for each nonprofit processed.</p>

<h2>4. Lead Charges and Billability Rules</h2>
<p>Lead charges are applied according to the tier(s) selected and the platform's billability rules shown in the product at the time of the search. Where the product states a verified event page link is required for a billable lead, leads without a qualifying link are not charged.</p>

<h2>5. Mistaken Charges / Billing Errors</h2>
<p>If you believe you were charged in error (duplicate charge, technical malfunction, or billing bug), contact support within <strong>14 days</strong> of the charge with: your account email, date/time of charge, amount, search/job ID (if available), and a description of the issue. We will review and may issue a correction, credit adjustment, or refund at our discretion.</p>

<h2>6. Promotional and Free Trial Credits</h2>
<p>Promotional credits, trial credits, and bonus credits are not redeemable for cash, are non-transferable, may expire per promotion terms, and may be revoked if issued in error or abused.</p>

<h2>7. Chargebacks</h2>
<p>Before initiating a chargeback, please contact us so we can resolve the issue. Fraudulent or abusive chargebacks may result in account suspension or termination.</p>

<h2>8. Contact</h2>
<p>Auction Finder<br>Email: <a href="mailto:support@auctionintel.us">support@auctionintel.us</a><br>Phone: 303-719-4851</p>
</div></body></html>"""


@app.route("/contact")
def contact_dmca_page():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contact / DMCA / Abuse - Auction Finder</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_LEGAL_STYLE}</style></head><body>
<div class="legal-wrap">
<a href="/" class="back-link">&larr; Back to Auction Finder</a>
<h1>Contact, DMCA &amp; Abuse Policy</h1>
<p class="effective">Effective Date: February 25, 2026</p>

<p>Auction Finder ("we," "us," "our") provides tools to help users discover nonprofit event information and related contact data.</p>

<h2>1. Contact Us</h2>
<p>For questions, support, billing issues, or general inquiries:</p>
<p>Email: <a href="mailto:support@auctionintel.us">support@auctionintel.us</a><br>Phone: 303-719-4851</p>

<h2>2. Report Abuse or Misuse</h2>
<p>If you believe the platform is being used for spam, harassment, unlawful activity, or other misuse, contact us at <a href="mailto:support@auctionintel.us">support@auctionintel.us</a> with:</p>
<ul>
<li>Your name and contact info</li>
<li>A description of the issue</li>
<li>Relevant URLs, screenshots, or examples</li>
<li>The account email (if known)</li>
</ul>
<p>We may investigate and take action, including warning, suspending, or terminating accounts.</p>

<h2>3. Copyright (DMCA) Notices</h2>
<p>If you believe content accessible through our Services infringes your copyright, send a notice to <a href="mailto:support@auctionintel.us">support@auctionintel.us</a> with the subject line "DMCA Notice" and include:</p>
<ul>
<li>Identification of the copyrighted work claimed to be infringed</li>
<li>Identification of the material and where it appears (URL and details)</li>
<li>Your name, address, phone number, and email</li>
<li>A statement of good-faith belief the use is not authorized</li>
<li>A statement under penalty of perjury that the notice is accurate and you are authorized to act</li>
<li>Your physical or electronic signature</li>
</ul>
<p>If you believe a DMCA notice was submitted in error, you may send a counter-notice to the same email.</p>

<h2>4. Law Enforcement Requests</h2>
<p>Law enforcement may submit requests to <a href="mailto:support@auctionintel.us">support@auctionintel.us</a>. We will respond as required by law and may require valid legal process.</p>
</div></body></html>"""


@app.route("/do-not-sell")
def do_not_sell_page():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Do Not Sell or Share My Information - Auction Finder</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_LEGAL_STYLE}{_DNS_FORM_STYLE}</style></head><body>
<div class="legal-wrap">
<a href="/" class="back-link">&larr; Back to Auction Finder</a>
<h1>Do Not Sell or Share My Information</h1>
<p class="effective">Effective Date: February 25, 2026</p>

<p>Auction Finder respects privacy requests from individuals and nonprofit representatives. If you believe your personal information appears in Auction Finder results, you may request that we remove or suppress your information, correct inaccurate information, or opt out of the sale or sharing of your personal information.</p>

<h2>Who Can Submit a Request</h2>
<ul>
<li>The person whose information appears in Auction Finder results</li>
<li>An authorized representative of a nonprofit/organization requesting review of listed contact information</li>
</ul>

<h2>Submit a Request</h2>
<div class="dns-form">
  <form id="dnsForm" onsubmit="return submitDNS(event)">
    <label for="dns_name">Your Full Name *</label>
    <input type="text" id="dns_name" name="name" required placeholder="Jane Smith">

    <label for="dns_email">Your Email Address *</label>
    <input type="email" id="dns_email" name="email" required placeholder="jane@example.com">

    <label for="dns_type">Request Type *</label>
    <select id="dns_type" name="request_type" required>
      <option value="">Select a request type...</option>
      <option value="remove">Remove / Suppress My Information</option>
      <option value="do_not_sell">Do Not Sell or Share My Personal Information</option>
      <option value="correct">Correct My Information</option>
    </select>

    <label for="dns_org">Nonprofit / Organization Name (if applicable)</label>
    <input type="text" id="dns_org" name="organization" placeholder="Example Foundation">

    <label for="dns_info">Information to be removed, corrected, or opted out *</label>
    <textarea id="dns_info" name="info_description" required placeholder="Describe the information (e.g., name, email, phone number) and where it appears..."></textarea>

    <label for="dns_urls">Relevant URLs or event page links (if known)</label>
    <input type="text" id="dns_urls" name="urls" placeholder="https://...">

    <button type="submit">Submit Request</button>
    <p class="form-note">We may contact you at the email provided to verify your identity before processing. Requests are typically reviewed within 10 business days.</p>
    <p class="success-msg" id="dnsSuccess">Your request has been submitted. We will contact you at the email address provided.</p>
  </form>
</div>

<h2 style="margin-top:36px;">What Happens After You Submit</h2>
<ul>
<li>We review the request and verify details as needed</li>
<li>We may remove, suppress, correct, or otherwise process the requested information</li>
<li>We will respond to you at the email address provided</li>
</ul>

<h2>Important Limitations</h2>
<p>In some cases, we may retain limited information as required for legal compliance, security and fraud prevention, billing and audit records, or internal recordkeeping. Backups may persist for a limited period before being overwritten.</p>

<h2>No Discrimination</h2>
<p>We will not deny service or provide a different level/quality of service solely because you submitted a privacy request, except as permitted by law.</p>

<h2>Contact</h2>
<p>If you prefer to submit your request by email: <a href="mailto:support@auctionintel.us">support@auctionintel.us</a><br>Subject line: <strong>Privacy Request</strong>, <strong>Remove My Information</strong>, or <strong>Do Not Sell or Share</strong><br>Phone: 303-719-4851</p>
</div>

<script>
function submitDNS(e) {{
  e.preventDefault();
  var f = document.getElementById('dnsForm');
  var name = document.getElementById('dns_name').value.trim();
  var email = document.getElementById('dns_email').value.trim();
  var type = document.getElementById('dns_type').value;
  var org = document.getElementById('dns_org').value.trim();
  var info = document.getElementById('dns_info').value.trim();
  var urls = document.getElementById('dns_urls').value.trim();
  if (!name || !email || !type || !info) {{ alert('Please fill in all required fields.'); return false; }}
  var subject = encodeURIComponent('Privacy Request: ' + type + ' - ' + name);
  var body = encodeURIComponent('Name: ' + name + '\\nEmail: ' + email + '\\nRequest Type: ' + type + '\\nOrganization: ' + (org || 'N/A') + '\\nInformation: ' + info + '\\nURLs: ' + (urls || 'N/A'));
  window.location.href = 'mailto:support@auctionintel.us?subject=' + subject + '&body=' + body;
  document.getElementById('dnsSuccess').style.display = 'block';
  return false;
}}
</script>
</body></html>"""


# ─── Routes: Admin Panel ──────────────────────────────────────────────────────

_ADMIN_STARTED_AT = time.time()

def _admin_required(f):
    """Decorator: login_required + admin check."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not _is_admin():
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/")
@_admin_required
def admin_dashboard():
    kpis = admin_get_kpis()
    html = ADMIN_DASHBOARD_HTML
    html = _inject_sidebar(html, "admin-dashboard")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    # Revenue cards
    html = html.replace("{{TOTAL_REVENUE}}", f"${kpis['total_topups']/100:,.2f}")
    html = html.replace("{{REVENUE_TODAY}}", f"${kpis['topups_today']/100:,.2f}")
    html = html.replace("{{REVENUE_WEEK}}", f"${kpis['topups_week']/100:,.2f}")
    html = html.replace("{{NET_REVENUE}}", f"${kpis['net_revenue']/100:,.2f}")
    # User cards
    html = html.replace("{{TOTAL_USERS}}", str(kpis["total_users"]))
    html = html.replace("{{TRIAL_USERS}}", str(kpis["trial_users"]))
    html = html.replace("{{VERIFIED_USERS}}", str(kpis["verified_users"]))
    html = html.replace("{{SIGNUPS_WEEK}}", str(kpis["signups_week"]))
    # Operations cards
    html = html.replace("{{TOTAL_JOBS}}", str(kpis["total_jobs"]))
    html = html.replace("{{RUNNING_JOBS}}", str(kpis["running_jobs"]))
    html = html.replace("{{TOTAL_LEADS}}", str(kpis["total_leads"]))
    html = html.replace("{{BILLABLE_LEADS}}", str(kpis["billable_leads"]))
    # Health cards
    html = html.replace("{{OPEN_TICKETS}}", str(kpis["open_tickets"]))
    html = html.replace("{{CACHE_ENTRIES}}", str(kpis["cache_entries"]))
    html = html.replace("{{EXCLUSIVE_SOLD}}", str(kpis["exclusive_sold"]))
    html = html.replace("{{BANNED_USERS}}", str(kpis["banned_users"]))
    return html


@app.route("/admin/users")
@_admin_required
def admin_users_page():
    users = admin_get_all_users()
    html = ADMIN_USERS_HTML
    html = _inject_sidebar(html, "admin-users")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    rows = ""
    for u in users:
        badges = ""
        if u.get("is_trial"):
            badges += '<span class="badge badge-trial">Trial</span> '
        if u.get("is_banned"):
            badges += '<span class="badge badge-banned">Banned</span> '
        if u.get("email_verified"):
            badges += '<span class="badge badge-verified">Verified</span> '
        last_login = u.get("last_login_at", "Never") or "Never"
        if last_login != "Never":
            last_login = last_login[:19]
        created = str(u.get("created_at", ""))[:10]
        rows += (
            f'<tr class="user-row" data-search="{html_escape(u["email"])} {html_escape(u.get("company",""))}">'
            f'<td><a href="/admin/users/{u["id"]}" class="user-link">{html_escape(u["email"])}</a></td>'
            f'<td>{html_escape(u.get("company","") or "-")}</td>'
            f'<td>${u["balance_cents"]/100:,.2f}</td>'
            f'<td>${u["total_spent"]/100:,.2f}</td>'
            f'<td>{u["job_count"]}</td>'
            f'<td>{badges}</td>'
            f'<td>{last_login}</td>'
            f'<td>{created}</td>'
            f'</tr>\n'
        )
    html = html.replace("{{USER_ROWS}}", rows)
    html = html.replace("{{USER_COUNT}}", str(len(users)))
    return html


@app.route("/admin/users/<int:user_id>")
@_admin_required
def admin_user_detail(user_id):
    u = admin_get_user_detail(user_id)
    if not u:
        return redirect("/admin/users")
    spending = get_spending_summary(user_id)
    txns = get_transactions(user_id, 50)
    user_jobs = get_user_jobs(user_id, 50)
    leads = get_user_exclusive_leads(user_id)
    tickets = get_tickets_for_user(user_id)

    html = ADMIN_USER_DETAIL_HTML
    html = _inject_sidebar(html, "admin-users")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    html = html.replace("{{USER_ID}}", str(u["id"]))
    html = html.replace("{{USER_EMAIL}}", html_escape(u["email"]))
    html = html.replace("{{USER_PHONE}}", html_escape(u.get("phone", "") or "-"))
    html = html.replace("{{USER_COMPANY}}", html_escape(u.get("company", "") or "-"))
    html = html.replace("{{USER_CREATED}}", str(u.get("created_at", ""))[:19])
    html = html.replace("{{USER_LAST_LOGIN}}", str(u.get("last_login_at", "Never") or "Never")[:19])
    html = html.replace("{{USER_BALANCE}}", f"${u['balance_cents']/100:,.2f}")
    html = html.replace("{{USER_TOTAL_SPENT}}", f"${spending['total_spent']/100:,.2f}")
    html = html.replace("{{USER_TOTAL_TOPUPS}}", f"${spending['total_topups']/100:,.2f}")
    html = html.replace("{{USER_JOB_COUNT}}", str(spending["job_count"]))

    # Status badges
    badges = ""
    if u.get("is_trial"):
        badges += '<span class="badge badge-trial">Trial</span> '
    if u.get("is_banned"):
        badges += '<span class="badge badge-banned">Banned</span> '
    if u.get("email_verified"):
        badges += '<span class="badge badge-verified">Verified</span> '
    html = html.replace("{{USER_BADGES}}", badges)
    html = html.replace("{{BAN_ACTION}}", "unban" if u.get("is_banned") else "ban")
    html = html.replace("{{BAN_LABEL}}", "Unban User" if u.get("is_banned") else "Ban User")
    html = html.replace("{{BAN_CLASS}}", "btn-success" if u.get("is_banned") else "btn-danger")

    # Jobs tab
    job_rows = ""
    for j in user_jobs:
        job_rows += (
            f'<tr><td>{j["job_id"][:12]}...</td><td>{j["status"]}</td>'
            f'<td>{j["nonprofit_count"]}</td><td>{j["found_count"]}</td>'
            f'<td>{j["billable_count"]}</td><td>${j["total_cost_cents"]/100:,.2f}</td>'
            f'<td>{str(j.get("created_at",""))[:16]}</td></tr>\n'
        )
    html = html.replace("{{JOB_ROWS}}", job_rows or '<tr><td colspan="7" style="text-align:center;color:#525252;">No jobs yet</td></tr>')

    # Transactions tab
    txn_rows = ""
    for t in txns:
        color = "#4ade80" if t["amount_cents"] > 0 else "#f87171"
        txn_rows += (
            f'<tr><td>{str(t.get("created_at",""))[:16]}</td><td>{t["type"]}</td>'
            f'<td style="color:{color}">${t["amount_cents"]/100:,.2f}</td>'
            f'<td>{html_escape(t.get("description","") or "")}</td></tr>\n'
        )
    html = html.replace("{{TXN_ROWS}}", txn_rows or '<tr><td colspan="4" style="text-align:center;color:#525252;">No transactions</td></tr>')

    # Exclusive leads tab
    lead_rows = ""
    for l in leads:
        lead_rows += (
            f'<tr><td>{html_escape(l.get("nonprofit_name",""))}</td>'
            f'<td>{html_escape(l.get("event_title",""))}</td>'
            f'<td><a href="{html_escape(l.get("event_url",""))}" target="_blank" style="color:#eab308;">Link</a></td></tr>\n'
        )
    html = html.replace("{{LEAD_ROWS}}", lead_rows or '<tr><td colspan="3" style="text-align:center;color:#525252;">No exclusive leads</td></tr>')

    # Tickets tab
    ticket_rows = ""
    for tk in tickets:
        ticket_rows += (
            f'<tr><td>#{tk["id"]}</td><td>{html_escape(tk.get("subject",""))}</td>'
            f'<td>{tk["status"]}</td><td>{str(tk.get("created_at",""))[:16]}</td></tr>\n'
        )
    html = html.replace("{{TICKET_ROWS}}", ticket_rows or '<tr><td colspan="4" style="text-align:center;color:#525252;">No tickets</td></tr>')

    return html


@app.route("/admin/users/<int:user_id>/ban", methods=["POST"])
@_admin_required
def admin_toggle_ban(user_id):
    action = request.form.get("action", "ban")
    if action == "unban":
        admin_unban_user(user_id)
        print(f"[ADMIN] Unbanned user {user_id}", flush=True)
    else:
        admin_ban_user(user_id)
        print(f"[ADMIN] Banned user {user_id}", flush=True)
    return redirect(f"/admin/users/{user_id}")


@app.route("/admin/users/<int:user_id>/wallet", methods=["POST"])
@_admin_required
def admin_wallet_adjust(user_id):
    try:
        amount_dollars = float(request.form.get("amount", 0))
        reason = request.form.get("reason", "Admin adjustment").strip()
        amount_cents = int(amount_dollars * 100)
        if amount_cents == 0:
            return redirect(f"/admin/users/{user_id}")
        admin_adjust_wallet(user_id, amount_cents, reason)
        print(f"[ADMIN] Wallet adjust user {user_id}: ${amount_dollars:,.2f} — {reason}", flush=True)
    except (ValueError, TypeError) as e:
        print(f"[ADMIN] Wallet adjust error: {e}", flush=True)
    return redirect(f"/admin/users/{user_id}")


@app.route("/admin/revenue")
@_admin_required
def admin_revenue_page():
    timeline = admin_get_revenue_timeline(30)
    spenders = admin_get_top_spenders(10)

    html = ADMIN_REVENUE_HTML
    html = _inject_sidebar(html, "admin-revenue")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))

    # Summary cards from first row totals
    total_topups = sum(r["topups"] for r in timeline)
    total_research = sum(r["research"] for r in timeline)
    total_leads = sum(r["leads"] for r in timeline)
    total_exclusive = sum(r["exclusive"] for r in timeline)
    total_refunds = sum(r["refunds"] for r in timeline)
    html = html.replace("{{TOTAL_TOPUPS_30D}}", f"${total_topups/100:,.2f}")
    html = html.replace("{{TOTAL_RESEARCH_30D}}", f"${total_research/100:,.2f}")
    html = html.replace("{{TOTAL_LEADS_30D}}", f"${total_leads/100:,.2f}")
    html = html.replace("{{TOTAL_EXCLUSIVE_30D}}", f"${total_exclusive/100:,.2f}")
    html = html.replace("{{TOTAL_REFUNDS_30D}}", f"${total_refunds/100:,.2f}")

    # Daily revenue table
    day_rows = ""
    for r in timeline:
        day_rows += (
            f'<tr><td>{r["date"]}</td><td>${r["topups"]/100:,.2f}</td>'
            f'<td>${r["research"]/100:,.2f}</td><td>${r["leads"]/100:,.2f}</td>'
            f'<td>${r["exclusive"]/100:,.2f}</td><td>${r["refunds"]/100:,.2f}</td>'
            f'<td>${r["net"]/100:,.2f}</td></tr>\n'
        )
    html = html.replace("{{DAILY_ROWS}}", day_rows)

    # Top spenders
    spender_rows = ""
    for s in spenders:
        spender_rows += (
            f'<tr><td><a href="/admin/users/{s["id"]}" style="color:#eab308;">{html_escape(s["email"])}</a></td>'
            f'<td>{html_escape(s.get("company","") or "-")}</td>'
            f'<td>${s["total_spent"]/100:,.2f}</td>'
            f'<td>${s["total_topups"]/100:,.2f}</td>'
            f'<td>{s["job_count"]}</td></tr>\n'
        )
    html = html.replace("{{SPENDER_ROWS}}", spender_rows or '<tr><td colspan="5" style="text-align:center;color:#525252;">No data yet</td></tr>')

    return html


@app.route("/admin/activity")
@_admin_required
def admin_activity_page():
    recent_jobs = admin_get_recent_activity(50)
    recent_logins = admin_get_recent_logins(20)

    html = ADMIN_ACTIVITY_HTML
    html = _inject_sidebar(html, "admin-activity")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))

    # Recent jobs
    job_rows = ""
    for j in recent_jobs:
        status_color = {"running": "#eab308", "complete": "#4ade80", "error": "#f87171"}.get(j["status"], "#a3a3a3")
        rebuild_btn = ""
        if j["status"] == "error":
            rebuild_btn = f' <button onclick="rebuildJob(\'{j["job_id"]}\')" style="background:#eab308;color:#000;border:none;padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;font-weight:600;">Rebuild</button>'
        job_rows += (
            f'<tr><td>{j["job_id"][:12]}...</td>'
            f'<td><a href="/admin/users/{j.get("id","")}" style="color:#eab308;">{html_escape(j.get("user_email",""))}</a></td>'
            f'<td style="color:{status_color}">{j["status"]}{rebuild_btn}</td>'
            f'<td>{j["nonprofit_count"]}</td><td>{j["found_count"]}</td>'
            f'<td>{j["billable_count"]}</td><td>${j["total_cost_cents"]/100:,.2f}</td>'
            f'<td>{str(j.get("created_at",""))[:16]}</td>'
            f'<td>{str(j.get("completed_at","") or "-")[:16]}</td></tr>\n'
        )
    html = html.replace("{{JOB_ROWS}}", job_rows or '<tr><td colspan="9" style="text-align:center;color:#525252;">No jobs yet</td></tr>')

    # Recent logins
    login_rows = ""
    for l in recent_logins:
        status = "Banned" if l.get("is_banned") else ("Trial" if l.get("is_trial") else "Active")
        login_rows += (
            f'<tr><td>{html_escape(l["email"])}</td>'
            f'<td>{str(l.get("last_login_at",""))[:19]}</td>'
            f'<td>{status}</td></tr>\n'
        )
    html = html.replace("{{LOGIN_ROWS}}", login_rows or '<tr><td colspan="3" style="text-align:center;color:#525252;">No logins recorded</td></tr>')

    return html


@app.route("/admin/system")
@_admin_required
def admin_system_page():
    cache_stats = admin_get_cache_stats()
    drip_stats = admin_get_drip_stats()

    html = ADMIN_SYSTEM_HTML
    html = _inject_sidebar(html, "admin-system")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))

    # Running jobs
    running = [(jid, j) for jid, j in jobs.items() if j.get("status") == "running"]
    run_rows = ""
    for jid, j in running:
        run_rows += (
            f'<tr><td>{jid[:12]}...</td><td>{j.get("processed",0)}/{j.get("total",0)}</td>'
            f'<td>{j.get("found",0)}</td></tr>\n'
        )
    html = html.replace("{{RUNNING_ROWS}}", run_rows or '<tr><td colspan="3" style="text-align:center;color:#525252;">No jobs running</td></tr>')
    html = html.replace("{{RUNNING_COUNT}}", str(len(running)))

    # Cache stats
    cache_rows = ""
    total_cache = 0
    for c in cache_stats:
        total_cache += c["count"]
        cache_rows += (
            f'<tr><td>{c["status"]}</td><td>{c["count"]}</td>'
            f'<td>{str(c.get("oldest",""))[:19]}</td>'
            f'<td>{str(c.get("newest",""))[:19]}</td></tr>\n'
        )
    html = html.replace("{{CACHE_ROWS}}", cache_rows or '<tr><td colspan="4" style="text-align:center;color:#525252;">No cache entries</td></tr>')
    html = html.replace("{{CACHE_TOTAL}}", str(total_cache))

    # Drip stats
    drip_rows = ""
    for d in drip_stats:
        drip_rows += (
            f'<tr><td>{d["drip_key"]}</td><td>{d["send_count"]}</td>'
            f'<td>{str(d.get("last_sent",""))[:19]}</td></tr>\n'
        )
    html = html.replace("{{DRIP_ROWS}}", drip_rows or '<tr><td colspan="3" style="text-align:center;color:#525252;">No drip emails sent</td></tr>')

    # Server info
    uptime_secs = int(time.time() - _ADMIN_STARTED_AT)
    uptime_hours = uptime_secs // 3600
    uptime_mins = (uptime_secs % 3600) // 60
    html = html.replace("{{PYTHON_VERSION}}", sys.version.split()[0])
    html = html.replace("{{UPTIME}}", f"{uptime_hours}h {uptime_mins}m")

    return html


@app.route("/admin/batch-runner")
@_admin_required
def admin_batch_runner():
    """Admin page showing all jobs from batch runner with real-time status and download links."""
    try:
        from db import _get_conn
        import json as _jmod
        conn = _get_conn()
        cur = conn.cursor()

        # Get all unique jobs with their stats
        cur.execute("""
            SELECT
                job_id,
                COUNT(*) as total_domains,
                COUNT(CASE WHEN result_json IS NOT NULL THEN 1 END) as processed,
                MIN(created_at) as started_at
            FROM job_results
            GROUP BY job_id
            ORDER BY MIN(created_at) DESC
            LIMIT 50
        """)
        rows = cur.fetchall()

        jobs_html = ""
        if not rows:
            jobs_html = '<p style="color:#737373;text-align:center;padding:40px;">No jobs found. Run batch_runner.py to create jobs.</p>'
        else:
            for row in rows:
                job_id = row[0]
                total_domains = row[1]
                processed = row[2]
                started_at = row[3]

                # Get found count and tier breakdown
                cur.execute("""
                    SELECT result_json FROM job_results
                    WHERE job_id = %s AND result_json IS NOT NULL
                """, (job_id,))
                result_rows = cur.fetchall()

                found_count = 0
                dm_count = 0
                or_count = 0
                ev_count = 0

                for r in result_rows:
                    try:
                        result = _jmod.loads(r[0]) if isinstance(r[0], str) else r[0]
                        if result.get("status") in ("found", "3rdpty_found"):
                            found_count += 1
                            tier, price = classify_lead_tier(result)
                            if tier == "decision_maker":
                                dm_count += 1
                            elif tier == "outreach_ready":
                                or_count += 1
                            elif tier == "event_verified":
                                ev_count += 1
                    except Exception:
                        pass

                # Determine job status
                if processed >= total_domains:
                    status_class = "status-complete"
                    status_text = "Complete"
                elif job_id in jobs:
                    status_class = "status-running"
                    status_text = f"Running ({processed}/{total_domains})"
                else:
                    status_class = "status-complete"
                    status_text = "Complete"

                # Check if files exist
                csv_exists = get_result_file(job_id, "csv") is not None
                json_exists = get_result_file(job_id, "json") is not None

                # Build download links
                download_links = ""
                if csv_exists:
                    download_links += f'<a href="/results/{job_id}/csv" class="btn-gold">Download CSV</a>'
                if json_exists:
                    download_links += f'<a href="/results/{job_id}/json" class="btn-outline">Download JSON</a>'
                if not csv_exists and not json_exists and processed > 0:
                    download_links += f'<form method="POST" action="/admin/rebuild-job/{job_id}" style="display:inline;"><button type="submit" class="btn-outline">Rebuild Files</button></form>'

                started_str = started_at.strftime('%Y-%m-%d %H:%M:%S') if started_at else 'Unknown'

                jobs_html += f"""
                <div class="job-card">
                    <div class="job-header">
                        <div class="job-id">{html_escape(job_id)}</div>
                        <div class="job-status {status_class}">{status_text}</div>
                    </div>
                    <div class="job-stats">
                        <div class="stat">
                            <div class="stat-value">{processed}/{total_domains}</div>
                            <div>Processed</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value green">{found_count}</div>
                            <div>Found</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value gold">{dm_count}</div>
                            <div>Decision Makers</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value">{or_count}</div>
                            <div>Outreach Ready</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value">{ev_count}</div>
                            <div>Event Verified</div>
                        </div>
                    </div>
                    <div class="job-actions">
                        {download_links}
                    </div>
                    <div style="font-size:10px;color:#525252;margin-top:8px;">Started: {started_str}</div>
                </div>
                """

        cur.close()

        html = ADMIN_BATCH_RUNNER_HTML
        html = _inject_sidebar(html, "admin-batch-runner")
        html = _inject_nav_badge(html)
        html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
        html = html.replace("{{JOBS_HTML}}", jobs_html)

        return html
    except Exception as e:
        print(f"[ADMIN BATCH RUNNER] CRASH: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return f"<h1>Batch Runner Error</h1><pre>{type(e).__name__}: {e}</pre>", 500


@app.route("/admin/results")
@_admin_required
def admin_results_page():
  try:
    from db import _get_conn
    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    # Get counts directly from SQL — no loading JSON blobs
    cur.execute("""
        SELECT status, COUNT(*) FROM research_cache
        WHERE expires_at > NOW()
        GROUP BY status
    """)
    status_counts = {}
    for row in cur.fetchall():
        status_counts[row[0]] = row[1]
    total = sum(status_counts.values())

    # Get expiry buckets via SQL
    cur.execute("""
        SELECT
            SUM(CASE WHEN expires_at <= NOW() THEN 1 ELSE 0 END),
            SUM(CASE WHEN expires_at > NOW() AND expires_at <= NOW() + INTERVAL '7 days' THEN 1 ELSE 0 END),
            SUM(CASE WHEN expires_at > NOW() + INTERVAL '7 days' AND expires_at <= NOW() + INTERVAL '30 days' THEN 1 ELSE 0 END),
            SUM(CASE WHEN expires_at > NOW() + INTERVAL '30 days' THEN 1 ELSE 0 END)
        FROM research_cache
    """)
    exp_row = cur.fetchone()
    expiry_buckets = {
        "expired": exp_row[0] or 0,
        "lt_7d": exp_row[1] or 0,
        "lt_30d": exp_row[2] or 0,
        "gt_30d": exp_row[3] or 0,
    }

    # Tier counts — need JSON but only for found results
    cur.execute("""
        SELECT result_json FROM research_cache
        WHERE status IN ('found', '3rdpty_found')
        AND expires_at > NOW()
    """)
    import json as _jmod
    tier_counts = {}
    for row in cur.fetchall():
        try:
            result = _jmod.loads(row[0]) if isinstance(row[0], str) else row[0]
            tier, price = classify_lead_tier(result)
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        except Exception:
            pass
    cur.close()

    found_count = status_counts.get("found", 0) + status_counts.get("3rdpty_found", 0)
    not_found_count = status_counts.get("not_found", 0)
    error_count = status_counts.get("error", 0) + status_counts.get("uncertain", 0)

    dm_count = tier_counts.get("decision_maker", 0)
    or_count = tier_counts.get("outreach_ready", 0)
    ev_count = tier_counts.get("event_verified", 0)

    html = ADMIN_RESULTS_HTML
    html = _inject_sidebar(html, "admin-results")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    html = html.replace("{{TOTAL_CACHED}}", str(total))
    html = html.replace("{{FOUND_COUNT}}", str(found_count))
    html = html.replace("{{NOT_FOUND_COUNT}}", str(not_found_count))
    html = html.replace("{{ERROR_COUNT}}", str(error_count))
    html = html.replace("{{DM_COUNT}}", str(dm_count))
    html = html.replace("{{OR_COUNT}}", str(or_count))
    html = html.replace("{{EV_COUNT}}", str(ev_count))
    html = html.replace("{{EXPIRY_EXPIRED}}", str(expiry_buckets["expired"]))
    html = html.replace("{{EXPIRY_7D}}", str(expiry_buckets["lt_7d"]))
    html = html.replace("{{EXPIRY_30D}}", str(expiry_buckets["lt_30d"]))
    html = html.replace("{{EXPIRY_GT30D}}", str(expiry_buckets["gt_30d"]))

    # Build status breakdown table
    status_rows = ""
    for st, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        status_rows += f'<tr><td>{html_escape(st)}</td><td>{cnt}</td></tr>\n'
    html = html.replace("{{STATUS_ROWS}}", status_rows or '<tr><td colspan="2" style="text-align:center;color:#525252;">No entries</td></tr>')

    return html
  except Exception as e:
    print(f"[ADMIN RESULTS] CRASH: {type(e).__name__}: {e}", flush=True)
    import traceback; traceback.print_exc()
    return f"<h1>Admin Results Error</h1><pre>{type(e).__name__}: {e}</pre>", 500


@app.route("/admin/results/export")
@_admin_required
def admin_results_export():
  try:
    import json as _jmod
    from db import _get_conn
    tier_filter = request.args.get("tier", "all")
    fmt = request.args.get("format", "csv")

    conn = _get_conn()
    cur = conn.cursor()
    if tier_filter in ("all",):
        cur.execute("SELECT result_json FROM research_cache WHERE expires_at > NOW()")
    else:
        cur.execute("SELECT result_json FROM research_cache WHERE status IN ('found', '3rdpty_found') AND expires_at > NOW()")
    raw_rows = cur.fetchall()
    cur.close()

    filtered = []
    for row in raw_rows:
        try:
            result = _jmod.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            continue
        if tier_filter == "all":
            filtered.append(result)
        elif tier_filter == "all_found":
            filtered.append(result)
        else:
            tier, price = classify_lead_tier(result)
            if tier == tier_filter:
                filtered.append(result)

    if fmt == "json":
        content = _jmod.dumps(filtered, indent=2, ensure_ascii=False)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=cache_export_{tier_filter}.json"},
        )

    # CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for r in filtered:
        writer.writerow({col: r.get(col, "") for col in CSV_COLUMNS})
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=cache_export_{tier_filter}.csv"},
    )
  except Exception as e:
    print(f"[RESULTS EXPORT] CRASH: {type(e).__name__}: {e}", flush=True)
    return f"<h1>Export Error</h1><pre>{type(e).__name__}: {e}</pre>", 500


@app.route("/admin/cleanup", methods=["POST"])
@_admin_required
def admin_cleanup():
    """Manually trigger DB cleanup to free disk space."""
    cache_deleted = cleanup_expired_cache()
    jobs_deleted = cleanup_old_job_results()
    return jsonify({"cache_deleted": cache_deleted, "jobs_deleted": jobs_deleted, "message": f"Freed space: {cache_deleted} expired cache + {jobs_deleted} old job results"})


@app.route("/admin/results/leads-export")
@_admin_required
def admin_leads_export():
    """Export filtered leads from research cache — lightweight direct SQL query."""
    try:
        import zipfile
        import json as _jmod
        from db import _get_conn

        placeholder_names = {"no contact found", "not found", "n/a", "unknown", "none", "no name found", "no contact", "no name", ""}
        placeholder_emails = {"no email found", "not found", "n/a", "unknown", "none", "no email", ""}
        min_date = datetime(2026, 4, 17)

        conn = _get_conn()
        cur = conn.cursor()
        # Only pull found results — much lighter than loading everything
        cur.execute("""
            SELECT result_json FROM research_cache
            WHERE status IN ('found', '3rdpty_found')
            AND expires_at > NOW()
        """)
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, r)) for r in rows]
        cur.close()

        leads_no_email = []
        leads_with_email = []

        for row in rows:
            try:
                result = _jmod.loads(row["result_json"]) if isinstance(row["result_json"], str) else row["result_json"]
            except Exception:
                continue

            name = (result.get("contact_name") or "").strip()
            if name.lower() in placeholder_names:
                continue

            date_str = (result.get("event_date") or "").strip()
            if not date_str:
                continue
            try:
                evt_date = datetime.strptime(date_str, "%m/%d/%Y")
            except ValueError:
                try:
                    evt_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
                except ValueError:
                    continue
            if evt_date < min_date:
                continue

            row_base = {col: result.get(col, "") for col in CSV_COLUMNS}
            leads_no_email.append(row_base)

            email = (result.get("contact_email") or "").strip()
            if email.lower() not in placeholder_emails and "@" in email:
                leads_with_email.append(row_base)

        # Build CSVs
        cols1 = CSV_COLUMNS
        buf1 = io.StringIO()
        w1 = csv.DictWriter(buf1, fieldnames=cols1, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        w1.writeheader()
        for r in leads_no_email:
            w1.writerow(r)

        cols2 = CSV_COLUMNS
        buf2 = io.StringIO()
        w2 = csv.DictWriter(buf2, fieldnames=cols2, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        w2.writeheader()
        for r in leads_with_email:
            w2.writerow(r)

        # Zip both files
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("leads_no_email.csv", buf1.getvalue())
            zf.writestr("leads_with_email.csv", buf2.getvalue())
        zip_buf.seek(0)

        print(f"[LEADS EXPORT] {len(leads_no_email)} contacts (no email), {len(leads_with_email)} with email", flush=True)
        return Response(
            zip_buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": "attachment; filename=leads_export.zip"},
        )
    except Exception as e:
        print(f"[LEADS EXPORT] CRASH: {type(e).__name__}: {e}", flush=True)
        import traceback; traceback.print_exc()
        return f"<h1>Leads Export Error</h1><pre>{type(e).__name__}: {e}</pre>", 500


@app.route("/admin/results/domains")
@_admin_required
def admin_export_cached_domains():
    """Download a plain text list of all domains in the research cache."""
    try:
        from db import _get_conn
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT cache_key FROM research_cache WHERE expires_at > NOW()")
        rows = cur.fetchall()
        cur.close()
        domains = sorted(set(r[0].strip() for r in rows if r[0] and r[0].strip()))
        content = "\n".join(domains)
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=searched_domains.txt"},
        )
    except Exception as e:
        print(f"[DOMAINS EXPORT] CRASH: {type(e).__name__}: {e}", flush=True)
        return f"<h1>Domains Export Error</h1><pre>{type(e).__name__}: {e}</pre>", 500


@app.route("/admin/rebuild-job/<job_id>", methods=["POST"])
@_admin_required
def admin_rebuild_job(job_id):
    """Rebuild a crashed job from saved individual results in job_results table."""
    results = get_completed_results(job_id)
    if not results:
        return jsonify({"error": f"No saved results found for {job_id}"}), 404

    print(f"[REBUILD] {job_id}: found {len(results)} saved results, rebuilding...", flush=True)

    # Classify and count
    found_count = sum(1 for r in results if r.get("status") in ("found", "3rdpty_found"))
    billable_count = 0
    for r in results:
        tier, price = classify_lead_tier(r)
        if tier != "not_billable":
            billable_count += 1

    # Generate export files (only billable leads)
    save_results = []
    for r in results:
        tier, price = classify_lead_tier(r)
        if tier != "not_billable":
            save_results.append(r)

    # CSV
    csv_file = os.path.join(RESULTS_DIR, f"{job_id}.csv")
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in save_results:
            writer.writerow({col: r.get(col, "") for col in CSV_COLUMNS})

    # JSON
    json_file = os.path.join(RESULTS_DIR, f"{job_id}.json")
    output = {
        "meta": {"total_nonprofits": len(results), "model": "Auctionintel.app", "rebuilt": True},
        "summary": {
            "found": found_count,
            "not_found": sum(1 for r in results if r.get("status") == "not_found"),
        },
        "results": save_results,
    }
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # XLSX
    xlsx_file = os.path.join(RESULTS_DIR, f"{job_id}.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Auction Results"
    ws.append(CSV_COLUMNS)
    for r in save_results:
        ws.append([r.get(col, "") for col in CSV_COLUMNS])
    wb.save(xlsx_file)

    # Save to DB
    try:
        with open(csv_file, "rb") as f:
            save_result_file(job_id, "csv", f.read())
        with open(json_file, "rb") as f:
            save_result_file(job_id, "json", f.read())
        with open(xlsx_file, "rb") as f:
            save_result_file(job_id, "xlsx", f.read())
    except Exception as e:
        print(f"[REBUILD] Failed to save files to DB: {e}", flush=True)

    # Update job status
    complete_search_job(job_id, found_count=found_count, billable_count=billable_count, total_cost_cents=0)
    print(f"[REBUILD] {job_id}: complete — {found_count} found, {billable_count} billable, {len(save_results)} exported", flush=True)

    return jsonify({
        "status": "rebuilt",
        "job_id": job_id,
        "total_results": len(results),
        "found": found_count,
        "billable": billable_count,
        "exported": len(save_results),
    })


@app.route("/admin/tickets")
@_admin_required
def admin_tickets_page():
    tickets = get_all_tickets()
    ticket_rows = ""
    for t in tickets:
        status = t["status"]
        sc = {"open": "#4ade80", "pending": "#eab308", "urgent": "#f87171", "resolved": "#a3a3a3"}.get(status, "#a3a3a3")
        unread = t.get("unread", 0)
        unread_badge = f' <span class="badge" style="background:#7f1d1d;color:#fca5a5;border:none;margin-left:4px;">{unread} new</span>' if unread > 0 else ""
        ticket_rows += (
            f'<tr>'
            f'<td><a href="/admin/tickets/{t["id"]}" class="user-link">#{t["id"]}</a></td>'
            f'<td><a href="/admin/tickets/{t["id"]}" style="color:#f5f5f5;text-decoration:none;">{html_escape(t["subject"])}{unread_badge}</a></td>'
            f'<td>{html_escape(t.get("user_email", ""))}</td>'
            f'<td style="color:{sc};font-weight:600;">{status.upper()}</td>'
            f'<td>{t.get("updated_at", "")}</td>'
            f'</tr>'
        )
    open_count = sum(1 for t in tickets if t["status"] in ("open", "urgent"))
    html = ADMIN_TICKETS_HTML
    html = _inject_sidebar(html, "admin-tickets")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    html = html.replace("{{TICKET_ROWS}}", ticket_rows or '<tr><td colspan="5" style="text-align:center;color:#525252;">No tickets</td></tr>')
    html = html.replace("{{TOTAL_TICKETS}}", str(len(tickets)))
    html = html.replace("{{OPEN_COUNT}}", str(open_count))
    return html


@app.route("/admin/tickets/<int:ticket_id>", methods=["GET", "POST"])
@_admin_required
def admin_ticket_detail(ticket_id):
    ticket = get_ticket(ticket_id)
    if not ticket:
        return redirect("/admin/tickets")

    if request.method == "POST":
        reply = request.form.get("message", "").strip()
        new_status = request.form.get("status", "").strip()
        if reply:
            add_ticket_message(ticket_id, session["user_id"], reply, is_admin=True)
            try:
                emails.send_ticket_reply_to_user(ticket["user_email"], ticket_id, ticket["subject"], reply)
            except Exception as e:
                print(f"[ADMIN TICKET] Email send error: {e}", flush=True)
        if new_status and new_status != ticket["status"]:
            update_ticket_status(ticket_id, new_status)
        return redirect(f"/admin/tickets/{ticket_id}")

    mark_messages_read_by_admin(ticket_id)
    messages = get_ticket_messages(ticket_id)
    msg_html = ""
    for m in messages:
        is_sender_admin = m["is_admin"]
        align = "right" if is_sender_admin else "left"
        bg = "#1a1500" if is_sender_admin else "#0a0a0a"
        border_color = "#eab308" if is_sender_admin else "#262626"
        label = "Admin" if is_sender_admin else html_escape(m["sender_email"])
        msg_html += (
            f'<div style="display:flex;justify-content:flex-{align};margin-bottom:12px;">'
            f'<div style="max-width:70%;background:{bg};border:1px solid {border_color};border-radius:12px;padding:12px 16px;">'
            f'<div style="font-size:11px;color:#737373;margin-bottom:4px;">{label} &middot; {m["created_at"]}</div>'
            f'<div style="font-size:13px;color:#d4d4d4;white-space:pre-wrap;">{html_escape(m["message"])}</div>'
            f'</div></div>'
        )

    status = ticket["status"]
    sc = {"open": "#4ade80", "pending": "#eab308", "urgent": "#f87171", "resolved": "#a3a3a3"}.get(status, "#a3a3a3")
    status_options = ""
    for s in ["open", "pending", "urgent", "resolved"]:
        sel = ' selected' if s == status else ''
        status_options += f'<option value="{s}"{sel}>{s.upper()}</option>'

    html = ADMIN_TICKET_DETAIL_HTML
    html = _inject_sidebar(html, "admin-tickets")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    html = html.replace("{{TICKET_ID}}", str(ticket_id))
    html = html.replace("{{SUBJECT}}", html_escape(ticket["subject"]))
    html = html.replace("{{USER_EMAIL}}", html_escape(ticket.get("user_email", "")))
    html = html.replace("{{STATUS}}", status.upper())
    html = html.replace("{{STATUS_COLOR}}", sc)
    html = html.replace("{{STATUS_OPTIONS}}", status_options)
    html = html.replace("{{MESSAGES}}", msg_html)
    html = html.replace("{{CREATED_AT}}", ticket.get("created_at", ""))
    return html


# ─── Admin HTML Templates ────────────────────────────────────────────────────

_ADMIN_STYLE = """
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'SF Mono','Consolas',monospace; background:#121212; color:#f5f5f5; min-height:100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width:1200px; margin:0 auto; padding:24px; }
  h1 { font-size:22px; font-weight:700; margin-bottom:20px; color:#f5f5f5; }
  h2 { font-size:16px; font-weight:600; margin-bottom:12px; color:#d4d4d4; }
  .section-title { font-size:12px; color:#eab308; text-transform:uppercase; letter-spacing:1px; font-weight:600; margin-bottom:12px; }
  .kpi-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; margin-bottom:24px; }
  .kpi-card { background:#0a0a0a; border:1px solid #1a1a1a; border-radius:8px; padding:16px; }
  .kpi-card .label { font-size:11px; color:#737373; text-transform:uppercase; margin-bottom:4px; }
  .kpi-card .value { font-size:24px; font-weight:700; color:#f5f5f5; }
  .kpi-card .value.gold { color:#eab308; }
  .kpi-card .value.green { color:#4ade80; }
  .kpi-card .value.red { color:#f87171; }
  .panel { background:#0a0a0a; border:1px solid #1a1a1a; border-radius:8px; padding:20px; margin-bottom:20px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { color:#737373; text-transform:uppercase; font-size:10px; padding:8px 6px; text-align:left; border-bottom:1px solid #262626; }
  td { padding:8px 6px; border-bottom:1px solid #1a1a1a; color:#d4d4d4; }
  tr:hover { background:#141414; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; }
  .badge-trial { background:#1a1500; color:#eab308; border:1px solid #eab308; }
  .badge-banned { background:#1a0000; color:#f87171; border:1px solid #f87171; }
  .badge-verified { background:#001a00; color:#4ade80; border:1px solid #4ade80; }
  .user-link { color:#eab308; text-decoration:none; }
  .user-link:hover { text-decoration:underline; }
  .search-input { width:100%; padding:10px 14px; background:#000; border:1px solid #333; border-radius:8px; color:#f5f5f5; font-size:13px; font-family:inherit; outline:none; margin-bottom:16px; }
  .search-input:focus { border-color:#eab308; }
  .stat-cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(160px, 1fr)); gap:12px; margin-bottom:20px; }
  .stat-card { background:#111; border:1px solid #262626; border-radius:8px; padding:14px; text-align:center; }
  .stat-card .val { font-size:20px; font-weight:700; color:#eab308; }
  .stat-card .lbl { font-size:10px; color:#737373; text-transform:uppercase; margin-top:4px; }
  .btn { display:inline-block; padding:8px 16px; border:none; border-radius:6px; font-size:12px; font-weight:600; cursor:pointer; font-family:inherit; text-decoration:none; }
  .btn-danger { background:#7f1d1d; color:#fca5a5; }
  .btn-danger:hover { background:#991b1b; }
  .btn-success { background:#14532d; color:#86efac; }
  .btn-success:hover { background:#166534; }
  .btn-gold { background:#eab308; color:#000; }
  .btn-gold:hover { background:#ca8a04; }
  .tabs { display:flex; gap:0; border-bottom:1px solid #262626; margin-bottom:16px; }
  .tab { padding:10px 20px; cursor:pointer; color:#737373; font-size:12px; font-weight:600; border-bottom:2px solid transparent; }
  .tab:hover { color:#d4d4d4; }
  .tab.active { color:#eab308; border-bottom-color:#eab308; }
  .tab-content { display:none; }
  .tab-content.active { display:block; }
  .action-bar { display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap; }
  .action-bar input { padding:8px 12px; background:#000; border:1px solid #333; border-radius:6px; color:#f5f5f5; font-size:13px; font-family:inherit; outline:none; width:120px; }
  .action-bar input:focus { border-color:#eab308; }
"""

ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Dashboard — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>Admin Dashboard</h1>

  <div class="section-title">Revenue</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Total Revenue</div><div class="value green">{{TOTAL_REVENUE}}</div></div>
    <div class="kpi-card"><div class="label">Today</div><div class="value green">{{REVENUE_TODAY}}</div></div>
    <div class="kpi-card"><div class="label">This Week</div><div class="value green">{{REVENUE_WEEK}}</div></div>
    <div class="kpi-card"><div class="label">Net (Topups - Refunds)</div><div class="value gold">{{NET_REVENUE}}</div></div>
  </div>

  <div class="section-title">Users</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Total Users</div><div class="value">{{TOTAL_USERS}}</div></div>
    <div class="kpi-card"><div class="label">Trial</div><div class="value gold">{{TRIAL_USERS}}</div></div>
    <div class="kpi-card"><div class="label">Verified</div><div class="value green">{{VERIFIED_USERS}}</div></div>
    <div class="kpi-card"><div class="label">Signups This Week</div><div class="value">{{SIGNUPS_WEEK}}</div></div>
  </div>

  <div class="section-title">Operations</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Total Jobs</div><div class="value">{{TOTAL_JOBS}}</div></div>
    <div class="kpi-card"><div class="label">Running Now</div><div class="value gold">{{RUNNING_JOBS}}</div></div>
    <div class="kpi-card"><div class="label">Leads Found</div><div class="value">{{TOTAL_LEADS}}</div></div>
    <div class="kpi-card"><div class="label">Billable Leads</div><div class="value green">{{BILLABLE_LEADS}}</div></div>
  </div>

  <div class="section-title">Health</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Open Tickets</div><div class="value red">{{OPEN_TICKETS}}</div></div>
    <div class="kpi-card"><div class="label">Cache Entries</div><div class="value">{{CACHE_ENTRIES}}</div></div>
    <div class="kpi-card"><div class="label">Exclusive Leads Sold</div><div class="value gold">{{EXCLUSIVE_SOLD}}</div></div>
    <div class="kpi-card"><div class="label">Banned Users</div><div class="value red">{{BANNED_USERS}}</div></div>
  </div>
</div>
</div>
</body></html>"""


ADMIN_USERS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>User Management — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>User Management <span style="color:#737373;font-size:14px;font-weight:400;">({{USER_COUNT}} users)</span></h1>
  <input type="text" class="search-input" id="userSearch" placeholder="Search by email or company..." oninput="filterUsers()">
  <div class="panel" style="overflow-x:auto;">
    <table id="userTable">
      <thead>
        <tr><th>Email</th><th>Company</th><th>Balance</th><th>Spent</th><th>Jobs</th><th>Status</th><th>Last Login</th><th>Joined</th></tr>
      </thead>
      <tbody>{{USER_ROWS}}</tbody>
    </table>
  </div>
</div>
</div>
<script>
function filterUsers(){
  var q = document.getElementById('userSearch').value.toLowerCase();
  var rows = document.querySelectorAll('.user-row');
  rows.forEach(function(r){
    r.style.display = r.getAttribute('data-search').toLowerCase().includes(q) ? '' : 'none';
  });
}
</script>
</body></html>"""


ADMIN_USER_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>User Detail — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <a href="/admin/users" style="color:#737373;font-size:12px;text-decoration:none;">&larr; Back to Users</a>
  <h1 style="margin-top:8px;">{{USER_EMAIL}} {{USER_BADGES}}</h1>

  <div class="stat-cards">
    <div class="stat-card"><div class="val">{{USER_BALANCE}}</div><div class="lbl">Balance</div></div>
    <div class="stat-card"><div class="val">{{USER_TOTAL_SPENT}}</div><div class="lbl">Total Spent</div></div>
    <div class="stat-card"><div class="val">{{USER_TOTAL_TOPUPS}}</div><div class="lbl">Total Topups</div></div>
    <div class="stat-card"><div class="val">{{USER_JOB_COUNT}}</div><div class="lbl">Jobs</div></div>
  </div>

  <div class="panel" style="margin-bottom:20px;">
    <table style="font-size:13px;">
      <tr><td style="color:#737373;width:120px;">Phone</td><td>{{USER_PHONE}}</td></tr>
      <tr><td style="color:#737373;">Company</td><td>{{USER_COMPANY}}</td></tr>
      <tr><td style="color:#737373;">Joined</td><td>{{USER_CREATED}}</td></tr>
      <tr><td style="color:#737373;">Last Login</td><td>{{USER_LAST_LOGIN}}</td></tr>
    </table>
  </div>

  <div class="action-bar">
    <form method="POST" action="/admin/users/{{USER_ID}}/ban" style="display:inline;">
      <input type="hidden" name="action" value="{{BAN_ACTION}}">
      <button type="submit" class="btn {{BAN_CLASS}}">{{BAN_LABEL}}</button>
    </form>
    <form method="POST" action="/admin/users/{{USER_ID}}/wallet" style="display:inline-flex;gap:8px;align-items:center;">
      <input type="number" name="amount" step="0.01" placeholder="$ amount" style="width:100px;">
      <input type="text" name="reason" placeholder="Reason" style="width:200px;">
      <button type="submit" class="btn btn-gold">Adjust Wallet</button>
    </form>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('jobs')">Jobs</div>
    <div class="tab" onclick="switchTab('txns')">Transactions</div>
    <div class="tab" onclick="switchTab('leads')">Exclusive Leads</div>
    <div class="tab" onclick="switchTab('tickets')">Tickets</div>
  </div>

  <div class="tab-content active" id="tab-jobs">
    <div class="panel" style="overflow-x:auto;">
      <table><thead><tr><th>Job ID</th><th>Status</th><th>Searched</th><th>Found</th><th>Billable</th><th>Cost</th><th>Started</th></tr></thead>
      <tbody>{{JOB_ROWS}}</tbody></table>
    </div>
  </div>
  <div class="tab-content" id="tab-txns">
    <div class="panel" style="overflow-x:auto;">
      <table><thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Description</th></tr></thead>
      <tbody>{{TXN_ROWS}}</tbody></table>
    </div>
  </div>
  <div class="tab-content" id="tab-leads">
    <div class="panel" style="overflow-x:auto;">
      <table><thead><tr><th>Nonprofit</th><th>Event</th><th>URL</th></tr></thead>
      <tbody>{{LEAD_ROWS}}</tbody></table>
    </div>
  </div>
  <div class="tab-content" id="tab-tickets">
    <div class="panel" style="overflow-x:auto;">
      <table><thead><tr><th>ID</th><th>Subject</th><th>Status</th><th>Created</th></tr></thead>
      <tbody>{{TICKET_ROWS}}</tbody></table>
    </div>
  </div>
</div>
</div>
<script>
function switchTab(name){
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
  document.querySelectorAll('.tab-content').forEach(function(c){ c.classList.remove('active'); });
  event.target.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
}
</script>
</body></html>"""


ADMIN_REVENUE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Revenue — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>Revenue (Last 30 Days)</h1>

  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Topups</div><div class="value green">{{TOTAL_TOPUPS_30D}}</div></div>
    <div class="kpi-card"><div class="label">Research Fees</div><div class="value">{{TOTAL_RESEARCH_30D}}</div></div>
    <div class="kpi-card"><div class="label">Lead Fees</div><div class="value">{{TOTAL_LEADS_30D}}</div></div>
    <div class="kpi-card"><div class="label">Exclusive Fees</div><div class="value gold">{{TOTAL_EXCLUSIVE_30D}}</div></div>
    <div class="kpi-card"><div class="label">Refunds</div><div class="value red">{{TOTAL_REFUNDS_30D}}</div></div>
  </div>

  <div class="section-title">Daily Breakdown</div>
  <div class="panel" style="overflow-x:auto;">
    <table>
      <thead><tr><th>Date</th><th>Topups</th><th>Research</th><th>Leads</th><th>Exclusive</th><th>Refunds</th><th>Net</th></tr></thead>
      <tbody>{{DAILY_ROWS}}</tbody>
    </table>
  </div>

  <div class="section-title">Top 10 Spenders</div>
  <div class="panel" style="overflow-x:auto;">
    <table>
      <thead><tr><th>Email</th><th>Company</th><th>Total Spent</th><th>Topups</th><th>Jobs</th></tr></thead>
      <tbody>{{SPENDER_ROWS}}</tbody>
    </table>
  </div>
</div>
</div>
</body></html>"""


ADMIN_ACTIVITY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Activity — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>Activity</h1>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('jobs')">Recent Jobs</div>
    <div class="tab" onclick="switchTab('logins')">Recent Logins</div>
  </div>

  <div class="tab-content active" id="tab-jobs">
    <div class="panel" style="overflow-x:auto;">
      <table>
        <thead><tr><th>Job ID</th><th>User</th><th>Status</th><th>Searched</th><th>Found</th><th>Billable</th><th>Cost</th><th>Started</th><th>Completed</th></tr></thead>
        <tbody>{{JOB_ROWS}}</tbody>
      </table>
    </div>
  </div>
  <div class="tab-content" id="tab-logins">
    <div class="panel" style="overflow-x:auto;">
      <table>
        <thead><tr><th>Email</th><th>Last Login</th><th>Status</th></tr></thead>
        <tbody>{{LOGIN_ROWS}}</tbody>
      </table>
    </div>
  </div>
</div>
</div>
<script>
function switchTab(name){
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
  document.querySelectorAll('.tab-content').forEach(function(c){ c.classList.remove('active'); });
  event.target.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
}
function rebuildJob(jobId){
  if(!confirm('Rebuild job '+jobId+'? This will recover saved results and generate export files.')) return;
  var btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Rebuilding...';
  fetch('/admin/rebuild-job/'+jobId, {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(data){
      if(data.error){ alert('Error: '+data.error); btn.disabled=false; btn.textContent='Rebuild'; return; }
      alert('Rebuilt! Found: '+data.found+', Billable: '+data.billable+', Exported: '+data.exported);
      location.reload();
    })
    .catch(function(e){ alert('Error: '+e); btn.disabled=false; btn.textContent='Rebuild'; });
}
</script>
</body></html>"""


ADMIN_SYSTEM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>System — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>System</h1>

  <div class="section-title">Server Info</div>
  <div class="panel" style="margin-bottom:20px;">
    <table style="font-size:13px;">
      <tr><td style="color:#737373;width:140px;">Python Version</td><td>{{PYTHON_VERSION}}</td></tr>
      <tr><td style="color:#737373;">Uptime</td><td>{{UPTIME}}</td></tr>
    </table>
  </div>

  <div class="section-title">Running Jobs ({{RUNNING_COUNT}})</div>
  <div class="panel" style="overflow-x:auto;margin-bottom:20px;">
    <table>
      <thead><tr><th>Job ID</th><th>Progress</th><th>Found</th></tr></thead>
      <tbody>{{RUNNING_ROWS}}</tbody>
    </table>
  </div>

  <div class="section-title">Research Cache ({{CACHE_TOTAL}} entries)</div>
  <div class="panel" style="overflow-x:auto;margin-bottom:20px;">
    <table>
      <thead><tr><th>Status</th><th>Count</th><th>Oldest</th><th>Newest</th></tr></thead>
      <tbody>{{CACHE_ROWS}}</tbody>
    </table>
  </div>

  <div class="section-title">Drip Campaign Stats</div>
  <div class="panel" style="overflow-x:auto;">
    <table>
      <thead><tr><th>Drip Key</th><th>Sent</th><th>Last Sent</th></tr></thead>
      <tbody>{{DRIP_ROWS}}</tbody>
    </table>
  </div>
</div>
</div>
</body></html>"""

ADMIN_TICKETS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tickets — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>Support Tickets</h1>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Total Tickets</div><div class="value">{{TOTAL_TICKETS}}</div></div>
    <div class="kpi-card"><div class="label">Open / Urgent</div><div class="value red">{{OPEN_COUNT}}</div></div>
  </div>
  <div class="panel" style="overflow-x:auto;">
    <table>
      <thead><tr><th>ID</th><th>Subject</th><th>User</th><th>Status</th><th>Updated</th></tr></thead>
      <tbody>{{TICKET_ROWS}}</tbody>
    </table>
  </div>
</div>
</div>
</body></html>"""

ADMIN_RESULTS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cache Results — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """
  .export-bar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:20px; }
  .export-bar a { display:inline-block; padding:8px 16px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none; font-family:inherit; }
  .btn-outline { background:transparent; border:1px solid #333; color:#d4d4d4; }
  .btn-outline:hover { border-color:#eab308; color:#eab308; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>Cache Results</h1>

  <div class="section-title">Overview</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Total Cached</div><div class="value">{{TOTAL_CACHED}}</div></div>
    <div class="kpi-card"><div class="label">Found</div><div class="value green">{{FOUND_COUNT}}</div></div>
    <div class="kpi-card"><div class="label">Not Found</div><div class="value">{{NOT_FOUND_COUNT}}</div></div>
    <div class="kpi-card"><div class="label">Error / Uncertain</div><div class="value red">{{ERROR_COUNT}}</div></div>
  </div>

  <div class="section-title">Tier Breakdown (Found Only)</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Decision Maker</div><div class="value gold">{{DM_COUNT}}</div></div>
    <div class="kpi-card"><div class="label">Outreach Ready</div><div class="value green">{{OR_COUNT}}</div></div>
    <div class="kpi-card"><div class="label">Event Verified</div><div class="value">{{EV_COUNT}}</div></div>
  </div>

  <div class="section-title">Expiry Breakdown</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="label">Already Expired</div><div class="value red">{{EXPIRY_EXPIRED}}</div></div>
    <div class="kpi-card"><div class="label">&lt; 7 Days</div><div class="value gold">{{EXPIRY_7D}}</div></div>
    <div class="kpi-card"><div class="label">&lt; 30 Days</div><div class="value">{{EXPIRY_30D}}</div></div>
    <div class="kpi-card"><div class="label">30+ Days</div><div class="value green">{{EXPIRY_GT30D}}</div></div>
  </div>

  <div class="section-title">Export by Tier</div>
  <div class="export-bar">
    <a href="/admin/results/export?tier=decision_maker&format=csv" class="btn btn-gold">Decision Maker CSV</a>
    <a href="/admin/results/export?tier=outreach_ready&format=csv" class="btn btn-success">Outreach Ready CSV</a>
    <a href="/admin/results/export?tier=event_verified&format=csv" class="btn-outline">Event Verified CSV</a>
    <a href="/admin/results/export?tier=all_found&format=csv" class="btn btn-gold" style="background:#7c3aed;color:#fff;">All Found CSV</a>
    <a href="/admin/results/export?tier=all&format=csv" class="btn-outline">Everything CSV</a>
  </div>
  <div class="export-bar">
    <a href="/admin/results/export?tier=all_found&format=json" class="btn-outline">All Found JSON</a>
    <a href="/admin/results/export?tier=all&format=json" class="btn-outline">Everything JSON</a>
  </div>

  <div class="section-title">Status Breakdown</div>
  <div class="panel" style="overflow-x:auto;">
    <table>
      <thead><tr><th>Status</th><th>Count</th></tr></thead>
      <tbody>{{STATUS_ROWS}}</tbody>
    </table>
  </div>

</div>
</div>
</body></html>"""


ADMIN_BATCH_RUNNER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Batch Runner — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """
  .btn-gold { display:inline-block; padding:8px 16px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none; background:#eab308; color:#000; }
  .btn-gold:hover { background:#ffd900; }
  .btn-outline { display:inline-block; padding:8px 16px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none; background:transparent; border:1px solid #333; color:#d4d4d4; }
  .btn-outline:hover { border-color:#eab308; color:#eab308; }
  .export-bar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:20px; }
  .job-card { background:#0a0a0a; border:1px solid #262626; border-radius:8px; padding:16px; margin-bottom:12px; }
  .job-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
  .job-id { font-family:monospace; font-size:13px; color:#eab308; }
  .job-status { font-size:11px; padding:4px 8px; border-radius:4px; text-transform:uppercase; font-weight:600; }
  .status-complete { background:#22c55e; color:#000; }
  .status-running { background:#3b82f6; color:#fff; }
  .job-stats { display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr)); gap:12px; margin-top:12px; }
  .stat { font-size:12px; color:#a3a3a3; }
  .stat-value { font-size:16px; font-weight:600; color:#f5f5f5; }
  .stat .green { color:#22c55e; }
  .stat .gold { color:#eab308; }
  .job-actions { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
  .job-actions a, .job-actions button { font-size:11px; padding:6px 12px; border:none; cursor:pointer; }
  .refresh-note { font-size:11px; color:#737373; margin-top:8px; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h1>Batch Runner</h1>

  <div class="export-bar">
    <a href="/admin/results/domains" class="btn-gold">Export All Searched Domains</a>
    <button onclick="location.reload()" class="btn-outline">Refresh Jobs</button>
  </div>

  <div class="section-title">Recent Jobs</div>
  <div id="jobsList">
    {{JOBS_HTML}}
  </div>

  <p class="refresh-note">Jobs auto-refresh every 10 seconds while running. Click "Refresh Jobs" for manual update.</p>
</div>
</div>

<script>
  let autoRefresh = setInterval(() => {
    const runningJobs = document.querySelectorAll('.status-running');
    if (runningJobs.length > 0) {
      location.reload();
    } else {
      clearInterval(autoRefresh);
    }
  }, 10000);
</script>
</body></html>"""


ADMIN_TICKET_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ticket #{{TICKET_ID}} — Auction Finder</title>
<style>""" + _ADMIN_STYLE + """
  .msg-area { width:100%; min-height:100px; padding:12px; background:#000; border:1px solid #333; border-radius:8px; color:#f5f5f5; font-size:13px; font-family:inherit; outline:none; resize:vertical; }
  .msg-area:focus { border-color:#eab308; }
</style>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <a href="/admin/tickets" style="color:#737373;font-size:12px;text-decoration:none;">&larr; Back to Tickets</a>
  <div style="display:flex;align-items:center;gap:12px;margin:12px 0 20px;">
    <h1 style="margin:0;">Ticket #{{TICKET_ID}}</h1>
    <span class="badge" style="background:{{STATUS_COLOR}}22;color:{{STATUS_COLOR}};border:1px solid {{STATUS_COLOR}};font-size:12px;">{{STATUS}}</span>
  </div>
  <div class="panel" style="margin-bottom:16px;">
    <table style="font-size:13px;">
      <tr><td style="color:#737373;width:100px;">Subject</td><td>{{SUBJECT}}</td></tr>
      <tr><td style="color:#737373;">User</td><td>{{USER_EMAIL}}</td></tr>
      <tr><td style="color:#737373;">Created</td><td>{{CREATED_AT}}</td></tr>
    </table>
  </div>
  <div class="section-title">Messages</div>
  <div class="panel" style="max-height:50vh;overflow-y:auto;margin-bottom:16px;">
    {{MESSAGES}}
  </div>
  <div class="panel">
    <form method="POST" action="/admin/tickets/{{TICKET_ID}}">
      <div class="section-title" style="margin-bottom:8px;">Reply</div>
      <textarea name="message" class="msg-area" placeholder="Type your reply..."></textarea>
      <div style="display:flex;gap:12px;align-items:center;margin-top:12px;">
        <select name="status" style="padding:8px 12px;background:#000;border:1px solid #333;border-radius:6px;color:#f5f5f5;font-size:12px;font-family:inherit;">{{STATUS_OPTIONS}}</select>
        <button type="submit" class="btn btn-gold">Send Reply &amp; Update</button>
      </div>
    </form>
  </div>
</div>
</div>
</body></html>"""


# ─── HTML Templates ──────────────────────────────────────────────────────────

_BASE_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; scrollbar-width:thin; scrollbar-color:#eab308 #0a0a0a; }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-track { background:#0a0a0a; }
  ::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }
  ::-webkit-scrollbar-thumb:hover { background:#ffd900; }
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
<title>Auction Finder - Register</title>
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
  <img src="/static/logo_dark.png" alt="Auction Finder" style="height:48px;margin-bottom:16px;">
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

  // Client-side validation — prevent form reset on password mismatch
  var form = document.querySelector('form');
  form.addEventListener('submit', function(e) {{
    var pw = form.querySelector('input[name="password"]').value;
    var confirm = form.querySelector('input[name="confirm"]').value;
    var existing = form.querySelector('.error');
    if (existing) existing.remove();

    if (pw !== confirm) {{
      e.preventDefault();
      var err = document.createElement('p');
      err.className = 'error';
      err.textContent = 'Passwords do not match';
      var marker = form.querySelector('.sub');
      marker.insertAdjacentElement('afterend', err);
      form.querySelector('input[name="confirm"]').value = '';
      form.querySelector('input[name="confirm"]').focus();
      return;
    }}
    if (pw.length < 6) {{
      e.preventDefault();
      var err = document.createElement('p');
      err.className = 'error';
      err.textContent = 'Password must be at least 6 characters';
      var marker = form.querySelector('.sub');
      marker.insertAdjacentElement('afterend', err);
      form.querySelector('input[name="password"]').focus();
      return;
    }}
  }});
}})();
</script>
</body>
</html>"""

LOGIN_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auction Finder - Login</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/login">
  <img src="/static/logo_dark.png" alt="Auction Finder" style="height:48px;margin-bottom:16px;">
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
<title>Auction Finder - Forgot Password</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/forgot-password">
  <img src="/static/logo_dark.png" alt="Auction Finder" style="height:48px;margin-bottom:16px;">
  <p class="sub">Reset your password</p>
  <!-- message -->
  <input type="email" name="email" placeholder="Email address" autofocus required>
  <button type="submit">Send Reset Link</button>
  <p class="link"><a href="/login">Back to login</a></p>
</form>
</body>
</html>"""

VERIFY_EMAIL_PENDING_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auction Finder - Verify Email</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<div class="auth-box">
  <img src="/static/logo_dark.png" alt="Auction Finder" style="height:48px;margin-bottom:16px;">
  <p class="sub">Check your email</p>
  <!-- message -->
  <p style="color:#a3a3a3;font-size:14px;line-height:1.6;margin-bottom:20px;">
    We sent a verification link to your email address. Click the link to activate your account.
  </p>
  <p style="color:#737373;font-size:12px;margin-bottom:20px;">
    The link expires in 24 hours. Check your spam folder if you don't see it.
  </p>
  <form method="POST" action="/resend-verification" style="margin:0;">
    <button type="submit">Resend Verification Email</button>
  </form>
  <p class="link" style="margin-top:16px;"><a href="/logout">Sign out</a></p>
</div>
</body>
</html>"""

RESET_PASSWORD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auction Finder - Reset Password</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>{_BASE_STYLE}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
</style>
</head>
<body>
<form class="auth-box" method="POST" action="/reset-password">
  <img src="/static/logo_dark.png" alt="Auction Finder" style="height:48px;margin-bottom:16px;">
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
<title>Auction Finder - Wallet</title>
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
      <input type="number" id="topupAmount" placeholder="Amount in USD" min="10" max="9999" step="1" value="100">
    </div>
    <p class="topup-hint">Minimum $10, maximum $9,999 per top-up</p>
    <label class="topup-ack" style="display:flex;align-items:flex-start;gap:10px;margin:14px 0 16px;cursor:pointer;">
      <input type="checkbox" id="topupAck" style="accent-color:#eab308;margin-top:3px;min-width:16px;min-height:16px;">
      <span style="font-size:12px;color:#a3a3a3;line-height:1.5;">I understand wallet credits are non-refundable (except where required by law) and will be used for research and lead charges according to the app's <a href="/refund-policy" target="_blank" style="color:#eab308;">billing rules</a>.</span>
    </label>
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
  if (!document.getElementById('topupAck').checked) {
    showError('Please acknowledge the non-refundable wallet policy before continuing.');
    return false;
  }
  const amount = parseFloat(document.getElementById('topupAmount').value);
  if (!amount || amount < 10 || amount > 9999) {
    showError('Amount must be between $10 and $9,999');
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
<title>Auction Finder - Profile</title>
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
<title>Auction Finder - Billing</title>
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
      <div class="label">Locked Leads</div>
      <div class="amount gold">{{EXCLUSIVE_FEES}}</div>
    </div>
    <div class="summary-card">
      <div class="label">Tier Refunds</div>
      <div class="amount green">{{REFUNDS}}</div>
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
<title>Auction Finder</title>
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

  .search-dots { display: none; text-align: center; padding: 10px 0 4px; }
  .search-dots span { display: inline-block; width: 10px; height: 10px; margin: 0 4px; border-radius: 50%; background: #eab308; animation: dotBounce 1.4s ease-in-out infinite; }
  .search-dots span:nth-child(2) { animation-delay: 0.16s; }
  .search-dots span:nth-child(3) { animation-delay: 0.32s; }
  .search-dots span:nth-child(4) { animation-delay: 0.48s; }
  .search-dots span:nth-child(5) { animation-delay: 0.64s; }
  @keyframes dotBounce { 0%,80%,100% { transform: scale(0.4); opacity: 0.3; } 40% { transform: scale(1); opacity: 1; } }

  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat { background: #000000; border-radius: 8px; padding: 12px; text-align: center; }
  .stat .num { font-size: 24px; font-weight: 700; }
  .stat .label { font-size: 11px; color: #a3a3a3; text-transform: uppercase; margin-top: 4px; }
  .stat.found .num { color: #4ade80; }
  .stat.external .num { color: #eab308; }
  .stat.notfound .num { color: #f87171; }
  .stat.uncertain .num { color: #fbbf24; }

  .terminal { background: #000000; border: 1px solid #262626; border-radius: 8px; padding: 16px; max-height: 400px; overflow-y: auto; font-size: 13px; line-height: 1.6; scrollbar-width:thin; scrollbar-color:#eab308 #000; }
  .terminal::-webkit-scrollbar { width:8px; }
  .terminal::-webkit-scrollbar-track { background:#000; }
  .terminal::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }
  .terminal::-webkit-scrollbar-thumb:hover { background:#ffd900; }
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
  .download-section button.new-search-btn { background: #404040; color: #f5f5f5; }

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
    <div class="controls">
      <button class="btn-primary" id="searchBtn" onclick="startSearch()">Search for Auctions</button>
      <button class="btn-secondary" id="stopBtn" onclick="stopSearch()" style="display:none;background:#7f1d1d;color:#fca5a5;border-color:#991b1b;">Stop Search</button>
      <button class="btn-secondary" onclick="document.getElementById('input').value='';updateCount()">Clear</button>
      <span class="count-label" id="countLabel">0 nonprofits</span>
    </div>
  </div>

  <div class="progress-section" id="progressSection">
    <div class="progress-bar-container">
      <div class="progress-bar" id="progressBar"></div>
      <div class="progress-text" id="progressText">0 / 0</div>
    </div>
    <div class="search-dots" id="searchDots"><span></span><span></span><span></span><span></span><span></span></div>
    <div id="etaDisplay" style="display:none;text-align:center;padding:6px 0 2px;font-size:13px;color:#a3a3a3;font-family:monospace;">
      <span id="etaElapsed"></span> &nbsp;&bull;&nbsp; <span id="etaRemaining" style="color:#eab308;"></span>
    </div>

    <div class="stats">
      <div class="stat found"><div class="num" id="statFound">0</div><div class="label">Auctions Found</div></div>
      <div class="stat external"><div class="num" id="statExternal">0</div><div class="label">3rd Party</div></div>
      <div class="stat notfound"><div class="num" id="statNotFound">0</div><div class="label">No Auction Found</div></div>
      <div class="stat uncertain"><div class="num" id="statUncertain">0</div><div class="label">Uncertain</div></div>
    </div>
    <div style="text-align:center;margin:-8px 0 12px;">
      <button onclick="document.getElementById('resultsKeyModal').style.display='flex'" style="background:none;border:1px solid #333;color:#a3a3a3;font-size:12px;padding:4px 12px;border-radius:4px;cursor:pointer;font-family:inherit;">What do these results mean?</button>
    </div>
    <div id="resultsKeyModal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.7);align-items:center;justify-content:center;" onclick="if(event.target===this)this.style.display='none'">
      <div style="background:#141414;border:1px solid #333;border-radius:12px;max-width:520px;width:90%;padding:28px;position:relative;max-height:85vh;overflow-y:auto;">
        <button onclick="document.getElementById('resultsKeyModal').style.display='none'" style="position:absolute;top:12px;right:16px;background:none;border:none;color:#737373;font-size:20px;cursor:pointer;">&times;</button>
        <h3 style="margin:0 0 16px;font-size:18px;font-weight:700;color:#f5f5f5;">Results Key</h3>
        <div style="font-size:13px;line-height:1.8;color:#d4d4d4;">
          <p style="margin:0 0 12px;"><span style="color:#4ade80;font-weight:700;">AUCTION FOUND</span> &mdash; A verified auction/fundraising event was discovered for this nonprofit.</p>
          <p style="margin:0 0 12px;"><span style="color:#60a5fa;font-weight:700;">3RD PARTY</span> &mdash; Event found via a third-party listing site (not the nonprofit's own page).</p>
          <p style="margin:0 0 12px;"><span style="color:#f87171;font-weight:700;">NO AUCTION FOUND</span> &mdash; No current auction or fundraising event was found for this nonprofit.</p>
          <p style="margin:0 0 16px;"><span style="color:#fbbf24;font-weight:700;">UNCERTAIN</span> &mdash; Possible event detected but not enough evidence to confirm.</p>
          <hr style="border:none;border-top:1px solid #333;margin:16px 0;">
          <h4 style="margin:0 0 10px;font-size:14px;font-weight:700;color:#f5f5f5;">Lead Tiers (only charged when auction is found)</h4>
          <p style="margin:0 0 8px;"><span style="color:#4ade80;font-weight:700;">Decision Maker — $1.75</span><br>Event details + contact name + verified email. The most complete lead.</p>
          <p style="margin:0 0 8px;"><span style="color:#60a5fa;font-weight:700;">Outreach Ready — $1.25</span><br>Event details + verified email, but no contact name found.</p>
          <p style="margin:0 0 8px;"><span style="color:#facc15;font-weight:700;">Event Verified — $0.75</span><br>Event title, date &amp; URL confirmed, but no email found.</p>
          <p style="margin:0 0 8px;"><span style="color:#737373;font-weight:700;">Not Billable — $0.00</span><br>Auction found but missing required evidence (no event URL or title). No charge.</p>
          <hr style="border:none;border-top:1px solid #333;margin:16px 0;">
          <p style="margin:0;color:#a3a3a3;font-size:12px;">You are only charged lead fees for tiers you selected before the search. Research fee ($0.04/nonprofit) is charged for every nonprofit searched. If an auction is found but lands in a tier you didn't select, it shows in your results at no lead fee charge.</p>
        </div>
      </div>
    </div>

    <div class="terminal" id="terminal"></div>

    <div class="billing-summary" id="billingSummary"></div>

    <div class="download-section" id="downloadSection">
      <div class="dl-row">
        <a href="#" id="downloadCsv">Download CSV</a>
        <a href="#" id="downloadJson" class="json-btn">Download JSON</a>
        <a href="#" id="downloadXlsx" class="xlsx-btn">Download XLSX</a>
        <button class="view-btn" onclick="toggleJsonViewer()">View Results (JSON)</button>
        <button class="new-search-btn" onclick="newSearch()">New Search</button>
      </div>
      <div class="json-viewer" id="jsonViewer"><pre id="jsonContent"></pre></div>
    </div>
  </div>
<!-- Tier Selection Modal -->
<div id="tierModal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);align-items:center;justify-content:center;">
  <div style="background:#0a0a0a;border:1px solid #262626;border-radius:12px;max-width:480px;width:90%;padding:28px 24px 20px;box-shadow:0 25px 50px rgba(0,0,0,0.5);">
    <h3 style="margin:0 0 4px;color:#f5f5f5;font-size:18px;">Choose Your Lead Tiers</h3>
    <p style="margin:0 0 16px;font-size:13px;color:#737373;" id="tierResearchFee">Research fee: $0.04 per nonprofit (charged for every nonprofit searched)</p>

    <label style="display:block;cursor:pointer;padding:14px;border:1px solid #262626;border-radius:8px;margin-bottom:10px;transition:border-color 0.15s;" id="tierLabel_decision_maker" onmouseenter="this.style.borderColor='#404040'" onmouseleave="this.style.borderColor=document.getElementById('tier_decision_maker').checked?'#eab308':'#262626'">
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="checkbox" id="tier_decision_maker" checked style="accent-color:#eab308;width:16px;height:16px;" onchange="tierCheckChanged()">
        <span style="font-weight:700;color:#4ade80;font-size:15px;">Decision Maker</span>
        <span style="margin-left:auto;color:#d4d4d4;font-weight:600;">$1.75</span>
      </div>
      <div style="margin:6px 0 0 26px;font-size:12.5px;color:#a3a3a3;">Charged only if a named contact and email are found</div>
    </label>

    <label style="display:block;cursor:pointer;padding:14px;border:1px solid #262626;border-radius:8px;margin-bottom:10px;transition:border-color 0.15s;" id="tierLabel_outreach_ready" onmouseenter="this.style.borderColor='#404040'" onmouseleave="this.style.borderColor=document.getElementById('tier_outreach_ready').checked?'#eab308':'#262626'">
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="checkbox" id="tier_outreach_ready" style="accent-color:#eab308;width:16px;height:16px;" onchange="tierCheckChanged()">
        <span style="font-weight:700;color:#60a5fa;font-size:15px;">Outreach Ready</span>
        <span style="margin-left:auto;color:#d4d4d4;font-weight:600;">$1.25</span>
      </div>
      <div style="margin:6px 0 0 26px;font-size:12.5px;color:#a3a3a3;">Charged only if a verified email is found</div>
    </label>

    <label style="display:block;cursor:pointer;padding:14px;border:1px solid #262626;border-radius:8px;margin-bottom:10px;transition:border-color 0.15s;" id="tierLabel_event_verified" onmouseenter="this.style.borderColor='#404040'" onmouseleave="this.style.borderColor=document.getElementById('tier_event_verified').checked?'#eab308':'#262626'">
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="checkbox" id="tier_event_verified" style="accent-color:#eab308;width:16px;height:16px;" onchange="tierCheckChanged()">
        <span style="font-weight:700;color:#facc15;font-size:15px;">Event Verified</span>
        <span style="margin-left:auto;color:#d4d4d4;font-weight:600;">$0.75</span>
      </div>
      <div style="margin:6px 0 0 26px;font-size:12.5px;color:#a3a3a3;">Charged only if an event page is found</div>
      <div style="margin:2px 0 0 26px;font-size:11px;color:#737373;font-style:italic;">Includes events where email validation failed</div>
    </label>

    <p style="margin:12px 0 4px;font-size:12.5px;color:#a3a3a3;text-align:center;">No event found = No charge</p>
    <p style="margin:0 0 4px;font-size:11.5px;color:#525252;text-align:center;">Research fee applies to all nonprofits searched</p>
    <p style="margin:0 0 16px;font-size:11.5px;color:#525252;text-align:center;">Select multiple tiers &mdash; each lead is only charged once at its highest matching tier</p>
    <p id="tierError" style="display:none;margin:0 0 10px;font-size:13px;color:#ef4444;text-align:center;">Please select at least one tier.</p>

    <div style="display:flex;gap:10px;justify-content:flex-end;">
      <button onclick="closeTierModal()" style="padding:8px 20px;background:transparent;color:#a3a3a3;border:1px solid #333;border-radius:6px;cursor:pointer;font-size:14px;font-family:inherit;">Cancel</button>
      <button id="tierStartBtn" onclick="confirmTierAndSearch()" style="padding:8px 24px;background:#eab308;color:#000;border:none;border-radius:6px;cursor:pointer;font-weight:700;font-size:14px;font-family:inherit;">Start Search</button>
    </div>
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
let sseReconnectCount = 0;

// Auto-fill from IRS database selection
const irsData = sessionStorage.getItem('irs_nonprofits');
if (irsData) {
  inputEl.value = irsData;
  sessionStorage.removeItem('irs_nonprofits');
  updateCount();
}

// Rejoin a running batch if ?rejoin=JOB_ID is in URL
(function(){
  const params = new URLSearchParams(window.location.search);
  const rejoinId = params.get('rejoin');
  if (!rejoinId) return;

  // Clean URL without reload
  history.replaceState(null, '', '/');

  currentJobId = rejoinId;
  searchBtn.disabled = true;
  const stopBtn = document.getElementById('stopBtn');
  stopBtn.style.display = 'inline-block';
  stopBtn.disabled = false;
  progressSection.style.display = 'block';
  document.getElementById('searchDots').style.display = 'block';
  downloadSection.style.display = 'none';
  document.getElementById('billingSummary').style.display = 'none';
  terminal.innerHTML = '';
  counts = { found: 0, '3rdpty_found': 0, not_found: 0, uncertain: 0, error: 0 };
  processed = 0;
  totalNonprofits = 0;

  log('Rejoining live batch: ' + rejoinId, 'info');
  log('Replaying events...', 'info');

  currentEvtSource = new EventSource('/api/progress/' + rejoinId);
  sseReconnectCount = 0;

  currentEvtSource.onmessage = function(e) {
    sseReconnectCount = 0;
    const data = JSON.parse(e.data);

    switch (data.type) {
      case 'started':
        totalNonprofits = data.total;
        updateProgress();
        break;

      case 'processing':
        totalNonprofits = data.total;
        break;

      case 'balance':
        if (!IS_ADMIN) {
          balanceCents = data.balance;
          balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
        }
        break;

      case 'result':
        processed++;
        totalNonprofits = data.total;
        var st = data.status || 'uncertain';
        if (counts.hasOwnProperty(st)) counts[st]++;
        else counts.uncertain++;

        var _tD = { decision_maker: 'Decision Maker', outreach_ready: 'Outreach Ready', event_verified: 'Event Verified' };
        var _sL = { found: 'AUCTION FOUND', not_found: 'NO AUCTION FOUND', '3rdpty_found': '3RDPTY_FOUND', uncertain: 'UNCERTAIN', error: 'ERROR' };
        var msg = '[' + data.index + '/' + data.total + '] ' + (_sL[st] || st.toUpperCase()) + ': ' + data.nonprofit;
        if (IS_ADMIN && data.event_title) msg += ' -> ' + data.event_title;
        if (st === 'found' || st === '3rdpty_found') {
          if (data.tier && data.tier !== 'not_billable' && _tD[data.tier] && data.tier_price > 0) {
            msg += ' [' + _tD[data.tier] + ']';
          } else if (data.tier === 'not_billable') {
            msg += ' [Not Billable — missing event evidence]';
          } else if (data.tier && data.tier_price === 0 && _tD[data.tier]) {
            msg += ' [' + _tD[data.tier] + ' — tier not selected, no charge]';
          }
        }
        log(msg, st);
        updateStats();
        updateProgress();
        break;

      case 'balance_warning':
        log('BALANCE WARNING: ' + data.message, 'error');
        break;

      case 'complete':
        log('SEARCH COMPLETE in ' + data.elapsed + 's', 'complete');
        log('Auctions Found: ' + data.summary.found + ' | 3rd Party: ' + data.summary['3rdpty_found'] +
            ' | No Auction Found: ' + data.summary.not_found + ' | Uncertain: ' + data.summary.uncertain, 'complete');
        var cjid = data.job_id || currentJobId;
        downloadSection.style.display = 'block';
        document.getElementById('downloadCsv').href = '/api/download/' + cjid + '/csv';
        document.getElementById('downloadJson').href = '/api/download/' + cjid + '/json';
        document.getElementById('downloadXlsx').href = '/api/download/' + cjid + '/xlsx';
        searchBtn.disabled = false;
        document.getElementById('stopBtn').style.display = 'none';
        document.getElementById('searchDots').style.display = 'none';
        if (!IS_ADMIN && data.billing) {
          balanceCents = data.balance;
          balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
        }
        currentEvtSource.close(); currentEvtSource = null;
        break;

      case 'eta':
        var eM = Math.floor(data.elapsed / 60);
        var eS = data.elapsed % 60;
        var rM = Math.floor(data.remaining / 60);
        var rS = data.remaining % 60;
        var etaDiv = document.getElementById('etaDisplay');
        if (etaDiv) {
          etaDiv.style.display = 'block';
          document.getElementById('etaElapsed').textContent = 'Elapsed: ' + eM + 'm ' + (eS < 10 ? '0' : '') + eS + 's';
          document.getElementById('etaRemaining').textContent = '~' + rM + 'm ' + (rS < 10 ? '0' : '') + rS + 's remaining';
        }
        break;
    }
  };

  currentEvtSource.onerror = function() {
    sseReconnectCount++;
    if (sseReconnectCount <= 20) {
      log('Connection interrupted — reconnecting (' + sseReconnectCount + '/20)...', 'info');
    } else {
      log('SSE reconnect failed. Falling back to polling...', 'error');
      if (currentEvtSource) { currentEvtSource.close(); currentEvtSource = null; }
      _pollForCompletion(currentJobId);
    }
  };
})();

function updateCount() {
  const items = inputEl.value.replace(/,/g, '\\n').split('\\n').filter(s => s.trim());
  const n = items.length;
  countLabel.textContent = n + ' nonprofit' + (n !== 1 ? 's' : '');
  if (!IS_ADMIN && n > 0) {
    const fee = n <= 10000 ? 4 : (n <= 50000 ? 3 : 2);
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

// ── Tier Modal Functions ──
let _pendingSearchTiers = [];

function tierCheckChanged() {
  const tiers = ['decision_maker', 'outreach_ready', 'event_verified'];
  const anyChecked = tiers.some(t => document.getElementById('tier_' + t).checked);
  document.getElementById('tierStartBtn').disabled = !anyChecked;
  document.getElementById('tierError').style.display = anyChecked ? 'none' : 'block';
  tiers.forEach(t => {
    const label = document.getElementById('tierLabel_' + t);
    label.style.borderColor = document.getElementById('tier_' + t).checked ? '#eab308' : '#262626';
  });
}

function showTierModal() {
  const raw = inputEl.value.trim();
  if (!raw) return;
  // Show research fee estimate in modal
  const lines = raw.split(/[\\n,]+/).map(s => s.trim()).filter(Boolean);
  const n = lines.length;
  const fee = n <= 10000 ? 4 : (n <= 50000 ? 3 : 2);
  document.getElementById('tierResearchFee').textContent = 'Research fee: $' + (fee/100).toFixed(2) + ' per nonprofit (' + n + ' nonprofit' + (n !== 1 ? 's' : '') + ' = $' + (n * fee / 100).toFixed(2) + ')';
  tierCheckChanged();
  const modal = document.getElementById('tierModal');
  modal.style.display = 'flex';
}

function closeTierModal() {
  document.getElementById('tierModal').style.display = 'none';
}

function confirmTierAndSearch() {
  const tiers = ['decision_maker', 'outreach_ready', 'event_verified'];
  _pendingSearchTiers = tiers.filter(t => document.getElementById('tier_' + t).checked);
  if (_pendingSearchTiers.length === 0) {
    document.getElementById('tierError').style.display = 'block';
    return;
  }
  closeTierModal();
  _doSearch(_pendingSearchTiers);
}

function startSearch() {
  const raw = inputEl.value.trim();
  if (!raw) return;
  if (IS_ADMIN) {
    _doSearch(['decision_maker', 'outreach_ready', 'event_verified']);
  } else {
    showTierModal();
  }
}

async function stopSearch() {
  if (!currentJobId) return;
  try {
    await fetch('/api/stop/' + currentJobId, { method: 'POST' });
    log('Stop requested — waiting for current nonprofit to finish...', 'error');
  } catch(e) {}
  document.getElementById('stopBtn').disabled = true;
}

async function _doSearch(selectedTiers) {
  const raw = inputEl.value.trim();
  if (!raw) return;

  // Close any previous EventSource
  if (currentEvtSource) {
    currentEvtSource.close();
    currentEvtSource = null;
  }

  searchBtn.disabled = true;
  const stopBtn = document.getElementById('stopBtn');
  stopBtn.style.display = 'inline-block';
  stopBtn.disabled = false;
  progressSection.style.display = 'block';
  document.getElementById('searchDots').style.display = 'block';
  downloadSection.style.display = 'none';
  document.getElementById('billingSummary').style.display = 'none';
  terminal.innerHTML = '';
  counts = { found: 0, '3rdpty_found': 0, not_found: 0, uncertain: 0, error: 0 };
  processed = 0;
  totalNonprofits = 0;
  updateStats();
  updateProgress();

  log('Starting search...', 'info');
  if (!IS_ADMIN) {
    const tierNames = { decision_maker: 'Decision Maker', outreach_ready: 'Outreach Ready', event_verified: 'Event Verified' };
    log('Selected tiers: ' + selectedTiers.map(t => tierNames[t]).join(', '), 'info');
  }

  try {
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nonprofits: raw, selected_tiers: selectedTiers }),
    });

    if (!res.ok) {
      const err = await res.json();
      if (res.status === 402 && err.affordable_count) {
        log('Insufficient balance: ' + err.error, 'error');
        log('Tip: Reduce your list to ~' + err.affordable_count + ' nonprofits, or top up your wallet.', 'info');
      } else {
        log('Error: ' + (err.error || 'Unknown error'), 'error');
      }
      searchBtn.disabled = false; document.getElementById('stopBtn').style.display = 'none'; document.getElementById('searchDots').style.display = 'none';
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

    sseReconnectCount = 0;

    currentEvtSource.onmessage = (e) => {
      sseReconnectCount = 0;  // Reset on every successful message
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

          const _tierDisplay = { decision_maker: 'Decision Maker', outreach_ready: 'Outreach Ready', event_verified: 'Event Verified' };
          const _tierColor = { decision_maker: '#4ade80', outreach_ready: '#60a5fa', event_verified: '#facc15' };
          const _statusLabels = { found: 'AUCTION FOUND', not_found: 'NO AUCTION FOUND', '3rdpty_found': '3RDPTY_FOUND', uncertain: 'UNCERTAIN', error: 'ERROR' };
          let msg = '[' + data.index + '/' + data.total + '] ' + (_statusLabels[status] || status.toUpperCase()) + ': ' + data.nonprofit;
          if (IS_ADMIN && data.event_title) msg += ' -> ' + data.event_title;
          log(msg, status);

          // Add colored tier badge after log line
          if (status === 'found' || status === '3rdpty_found') {
            const lastLine = terminal.lastElementChild;
            if (lastLine) {
              if (data.tier && data.tier !== 'not_billable' && _tierDisplay[data.tier] && data.tier_price > 0) {
                const badge = document.createElement('span');
                badge.textContent = _tierDisplay[data.tier];
                badge.style.cssText = 'margin-left:8px;font-size:10px;padding:1px 6px;border-radius:3px;font-weight:700;color:#000;background:' + _tierColor[data.tier];
                lastLine.appendChild(badge);
                const priceTag = document.createElement('span');
                priceTag.textContent = '$' + (data.tier_price / 100).toFixed(2);
                priceTag.style.cssText = 'margin-left:4px;font-size:10px;color:#a3a3a3;';
                lastLine.appendChild(priceTag);
              } else if (data.tier === 'not_billable') {
                const nb = document.createElement('span');
                nb.textContent = 'Not Billable — missing event evidence';
                nb.style.cssText = 'margin-left:8px;font-size:10px;padding:1px 6px;border-radius:3px;font-weight:600;color:#f87171;background:#1c1917;border:1px solid #7f1d1d;';
                lastLine.appendChild(nb);
              } else if (data.tier && data.tier_price === 0 && _tierDisplay[data.tier]) {
                const nb2 = document.createElement('span');
                nb2.textContent = _tierDisplay[data.tier] + ' — tier not selected, no charge';
                nb2.style.cssText = 'margin-left:8px;font-size:10px;padding:1px 6px;border-radius:3px;font-weight:600;color:#a3a3a3;background:#1c1917;border:1px solid #404040;';
                lastLine.appendChild(nb2);
              }
            }
          }

          // Show email verified badge
          if (data.email_status === 'deliverable') {
            const lastLine2 = terminal.lastElementChild;
            if (lastLine2 && lastLine2.tagName !== 'DIV') {
              const evBadge = document.createElement('span');
              evBadge.textContent = 'Email Verified';
              evBadge.style.cssText = 'margin-left:6px;font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600;color:#065f46;background:#d1fae5;';
              lastLine2.appendChild(evBadge);
            }
          }

          // Add Make Exclusive button for billable leads
          if (!IS_ADMIN && status !== 'not_found' && data.tier && data.tier !== 'not_billable' && data.event_url && data.event_title) {
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
          log('Auctions Found: ' + data.summary.found + ' | 3rd Party: ' + data.summary['3rdpty_found'] +
              ' | No Auction Found: ' + data.summary.not_found + ' | Uncertain: ' + data.summary.uncertain, 'complete');

          // Show billing summary for non-admin
          if (!IS_ADMIN && data.billing) {
            const bs = document.getElementById('billingSummary');
            const b = data.billing;
            let html = '<div class="row"><span>Searched: ' + totalNonprofits + ' nonprofits</span><span>$' + (b.research_fees / 100).toFixed(2) + '</span></div>';
            const _bTierNames = { decision_maker: 'Decision Maker', outreach_ready: 'Outreach Ready', event_verified: 'Event Verified' };
            if (b.lead_fees) {
              for (const [tier, info] of Object.entries(b.lead_fees)) {
                const tName = _bTierNames[tier] || tier;
                html += '<div class="row"><span>' + info.count + ' ' + tName + ' leads x $' + (info.price_each / 100).toFixed(2) + '</span><span>$' + (info.total / 100).toFixed(2) + '</span></div>';
              }
            }
            html += '<div class="row total"><span>Total charged</span><span>$' + (b.total_charged / 100).toFixed(2) + '</span></div>';
            html += '<div class="row"><span>Remaining balance</span><span style="color:#4ade80">$' + (data.balance / 100).toFixed(2) + '</span></div>';
            if (data.billable_count !== undefined) {
              html += '<div class="row" style="margin-top:8px;color:#a3a3a3;"><span>Billable leads: ' + data.billable_count + '</span></div>';
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

          searchBtn.disabled = false; document.getElementById('stopBtn').style.display = 'none'; document.getElementById('searchDots').style.display = 'none';
          currentEvtSource.close();
          currentEvtSource = null;
          break;

        case 'error':
          log('ERROR: ' + data.message, 'error');
          searchBtn.disabled = false; document.getElementById('stopBtn').style.display = 'none'; document.getElementById('searchDots').style.display = 'none';
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

        case 'email_validating':
        case 'email_result':
        case 'email_purged':
          break;

        case 'heartbeat':
          break;
      }
    };

    currentEvtSource.onerror = () => {
      sseReconnectCount++;
      if (sseReconnectCount <= 20) {
        // Let EventSource auto-reconnect (built-in). Don't call .close()!
        log('Connection interrupted — reconnecting (' + sseReconnectCount + '/20)...', 'info');
      } else {
        // Give up on SSE, fall back to polling
        log('SSE reconnect failed after 20 attempts. Falling back to polling...', 'error');
        if (currentEvtSource) { currentEvtSource.close(); currentEvtSource = null; }
        _pollForCompletion(currentJobId);
      }
    };

  } catch (err) {
    log('Request failed: ' + err.message, 'error');
    searchBtn.disabled = false; document.getElementById('searchDots').style.display = 'none';
  }
}

function _pollForCompletion(jobId) {
  if (!jobId) return;
  log('Polling for job completion...', 'info');
  const pollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/job-status/' + jobId);
      if (!res.ok) { clearInterval(pollTimer); return; }
      const data = await res.json();
      if (data.status === 'complete') {
        clearInterval(pollTimer);
        log('Job complete (via polling)!', 'complete');
        if (data.summary) {
          log('Auctions Found: ' + (data.summary.found || 0) + ' | 3rd Party: ' + (data.summary['3rdpty_found'] || 0) +
              ' | No Auction Found: ' + (data.summary.not_found || 0) + ' | Uncertain: ' + (data.summary.uncertain || 0), 'complete');
        }
        if (!IS_ADMIN && data.billing) {
          balanceCents = data.balance || 0;
          balDisplay.textContent = '$' + (balanceCents / 100).toFixed(2);
        }
        const cjid = data.job_id || jobId;
        downloadSection.style.display = 'block';
        document.getElementById('downloadCsv').href = '/api/download/' + cjid + '/csv';
        document.getElementById('downloadJson').href = '/api/download/' + cjid + '/json';
        document.getElementById('downloadXlsx').href = '/api/download/' + cjid + '/xlsx';
        searchBtn.disabled = false; document.getElementById('stopBtn').style.display = 'none'; document.getElementById('searchDots').style.display = 'none';
      } else if (data.status === 'error') {
        clearInterval(pollTimer);
        log('Job failed (server error).', 'error');
        searchBtn.disabled = false; document.getElementById('stopBtn').style.display = 'none'; document.getElementById('searchDots').style.display = 'none';
      }
    } catch (e) {
      // Network error during poll — keep trying
    }
  }, 10000);
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

function newSearch() {
  if (currentEvtSource) { currentEvtSource.close(); currentEvtSource = null; }
  currentJobId = null;
  terminal.innerHTML = '';
  counts = { found: 0, '3rdpty_found': 0, not_found: 0, uncertain: 0, error: 0 };
  processed = 0;
  totalNonprofits = 0;
  updateStats();
  updateProgress();
  progressSection.style.display = 'none';
  downloadSection.style.display = 'none';
  document.getElementById('billingSummary').style.display = 'none';
  document.getElementById('jsonViewer').style.display = 'none';
  document.getElementById('etaDisplay').style.display = 'none';
  document.getElementById('searchDots').style.display = 'none';
  searchBtn.disabled = false;
  inputEl.value = '';
  inputEl.focus();
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
<title>Auction Finder - Search Nonprofit Database</title>
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
        <h2>Nonprofit Database USA</h2>
        <p class="subtitle">Verified nonprofit organizations</p>
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
    <p style="font-size:10px;color:#64748b;margin-top:6px;">All organizations in this database have confirmed auction events.</p>

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
    has_website: true,
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
<title>Auction Finder - Results</title>
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
<title>Auction Finder - File Merger</title>
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
  .file-list { display:flex; flex-direction:column; gap:6px; max-height:320px; overflow-y:auto; padding-right:4px; }
  .file-list::-webkit-scrollbar { width:4px; }
  .file-list::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }
  .file-item { display:flex; align-items:flex-start; gap:10px; padding:10px 12px; background:#1a1a1a; border:1px solid #262626; border-radius:8px; }
  .file-icon { width:32px; height:32px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:13px; background:#262626; color:#eab308; flex-shrink:0; margin-top:2px; }
  .mode-json .file-icon { color:#7c3aed; }
  .file-info { flex:1; min-width:0; }
  .file-name { font-size:13px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .file-meta { font-size:11px; color:#737373; }
  .file-domain-row { display:flex; align-items:center; gap:6px; margin-top:5px; }
  .file-domain-label { font-size:10px; font-weight:600; color:#737373; white-space:nowrap; letter-spacing:0.3px; text-transform:uppercase; }
  .file-domain-input { flex:1; min-width:0; padding:4px 8px; border:1px solid #333; border-radius:6px; background:#262626; color:#eab308; font-family:'SF Mono','Consolas',monospace; font-size:12px; font-weight:500; outline:none; transition:border-color 0.2s, box-shadow 0.2s; }
  .file-domain-input:focus { border-color:#eab308; box-shadow:0 0 0 2px rgba(234,179,8,0.15); }
  .file-remove { width:28px; height:28px; border-radius:6px; border:none; background:transparent; color:#737373; cursor:pointer; display:flex; align-items:center; justify-content:center; font-size:12px; flex-shrink:0; align-self:flex-start; margin-top:2px; }
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
  .preview-box::-webkit-scrollbar-thumb { background:#eab308; border-radius:4px; }

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
  <p class="subtitle">Upload multiple CSV or JSON result files, tag each with a query domain, and merge into a single download.</p>

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
  var csvEntries = [];   // [{file, domain}]
  var jsonEntries = [];  // [{file, domain}]
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

  function getEntries() { return currentMode === 'csv' ? csvEntries : jsonEntries; }
  function setEntries(e) { if (currentMode === 'csv') csvEntries = e; else jsonEntries = e; }
  function domainFromName(name) { return name.replace(/\\.[^.]+$/, ''); }

  function addFiles(newFiles) {
    var current = getEntries();
    var remaining = 20 - current.length;
    if (remaining <= 0) { showStatus('error', 'Maximum 20 files. Remove some first.'); return; }
    var ext = currentMode === 'csv' ? '.csv' : '.json';
    var accepted = [], rejected = 0;
    for (var i = 0; i < newFiles.length && accepted.length < remaining; i++) {
      var f = newFiles[i];
      if (f.name.toLowerCase().endsWith(ext)) {
        var dup = false;
        for (var j = 0; j < current.length; j++) { if (current[j].file.name === f.name) { dup = true; break; } }
        if (!dup) accepted.push({ file: f, domain: domainFromName(f.name) }); else rejected++;
      } else { rejected++; }
    }
    if (rejected > 0) showStatus('error', rejected + ' file(s) skipped (wrong type or duplicate).');
    if (accepted.length > 0) { setEntries(current.concat(accepted)); clearResults(); renderFileList(); }
  }

  fileInput.addEventListener('change', function(e) { if (e.target.files.length > 0) { addFiles(Array.from(e.target.files)); e.target.value = ''; } });
  dropZone.addEventListener('dragover', function(e) { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', function(e) { e.preventDefault(); dropZone.classList.remove('dragover'); });
  dropZone.addEventListener('drop', function(e) { e.preventDefault(); dropZone.classList.remove('dragover'); if (e.dataTransfer.files.length > 0) addFiles(Array.from(e.dataTransfer.files)); });

  function fmtSize(b) { if (b < 1024) return b+' B'; if (b < 1048576) return (b/1024).toFixed(1)+' KB'; return (b/1048576).toFixed(1)+' MB'; }

  function renderFileList() {
    var entries = getEntries();
    if (entries.length === 0) { fileListSection.style.display='none'; actionsArea.style.display='none'; return; }
    fileListSection.style.display = 'block';
    actionsArea.style.display = 'flex';
    fileCount.textContent = entries.length + ' / 20';
    btnMerge.disabled = entries.length < 2;
    fileListSection.className = 'file-list-section ' + (currentMode==='csv'?'mode-csv':'mode-json');
    var html = '';
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      html += '<div class="file-item">' +
        '<div class="file-icon">'+(currentMode==='csv'?'CSV':'{ }')+'</div>' +
        '<div class="file-info">' +
          '<div class="file-name">'+escapeHtml(entry.file.name)+'</div>' +
          '<div class="file-meta">'+fmtSize(entry.file.size)+'</div>' +
          '<div class="file-domain-row">' +
            '<span class="file-domain-label">domain:</span>' +
            '<input class="file-domain-input" type="text" value="'+escapeHtml(entry.domain)+'" ' +
              'data-index="'+i+'" onchange="updateDomain(this)" oninput="updateDomain(this)" />' +
          '</div>' +
        '</div>' +
        '<button class="file-remove" onclick="removeFile('+i+')" title="Remove">&times;</button>' +
      '</div>';
    }
    fileList.innerHTML = html;
  }

  window.updateDomain = function(el) {
    var idx = parseInt(el.getAttribute('data-index'), 10);
    var entries = getEntries();
    if (entries[idx]) entries[idx].domain = el.value;
  };

  window.removeFile = function(i) { var e=getEntries(); e.splice(i,1); setEntries(e); clearResults(); renderFileList(); };

  window.confirmClear = function() {
    var entries = getEntries(); if (entries.length===0) return;
    var ov = document.createElement('div'); ov.className='modal-overlay';
    ov.innerHTML='<div class="modal-box"><h3>Clear All Files?</h3><p>Remove all '+entries.length+' '+currentMode.toUpperCase()+' file(s) and results.</p><div class="modal-actions"><button class="modal-btn modal-btn-cancel" id="mc">Cancel</button><button class="modal-btn modal-btn-confirm" id="mk">Clear All</button></div></div>';
    document.body.appendChild(ov);
    document.getElementById('mc').onclick=function(){ov.remove();};
    document.getElementById('mk').onclick=function(){ov.remove();setEntries([]);clearResults();renderFileList();};
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
    var entries=getEntries(); if(entries.length<2)return;
    clearResults(); btnMerge.disabled=true; mergeLabel.textContent='Merging...';
    try { if(currentMode==='csv') await mergeCSV(entries); else await mergeJSON(entries); } catch(e) { showStatus('error',e.message||'Merge error.'); }
    mergeLabel.textContent=currentMode==='csv'?'Merge CSV Files':'Merge JSON Files'; btnMerge.disabled=false;
  };

  async function mergeCSV(entries) {
    var allParsed=[];
    for(var i=0;i<entries.length;i++){var t=await readFile(entries[i].file);allParsed.push({name:entries[i].file.name,domain:entries[i].domain,rows:parseCSV(t)});}
    if(allParsed[0].rows.length===0)throw new Error('First file "'+allParsed[0].name+'" is empty.');
    var header=allParsed[0].rows[0];
    var headerStr=header.join(',').toLowerCase().trim();

    // Columns to exclude from output
    var excludeCols=['event_time','venue_name','venue_address'];
    var excludeIndices=[];
    for(var ei=0;ei<header.length;ei++){
      var colLower=header[ei].trim().toLowerCase();
      for(var ec=0;ec<excludeCols.length;ec++){if(colLower===excludeCols[ec]){excludeIndices.push(ei);break;}}
    }

    // Build filtered header (without excluded columns)
    var filteredHeader=[];
    for(var fh=0;fh<header.length;fh++){if(excludeIndices.indexOf(fh)===-1)filteredHeader.push(header[fh]);}

    // Build merged rows with excluded columns removed and query_domain appended
    var mergedRows=[];
    for(var f=0;f<allParsed.length;f++){
      var p=allParsed[f];
      if(p.rows.length===0)continue;
      if(f>0){var thisHeader=p.rows[0].join(',').toLowerCase().trim();if(thisHeader!==headerStr)throw new Error('Header mismatch in "'+p.name+'". Expected: '+header.join(', '));}
      var dataRows=p.rows.slice(1).filter(function(r){return r.length>1||(r.length===1&&r[0].trim()!=='');});
      for(var d=0;d<dataRows.length;d++){
        var filteredRow=[];
        for(var c=0;c<dataRows[d].length;c++){if(excludeIndices.indexOf(c)===-1)filteredRow.push(dataRows[d][c]);}
        filteredRow.push(p.domain);
        mergedRows.push(filteredRow);
      }
    }

    // Build output with query_domain column
    var outputHeader=filteredHeader.concat(['query_domain']);
    var lines=[rowToCSV(outputHeader)];
    for(var k=0;k<mergedRows.length;k++)lines.push(rowToCSV(mergedRows[k]));
    var out=lines.join('\\n');

    showStatus('success','Merged '+entries.length+' files \\u2014 '+mergedRows.length+' rows (with query_domain).');
    createDownload(out,'merged.csv','text/csv','Download Merged CSV ('+mergedRows.length+' rows)');
    showPreview(out,'csv',mergedRows.length);
  }

  async function mergeJSON(entries) {
    var allData=[];
    var jsonExclude=['event_time','venue_name','venue_address'];
    for(var i=0;i<entries.length;i++){
      var t=await readFile(entries[i].file);
      var domain=entries[i].domain;
      var parsed;try{parsed=JSON.parse(t);}catch(e){throw new Error('Invalid JSON in "'+entries[i].file.name+'"');}
      var items;
      if(Array.isArray(parsed))items=parsed;
      else if(typeof parsed==='object'&&parsed!==null)items=[parsed];
      else throw new Error('"'+entries[i].file.name+'" unsupported JSON.');
      for(var j=0;j<items.length;j++){
        if(typeof items[j]==='object'&&items[j]!==null){
          for(var je=0;je<jsonExclude.length;je++){delete items[j][jsonExclude[je]];}
          items[j].query_domain=domain;
        }
      }
      allData=allData.concat(items);
    }
    var out=JSON.stringify(allData,null,2);
    showStatus('success','Merged '+entries.length+' files \\u2014 '+allData.length+' items (with query_domain).');
    createDownload(out,'merged.json','application/json','Download Merged JSON ('+allData.length+' items)');
    showPreview(out,'json',allData.length);
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
<title>Auction Finder - JSON Field Analyzer</title>
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
  .dist-tab { padding:5px 12px; border:1px solid #333; border-radius:6px; background:#1a1a1a; color:#a3a3a3; font-family:'IBM Plex Mono',monospace; font-size:12px; cursor:pointer; transition:all .2s; }
  .dist-tab:hover { border-color:#eab308; color:#f5f5f5; }
  .dist-tab.active { background:#eab308; color:#000; border-color:#eab308; }
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
    <p class="sub">or click to browse &mdash; up to 10 MB per file</p>
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
    <div class="section" id="valueDistSection" class="hidden">
      <div class="section-title">Value Distribution <span id="distFieldLabel" style="color:#a3a3a3;font-weight:400;"></span></div>
      <div style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;" id="distFieldTabs"></div>
      <div class="ftable-wrap">
        <div class="ftable-header"><span>Value breakdown for selected field</span></div>
        <div class="ftable-body" id="distBody"></div>
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
  var files={};var activeFile=null;var currentSort="name";
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
    if(Array.isArray(data)){
      // Check if array items are wrapper objects with a "results" key (merged files)
      if(data.length>0&&data[0]&&typeof data[0]==="object"&&!Array.isArray(data[0])&&Array.isArray(data[0].results)){
        var merged=[];for(var i=0;i<data.length;i++){
          if(data[i]&&Array.isArray(data[i].results)){
            for(var j=0;j<data[i].results.length;j++){
              var rec=data[i].results[j];
              if(rec&&typeof rec==="object"&&!Array.isArray(rec))merged.push(rec);
            }
          }
        }
        return merged;
      }
      return flattenObjects(data);
    }
    if(data&&typeof data==="object"){
      if(Array.isArray(data.results))return flattenObjects(data.results);
      return flattenObjects(Object.values(data));
    }
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
    renderFieldTable();renderCombos();renderDistTabs();
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

  var DIST_FIELDS=["auction_type","status","email_status","confidence_score","event_type"];
  var activeDistField=null;

  function renderDistTabs(){
    var data=files[activeFile];if(!data)return;
    var tabs=document.getElementById("distFieldTabs");tabs.innerHTML="";
    var available=[];
    for(var i=0;i<DIST_FIELDS.length;i++){
      if(data.fieldNames.indexOf(DIST_FIELDS[i])!==-1)available.push(DIST_FIELDS[i]);
    }
    if(available.length===0){document.getElementById("valueDistSection").style.display="none";return;}
    document.getElementById("valueDistSection").style.display="";
    if(!activeDistField||available.indexOf(activeDistField)===-1)activeDistField=available[0];
    for(var j=0;j<available.length;j++){(function(field){
      var btn=document.createElement("button");btn.className="dist-tab"+(field===activeDistField?" active":"");
      btn.textContent=field;btn.addEventListener("click",function(){activeDistField=field;renderDistTabs();renderDistValues();});
      tabs.appendChild(btn);
    })(available[j]);}
    renderDistValues();
  }

  function renderDistValues(){
    var data=files[activeFile];if(!data||!activeDistField)return;
    var counts={};var total=data.records.length;
    for(var i=0;i<total;i++){
      var val=data.records[i][activeDistField];
      var key=(val===undefined||val===null||String(val).trim()==="")?"(empty)":String(val).trim().toLowerCase();
      if(!counts[key])counts[key]=0;counts[key]++;
    }
    var sorted=[];for(var k in counts){sorted.push({value:k,count:counts[k]});}
    sorted.sort(function(a,b){return b.count-a.count;});
    document.getElementById("distFieldLabel").textContent="("+sorted.length+" unique values)";
    var body=document.getElementById("distBody");body.innerHTML="";
    var head=document.createElement("div");head.className="frow-head frow";
    head.innerHTML="<span>Value</span><span>Count</span><span>%</span><span>Distribution</span>";body.appendChild(head);
    var colors={"live":"#4ADE80","silent":"#60a5fa","both":"#A78BFA","online":"#eab308","unknown":"#F87171","(empty)":"#525252",
      "found":"#4ADE80","3rdpty_found":"#60a5fa","not_found":"#FB923C","uncertain":"#F87171","error":"#EF4444",
      "deliverable":"#4ADE80","undeliverable":"#F87171","risky":"#FB923C","unknown":"#F87171"};
    for(var i=0;i<sorted.length;i++){
      var s=sorted[i];var pct=total>0?Math.round((s.count/total)*100):0;
      var row=document.createElement("div");row.className="frow";
      var n=document.createElement("div");n.className="frow-name";n.textContent=s.value;
      var c=colors[s.value]||"#eab308";n.style.color=c;row.appendChild(n);
      var p=document.createElement("div");p.className="frow-pop";p.textContent=s.count;row.appendChild(p);
      var m=document.createElement("div");m.className="frow-miss";m.style.color="#a3a3a3";m.textContent=pct+"%";row.appendChild(m);
      var bw=document.createElement("div");bw.style.cssText="display:flex;align-items:center;gap:8px;";
      var bar=document.createElement("div");bar.className="frow-bar";bar.style.flex="1";
      var fill=document.createElement("div");fill.style.cssText="height:100%;border-radius:4px;background:"+c+";width:"+pct+"%;transition:width .6s ease;";
      bar.appendChild(fill);bw.appendChild(bar);row.appendChild(bw);body.appendChild(row);
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
      var ownKeys=Object.keys(rec);
      for(var j=0;j<ownKeys.length;j++){if(isEmpty(rec[ownKeys[j]])){allFilled=false;break;}}
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
<title>Auction Finder - Getting Started</title>
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
  <h2>Getting Started with Auction Finder</h2>
  <p class="subtitle">Follow these 4 steps to find nonprofit auction events and download verified leads.</p>

  <div class="step">
    <div class="step-num">1</div>
    <div class="step-content">
      <h3>Search the Nonprofit Database</h3>
      <p>Go to the <a href="/database">Database</a> page to browse our curated database of nonprofits with confirmed auction events. Filter by <strong>state, city, region, event type</strong>, and <strong>prospect tier</strong> (A+, A, B+, C). Use the financial filters to narrow by revenue, fundraising income, or event gross receipts.</p>
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
      <p style="margin-top:8px;"><strong style="color:#4ade80;">Decision Maker ($1.75)</strong> &mdash; named contact + verified email + event details &nbsp;|&nbsp; <strong style="color:#60a5fa;">Outreach Ready ($1.25)</strong> &mdash; verified email + event details &nbsp;|&nbsp; <strong style="color:#facc15;">Event Verified ($0.75)</strong> &mdash; event title, date &amp; URL only</p>
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
<title>Auction Finder - Support</title>
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
<title>Auction Finder - New Support Ticket</title>
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
<title>Auction Finder - Ticket #{{TICKET_ID}}</title>
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

API_DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auction Finder - API Documentation</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono', 'Consolas', monospace; background: #121212; color: #f5f5f5; min-height: 100vh; }
  {{SIDEBAR_CSS}}
  .container { max-width: 860px; margin: 0 auto; padding: 24px; }
  h2 { font-size: 20px; color: #eab308; margin-bottom: 8px; }
  .subtitle { font-size: 13px; color: #a3a3a3; margin-bottom: 32px; }
  h3 { font-size: 15px; color: #f5f5f5; margin: 24px 0 8px; }
  p, li { font-size: 13px; color: #a3a3a3; line-height: 1.7; }
  ul { padding-left: 20px; margin-bottom: 16px; }
  .endpoint { background: #0a0a0a; border: 1px solid #262626; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
  .endpoint .method { display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 700; margin-right: 8px; }
  .method-post { background: #14532d; color: #86efac; }
  .method-get { background: #1e3a5f; color: #7dd3fc; }
  .endpoint .path { font-size: 14px; font-weight: 600; color: #f5f5f5; }
  .endpoint .desc { font-size: 12px; color: #a3a3a3; margin-top: 6px; }
  pre { background: #000; border: 1px solid #262626; border-radius: 8px; padding: 16px; overflow-x: auto; margin: 12px 0; font-size: 12px; line-height: 1.6; color: #d4d4d4; }
  code { font-family: inherit; }
  .param-table { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 12px; }
  .param-table th { text-align: left; color: #737373; font-size: 10px; text-transform: uppercase; padding: 6px 8px; border-bottom: 1px solid #262626; }
  .param-table td { padding: 6px 8px; border-bottom: 1px solid #1a1a1a; color: #d4d4d4; }
  .param-table .type { color: #eab308; font-size: 11px; }
  .note { background: #1a1500; border: 1px solid #332d00; border-radius: 8px; padding: 14px; margin: 16px 0; }
  .note p { color: #eab308; font-size: 12px; }
  a { color: #eab308; text-decoration: none; }
  a:hover { text-decoration: underline; }
  pre code.hljs { background: transparent; padding: 0; }
  .hljs { color: #d4d4d4; }
  .hljs-keyword, .hljs-built_in, .hljs-type { color: #c084fc; }
  .hljs-string, .hljs-template-variable { color: #fb923c; }
  .hljs-number, .hljs-literal { color: #fb923c; }
  .hljs-comment { color: #525252; font-style: italic; }
  .hljs-function .hljs-title, .hljs-title.function_ { color: #e9d5ff; }
  .hljs-attr, .hljs-property { color: #c084fc; }
  .hljs-params { color: #d4d4d4; }
  .hljs-meta { color: #a78bfa; }
  .hljs-variable, .hljs-name { color: #e2e8f0; }
  .hljs-punctuation { color: #a3a3a3; }
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/python.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/json.min.js"></script>
</head>
<body>
{{SIDEBAR_HTML}}
<div class="main-content">
<div class="container">
  <h2>API Documentation</h2>
  <p class="subtitle">Use the Auction Finder REST API to run searches, check status, and download results programmatically.</p>

  <div class="note"><p>All requests require an API key. Generate one at <a href="/settings/api-keys">Settings &rarr; API Keys</a>. Pass it as a Bearer token in the Authorization header.</p></div>

  <h3>Authentication</h3>
  <pre>Authorization: Bearer ak_your_api_key_here</pre>

  <!-- SEARCH -->
  <div class="endpoint">
    <span class="method method-post">POST</span>
    <span class="path">/api/v1/search</span>
    <p class="desc">Submit a list of nonprofit domains to research. Returns a job_id to track progress.</p>
    <table class="param-table">
      <thead><tr><th>Parameter</th><th>Type</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td>domains</td><td class="type">list[str]</td><td>List of nonprofit domains (e.g. ["habitat.org", "redcross.org"])</td></tr>
        <tr><td>selected_tiers</td><td class="type">list[str]</td><td>Optional. Tiers to bill: "decision_maker", "outreach_ready", "event_verified". Default: all.</td></tr>
      </tbody>
    </table>
    <p style="margin-top:12px;color:#737373;font-size:11px;">Example:</p>
    <pre>import requests

API_KEY = "ak_your_api_key_here"
BASE = "https://auctionintel.app"

# Start a search
resp = requests.post(
    f"{BASE}/api/v1/search",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "domains": [
            "habitat.org",
            "redcross.org",
            "unitedway.org"
        ],
        "selected_tiers": ["decision_maker", "outreach_ready"]
    }
)
data = resp.json()
job_id = data["job_id"]
print(f"Job started: {job_id} ({data['total_domains']} domains)")</pre>
    <p style="margin-top:8px;color:#737373;font-size:11px;">Response:</p>
    <pre>{
  "job_id": "job_20260303_141522_a1b2c3d4",
  "total_domains": 3,
  "status": "running"
}</pre>
  </div>

  <!-- STATUS -->
  <div class="endpoint">
    <span class="method method-get">GET</span>
    <span class="path">/api/v1/status/&lt;job_id&gt;</span>
    <p class="desc">Check job progress. Poll this endpoint until status is "complete" or "error".</p>
    <p style="margin-top:12px;color:#737373;font-size:11px;">Example:</p>
    <pre>import time

# Poll until complete
while True:
    resp = requests.get(
        f"{BASE}/api/v1/status/{job_id}",
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    status = resp.json()
    print(f"  {status['processed']}/{status['total']} processed, "
          f"{status['found']} found")

    if status["status"] in ("complete", "error"):
        break
    time.sleep(5)</pre>
    <p style="margin-top:8px;color:#737373;font-size:11px;">Response:</p>
    <pre>{
  "job_id": "job_20260303_141522_a1b2c3d4",
  "status": "running",
  "total": 3,
  "processed": 1,
  "found": 1,
  "eta_seconds": 45,
  "balance_cents": 1850
}</pre>
  </div>

  <!-- RESULTS -->
  <div class="endpoint">
    <span class="method method-get">GET</span>
    <span class="path">/api/v1/results/&lt;job_id&gt;?format=csv|json|xlsx</span>
    <p class="desc">Download results once the job is complete. Returns a file attachment.</p>
    <table class="param-table">
      <thead><tr><th>Parameter</th><th>Type</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td>format</td><td class="type">string</td><td>Output format: "csv", "json", or "xlsx". Default: "csv".</td></tr>
      </tbody>
    </table>
    <p style="margin-top:12px;color:#737373;font-size:11px;">Example:</p>
    <pre># Download CSV results
resp = requests.get(
    f"{BASE}/api/v1/results/{job_id}?format=csv",
    headers={"Authorization": f"Bearer {API_KEY}"}
)
with open(f"results_{job_id}.csv", "wb") as f:
    f.write(resp.content)
print(f"Saved {len(resp.content)} bytes")

# Or get JSON for programmatic use
resp = requests.get(
    f"{BASE}/api/v1/results/{job_id}?format=json",
    headers={"Authorization": f"Bearer {API_KEY}"}
)
results = resp.json()
for r in results:
    print(f"  {r['nonprofit_name']}: {r['event_title']}")</pre>
  </div>

  <!-- STOP -->
  <div class="endpoint">
    <span class="method method-post">POST</span>
    <span class="path">/api/v1/stop/&lt;job_id&gt;</span>
    <p class="desc">Stop a running job. The job finishes processing the current domain and halts.</p>
    <pre># Stop a running job
resp = requests.post(
    f"{BASE}/api/v1/stop/{job_id}",
    headers={"Authorization": f"Bearer {API_KEY}"}
)
print(resp.json()["message"])</pre>
  </div>

  <!-- RESUME -->
  <div class="endpoint">
    <span class="method method-post">POST</span>
    <span class="path">/api/v1/resume/&lt;job_id&gt;</span>
    <p class="desc">Resume an interrupted or stopped job. Creates a new child job for the remaining domains.</p>
    <pre># Resume a stopped/failed job
resp = requests.post(
    f"{BASE}/api/v1/resume/{job_id}",
    headers={"Authorization": f"Bearer {API_KEY}"}
)
new_job = resp.json()
print(f"Resumed as {new_job['job_id']} — {new_job['remaining_domains']} domains left")</pre>
  </div>

  <!-- FULL EXAMPLE -->
  <h3>Complete Example</h3>
  <pre>import requests
import time

API_KEY = "ak_your_api_key_here"
BASE = "https://auctionintel.app"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# 1. Start search
resp = requests.post(f"{BASE}/api/v1/search", headers=HEADERS, json={
    "domains": ["habitat.org", "redcross.org", "salvationarmy.org"],
    "selected_tiers": ["decision_maker"]
})
job_id = resp.json()["job_id"]
print(f"Started job {job_id}")

# 2. Poll until done
while True:
    status = requests.get(f"{BASE}/api/v1/status/{job_id}", headers=HEADERS).json()
    print(f"  {status['processed']}/{status['total']} — {status['found']} found")
    if status["status"] in ("complete", "error"):
        break
    time.sleep(5)

# 3. Download results
resp = requests.get(f"{BASE}/api/v1/results/{job_id}?format=csv", headers=HEADERS)
with open(f"results_{job_id}.csv", "wb") as f:
    f.write(resp.content)
print(f"Done! Saved results_{job_id}.csv")</pre>

  <h3>Error Codes</h3>
  <table class="param-table">
    <thead><tr><th>Code</th><th>Meaning</th></tr></thead>
    <tbody>
      <tr><td>200</td><td>Success</td></tr>
      <tr><td>202</td><td>Job still running, no results yet</td></tr>
      <tr><td>400</td><td>Bad request (missing params, invalid format)</td></tr>
      <tr><td>401</td><td>Invalid or missing API key</td></tr>
      <tr><td>402</td><td>Insufficient wallet balance</td></tr>
      <tr><td>404</td><td>Job or resource not found</td></tr>
    </tbody>
  </table>

  <h3>Pricing</h3>
  <ul>
    <li><strong>Research fee:</strong> $0.04 per nonprofit domain (charged regardless of result)</li>
    <li><strong>Decision Maker lead:</strong> $1.75 (has event + contact name + email)</li>
    <li><strong>Outreach Ready lead:</strong> $1.25 (has event + email)</li>
    <li><strong>Event Verified lead:</strong> $0.75 (has verified event page only)</li>
  </ul>

</div>
</div>
<script>
document.querySelectorAll('pre').forEach(function(pre) {
  var text = pre.textContent;
  var lang = text.trim().startsWith('{') ? 'json' : 'python';
  pre.innerHTML = '<code class="language-' + lang + '">' + pre.innerHTML + '</code>';
});
hljs.highlightAll();
</script>
</body></html>"""


LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auction Finder - Find Nonprofit Auction Events at Scale</title>
<meta name="description" content="Find nonprofit auction leads, charity gala contacts, and fundraising event data at scale. Get verified decision-maker emails for nonprofit auctions, golf tournaments, and sponsorship opportunities.">
<meta name="keywords" content="nonprofit auction leads, nonprofit gala leads, charity golf tournament sponsorship contacts, nonprofit event contact database, fundraising event lead list, nonprofit gala contact list, fundraising event leads, sponsorship opportunities nonprofit, silent auction events, charity auction database, nonprofit event leads, find charity galas, upcoming auction events, charity events near me, fundraising auction, golf auction, silent auction fundraising, upcoming auction, charity auctions near me, charity fundraising auctions, charity gala events, charity golf outing, charity golf tournaments, charity silent auction, corporate sponsorship nonprofit, auction finder">
<meta name="author" content="Auction Intel">
<meta property="og:title" content="Auction Finder - Find Nonprofit Auction Events at Scale">
<meta property="og:description" content="AI-powered nonprofit event discovery. Find verified auction leads, gala contacts, and fundraising event data with decision-maker emails.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://auctionintel.app">
<meta property="og:image" content="https://auctionintel.app/static/logo_light.png">
<link rel="icon" type="image/png" href="/static/favicon.png">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "Auction Intel",
  "url": "https://auctionintel.app",
  "logo": "https://auctionintel.app/static/logo_light.png",
  "description": "AI-powered nonprofit auction event discovery. Find verified auction leads, gala contacts, and fundraising event data with decision-maker emails.",
  "sameAs": []
}
</script>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "WebSite",
  "name": "Auction Finder by Auction Intel",
  "url": "https://auctionintel.app"
}
</script>
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
  .footer-links { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px 20px; margin-top: 24px; padding-top: 20px; border-top: 1px solid #1a1a1a; }
  .footer-links a { font-size: 12px; color: #525252; text-decoration: none; }
  .footer-links a:hover { color: #eab308; }
  .footer-copy { text-align: center; margin-top: 16px; font-size: 12px; color: #525252; }

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
    .footer-links { gap: 6px 16px; }
  }
</style>
</head>
<body>

<div class="topnav">
  <div class="logo">
    <img src="/static/logo_light.png" alt="Auction Finder">
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
  <h1><span class="gold">Find charity auction events.</span><br><span style="font-size:38px;">Verified. Exportable. Ready to contact.</span></h1>
  <p class="subtitle">Auction Finder's new Auction Finder Research Engine scans nonprofit websites to find upcoming fundraising events with a live auction, a silent auction, or both. Get high-quality leads with a verified event page link, the event date, event-level contacts (email/phone), the auction type, and more.</p>
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
  <div class="stat"><div class="num">$20</div><div class="lbl">Free Credit</div></div>
</div>

<!-- How It Works -->
<section class="alt" id="how-it-works">
  <div class="section-header">
    <span class="tag">How It Works</span>
    <h2>How it works</h2>
    <p>Stop manually researching one organization at a time.<br>Auction Finder runs research in batches and returns verified, outreach-ready leads.</p>
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
    <p>Spend less time researching and more time closing.<br>Auction Finder delivers verified opportunities at scale&mdash;with proof.</p>
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
    <div style="font-size:72px;font-weight:900;color:#8b5cf6;line-height:1;margin-bottom:4px;">$20</div>
    <div style="font-size:22px;font-weight:700;color:#f5f5f5;margin-bottom:24px;">in free credit&mdash;on&nbsp;us</div>
    <p style="font-size:15px;color:#a3a3a3;margin-bottom:6px;">Covers searches and results. No credit card required.</p>
    <p style="font-size:14px;color:#737373;margin-bottom:28px;">Enter your promo code at registration to activate your free trial.</p>
    <a href="/register" style="display:inline-block;padding:14px 36px;background:#8b5cf6;color:#fff;border-radius:10px;font-size:16px;font-weight:700;text-decoration:none;">Start Your Free Trial</a>
    <p style="font-size:12px;color:#525252;margin-top:16px;">$20 free credit &middot; No credit card required &middot; Covers searches + leads</p>
  </div>
</section>

<!-- Pricing -->
<section id="pricing">
  <div class="section-header">
    <span class="tag">Pricing</span>
    <h2>Simple, transparent pricing</h2>
    <p>All tiers require a verified event page link.<br>Wallet-based billing&mdash;no subscriptions, no commitments.</p>
  </div>
  <div class="pricing-grid" style="grid-template-columns:repeat(3,1fr);">
    <div class="price-card highlight">
      <h3>Decision Maker</h3>
      <div class="price-tag"><b>$1.75</b> / lead</div>
      <ul>
        <li>Named contact + verified email</li>
        <li>Event title &amp; date</li>
        <li>Verified event page link</li>
        <li>Email deliverability confirmed</li>
      </ul>
      <p style="font-size:12px;color:#737373;margin-top:8px;">Best for: Phone campaigns &amp; personalized outreach</p>
      <a href="/register" class="signup-btn">Get Started</a>
    </div>
    <div class="price-card std">
      <h3>Outreach Ready</h3>
      <div class="price-tag"><b>$1.25</b> / lead</div>
      <ul>
        <li>Verified email address</li>
        <li>Event title &amp; date</li>
        <li>Verified event page link</li>
        <li>Email deliverability confirmed</li>
      </ul>
      <p style="font-size:12px;color:#737373;margin-top:8px;">Best for: Email campaigns &amp; cold outreach</p>
      <a href="/register" class="signup-btn">Create Account</a>
    </div>
    <div class="price-card std">
      <h3>Event Verified</h3>
      <div class="price-tag"><b>$0.75</b> / lead</div>
      <ul>
        <li>Event title &amp; date</li>
        <li>Verified event page link</li>
        <li>No contact info</li>
      </ul>
      <p style="font-size:12px;color:#737373;margin-top:8px;">Best for: Building prospect lists &amp; DIY research</p>
      <a href="/register" class="signup-btn">Create Account</a>
    </div>
  </div>
  <p class="pricing-note"><strong>No event page link = no lead charge.</strong></p>
  <p class="pricing-note" style="margin-top:12px;">Research fee: <strong>$0.04</strong>/nonprofit ($0.03 at 10K+, $0.02 at 50K+) &mdash; charged whether or not a lead is found.</p>
  <p class="pricing-bonus">Bonus fields (no extra charge): mailing address + main phone (when available).</p>

  <!-- Tier Selection callout -->
  <div style="max-width:900px;margin:40px auto 0;background:#111;border:1px solid #1a1a1a;border-radius:16px;padding:32px;text-align:left;">
    <h3 style="font-size:20px;font-weight:700;margin-bottom:8px;">Choose Your Tiers Before Each Search</h3>
    <p style="font-size:15px;color:#a3a3a3;line-height:1.6;">Before every search, a popup lets you pick which lead tiers you want. Only pay for the tiers you select&mdash;unselected tiers are excluded from your results and never billed.</p>
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
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What types of events does Auction Finder find? <span class="arrow">+</span></div>
      <div class="faq-a">We find nonprofit fundraising events with an auction component: silent auctions, live auctions, galas with paddle raises, charity dinners with auction items, golf tournaments with silent auctions, art shows, casino nights, and more. If the event has bidding, we find it.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">How does the research engine verify that an event actually exists? <span class="arrow">+</span></div>
      <div class="faq-a">Our 3-phase research pipeline does not rely on search snippets alone. The research engine visits the actual event page, reads the full page text, extracts evidence quotes showing an auction component, and returns the direct event page link. Every billable lead must include a verified event page link&mdash;no link, no charge.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">What if a nonprofit doesn't have an upcoming auction? <span class="arrow">+</span></div>
      <div class="faq-a">You still pay the research fee ($0.04 per nonprofit) because the research engine performed the web research. However, you are not charged a lead fee when no auction was found. The research fee covers the compute cost regardless of outcome.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">How do I add funds to my account? <span class="arrow">+</span></div>
      <div class="faq-a">We use Stripe for secure payments. Go to the Wallet page, enter an amount between $50 and $9,999, and complete checkout. Your balance updates instantly. All funds are non-refundable but never expire.</div>
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
      <div class="faq-a">Leads are priced by use case, and all tiers require a verified event page link: <b>Decision Maker ($1.75)</b>: Named contact + verified email + event title/date + event page link. Email deliverability confirmed. Best for phone campaigns. <b>Outreach Ready ($1.25)</b>: Verified email + event title/date + event page link. Email deliverability confirmed. Best for email campaigns. <b>Event Verified ($0.75)</b>: Event title/date + event page link only. No contact info. Best for building prospect lists. No event page link = no lead charge. If an email fails deliverability verification, the lead is automatically downgraded to Event Verified. Bonus fields (no extra charge): mailing address + main phone (when available).</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">Is mailing address included? <span class="arrow">+</span></div>
      <div class="faq-a">Mailing address is included when available (no extra charge). Some nonprofits use PO Boxes or have incomplete public address data.</div>
    </div>
    <div class="faq-item">
      <div class="faq-q" onclick="this.parentElement.classList.toggle('open')">How does tier selection work? <span class="arrow">+</span></div>
      <div class="faq-a">Before each search, a popup lets you choose which lead tiers you want to pay for. Check one or more tiers&mdash;Decision Maker, Outreach Ready, or Event Verified. You're only charged lead fees for tiers you selected, and only those leads appear in your download. You're never charged for nonprofits with no events found.</div>
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
  <p>Join auction professionals who use Auction Finder to discover<br>nonprofit events before the competition.</p>
  <a href="/register" class="btn-primary">Create Your Free Account</a>
</div>

<div class="footer">
  <div class="footer-inner">
    <div class="footer-brand">
      <div class="footer-logo"><img src="/static/logo_dark.png" alt="Auction Finder" style="height:36px;"></div>
      <p>Event-driven sales intelligence with verified nonprofit fundraising leads. Timing precision that turns research into revenue.</p>
    </div>
    <div class="footer-contact">
      <div class="label">Contact</div>
      <a href="mailto:support@auctionintel.us">support@auctionintel.us</a>
      <a href="tel:3037194851">303-719-4851</a>
    </div>
  </div>
  <div class="footer-links">
    <a href="/terms">Terms of Service</a>
    <a href="/privacy-policy">Privacy Policy</a>
    <a href="/refund-policy">Refund Policy</a>
    <a href="/do-not-sell">Do Not Sell My Info</a>
    <a href="/contact">Contact / DMCA / Abuse</a>
    <a href="/blog">Blog</a>
  </div>
  <div class="footer-copy">&copy; 2026 Auction Finder. All rights reserved.</div>
</div>

</body>
</html>"""

# ─── Blog ─────────────────────────────────────────────────────────────────────

BLOG_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
"""

BLOG_STYLES = """
<link rel="icon" type="image/png" href="/static/favicon.png">
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config={theme:{extend:{colors:{brand:{50:'#fffdf2',100:'#fff8d1',200:'#fff1a7',300:'#fde86e',400:'#f6de45',500:'#eed12f',600:'#d4b71f',700:'#aa8f16',800:'#7e6912',900:'#58480e'},ink:'#101010',sand:'#d9d2c7',paper:'#f5f2ec',panel:'#f8f6f1'},boxShadow:{soft:'0 10px 40px rgba(0,0,0,0.08)',card:'0 14px 50px rgba(0,0,0,0.10)'},backgroundImage:{grid:'linear-gradient(to right, rgba(16,16,16,0.06) 1px, transparent 1px), linear-gradient(to bottom, rgba(16,16,16,0.06) 1px, transparent 1px)'}}}}
</script>
<link href="https://cdnjs.cloudflare.com/ajax/libs/flowbite/2.5.1/flowbite.min.css" rel="stylesheet" />
<style>
html{scroll-behavior:smooth}
body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.section-kicker{letter-spacing:.22em}
.prose h2{color:#101010;font-weight:900;font-size:1.5rem;margin-top:2rem;margin-bottom:.75rem}
.prose p{color:rgba(16,16,16,.78);line-height:1.8;margin-bottom:1rem}
.prose ul{list-style:disc;padding-left:1.5rem;margin-bottom:1rem}
.prose li{color:rgba(16,16,16,.78);margin-bottom:.35rem}
</style>
</head>
"""

BLOG_NAV = """
<body class="bg-sand text-ink antialiased">
<div class="fixed inset-0 -z-10 bg-sand"></div>
<div class="fixed inset-0 -z-10 bg-grid bg-[size:36px_36px] opacity-25"></div>
<header class="sticky top-0 z-50 border-b border-black/10 bg-paper/90 backdrop-blur-xl">
<nav class="mx-auto flex max-w-7xl items-center justify-between px-6 py-4 lg:px-8">
  <a href="/" class="flex items-center">
    <img src="/static/header_logo.png" alt="Auction Finder" class="h-10 w-auto">
  </a>
  <div class="hidden items-center gap-8 lg:flex">
    <a href="/blog" class="text-sm font-semibold text-black/80 transition hover:text-black">Blog</a>
  </div>
  <div class="hidden items-center gap-3 lg:flex">
    <a href="/login" class="rounded-none border border-black bg-white px-4 py-2 text-sm font-semibold text-black transition hover:bg-black hover:text-white">Log In</a>
    <a href="/register" class="rounded-none border border-black bg-brand-500 px-5 py-2.5 text-sm font-extrabold text-black transition hover:bg-brand-400">Start Free</a>
  </div>
</nav>
</header>
"""

BLOG_FOOTER = """
<footer class="border-t-2 border-brand-500 bg-ink mt-24">
<div class="mx-auto max-w-7xl px-6 py-10 lg:px-8">
  <div class="grid gap-10 lg:grid-cols-[1fr_auto]">
    <div>
      <a href="/"><img src="/static/logo_dark.png" alt="Auction Finder" class="h-8 w-auto"></a>
      <p class="mt-4 max-w-md text-sm leading-7 text-white/70">Event-driven sales intelligence for verified nonprofit fundraising leads — turning research into revenue with timing precision.</p>
    </div>
    <div class="grid grid-cols-2 gap-x-16 gap-y-4 text-sm font-semibold sm:grid-cols-2">
      <div class="space-y-3">
        <p class="text-xs font-black uppercase tracking-[0.15em] text-white/40">Product</p>
        <a href="/" class="block text-white/70 transition hover:text-brand-400">Home</a>
        <a href="/blog" class="block text-white/70 transition hover:text-brand-400">Blog</a>
      </div>
      <div class="space-y-3">
        <p class="text-xs font-black uppercase tracking-[0.15em] text-white/40">Legal</p>
        <a href="/terms" class="block text-white/70 transition hover:text-brand-400">Terms of Service</a>
        <a href="/privacy-policy" class="block text-white/70 transition hover:text-brand-400">Privacy Policy</a>
        <a href="/refund-policy" class="block text-white/70 transition hover:text-brand-400">Refund Policy</a>
      </div>
    </div>
  </div>
  <div class="mt-8 border-t border-white/10 pt-6 text-center text-xs text-white/50">&copy; 2026 Auction Finder. All rights reserved.</div>
</div>
</footer>
<script src="https://cdnjs.cloudflare.com/ajax/libs/flowbite/2.5.1/flowbite.min.js"></script>
</body>
</html>
"""

# ── Article data ─────────────────────────────────────────────────────────────

BLOG_ARTICLES = [
    {
        "slug": "charity-golf-outing",
        "title": "Charity Golf Tournament Leads: How to Find High-Value Fundraising Events",
        "seo_title": "Charity Golf Tournament Leads: Find Nonprofit Golf Fundraisers",
        "summary": "A practical guide to discovering charity golf tournaments, connecting with event organizers, and securing auction and sponsorship opportunities.",
        "date": "Mar 03, 2026",
        "author": "Auction Finder Team",
        "read_time": "7 min read",
        "category": "Golf Fundraisers",
        "hero": "/static/blog/golf_1.png",
        "thumb": "/static/blog/golf_2.png",
        "related": ["charity-gala-events", "charity-auctions-near-me", "charity-events-near-me"],
    },
    {
        "slug": "charity-gala-events",
        "title": "Nonprofit Gala Leads: How to Find Fundraising Galas and Event Planners",
        "seo_title": "Nonprofit Gala Leads: Find Fundraising Galas & Event Planners",
        "summary": "How to discover nonprofit gala events, connect with development directors and event planners, and participate in high-value charity auctions.",
        "date": "Feb 27, 2026",
        "author": "Blake Bridge",
        "read_time": "6 min read",
        "category": "Galas & Events",
        "hero": "/static/blog/gala_1.png",
        "thumb": "/static/blog/gala_2.png",
        "related": ["charity-golf-outing", "charity-fundraising-auctions", "charity-events-near-me"],
    },
    {
        "slug": "charity-events-near-me",
        "title": "Charity Banquet Events: Finding Fundraising Dinners and Donor Events",
        "seo_title": "Charity Banquet Events: Find Fundraising Dinners & Donor Events",
        "summary": "Discover charity banquet events and connect with nonprofit organizers hosting fundraising dinners, raffles, and donor recognition events.",
        "date": "Feb 21, 2026",
        "author": "Auction Finder Team",
        "read_time": "5 min read",
        "category": "Banquets & Dinners",
        "hero": "/static/blog/banquet_1.png",
        "thumb": "/static/blog/banquet_2.png",
        "related": ["charity-gala-events", "charity-fundraising-auctions", "charity-auctions-near-me"],
    },
    {
        "slug": "charity-fundraising-auctions",
        "title": "Benefit Fundraiser Leads: Finding High-Impact Charity Events",
        "seo_title": "Benefit Fundraiser Leads: Find High-Impact Charity Events",
        "summary": "How to locate benefit fundraiser events, from charity concerts to auctions and raffles, and connect with the nonprofit organizers running them.",
        "date": "Feb 18, 2026",
        "author": "Auction Finder Team",
        "read_time": "5 min read",
        "category": "Benefit Events",
        "hero": "/static/blog/benefit_1.png",
        "thumb": "/static/blog/benefit_2.png",
        "related": ["charity-events-near-me", "charity-auctions-near-me", "charity-golf-outing"],
    },
    {
        "slug": "charity-auctions-near-me",
        "title": "Silent Auction Event Leads: How to Find Fundraising Auctions",
        "seo_title": "Silent Auction Event Leads: Find Nonprofit Fundraising Auctions",
        "summary": "Find silent auction events hosted by nonprofits, connect with event organizers, and get your auction items in front of donors.",
        "date": "Feb 12, 2026",
        "author": "Blake Bridge",
        "read_time": "5 min read",
        "category": "Silent Auctions",
        "hero": "/static/blog/silent_1.png",
        "thumb": "/static/blog/silent_2.png",
        "related": ["charity-golf-outing", "charity-gala-events", "charity-fundraising-auctions"],
    },
]

BLOG_ARTICLE_BODIES = {
    "charity-golf-outing": """
<p>Charity golf tournaments are one of the most profitable fundraising events nonprofits host each year. Across the United States, thousands of nonprofit organizations host charity golf outings, golf classics, and fundraising tournaments to raise money for their missions.</p>
<p>For businesses that sell travel packages, sports memorabilia, sponsorship packages, or luxury auction items, these events represent a massive opportunity.</p>
<p>The challenge? Finding them.</p>
<p>Most charity golf tournaments are organized locally and announced months in advance &mdash; but they can be difficult to discover without the right tools.</p>
<p>This guide explains how to find charity golf tournament leads and connect with the decision-makers running these events.</p>

<h2>Why Charity Golf Tournaments Are Valuable Opportunities</h2>
<p>Golf fundraisers attract affluent donors and corporate sponsors. That makes them ideal events for businesses offering premium items or experiences.</p>
<p>Typical charity golf tournaments include:</p>
<ul>
<li>Live auctions</li>
<li>Silent auctions</li>
<li>Raffle packages</li>
<li>Travel prizes</li>
<li>Sponsorship packages</li>
<li>Donor appreciation dinners</li>
</ul>
<p>Because attendees are typically business owners and high-net-worth individuals, the items offered during these events often generate strong bids.</p>

<h2>Who Organizes Charity Golf Tournaments?</h2>
<p>These events are usually managed by:</p>
<ul>
<li>Nonprofit development directors</li>
<li>Fundraising coordinators</li>
<li>Event committees</li>
<li>Board members</li>
<li>Corporate sponsors</li>
</ul>
<p>These decision-makers control sponsorship packages, auction items, and vendor opportunities. Finding the right contact is the key to securing participation in the event.</p>

<h2>Where Charity Golf Tournaments Take Place</h2>
<p>Golf fundraising events are held nationwide and are especially common during spring and summer months. Common event types include:</p>
<ul>
<li>Charity golf outings</li>
<li>Nonprofit golf tournaments</li>
<li>Golf fundraising classics</li>
<li>Corporate charity tournaments</li>
<li>Benefit golf events</li>
</ul>
<p>Each one represents an opportunity to introduce auction items or sponsorship opportunities.</p>

<h2>How Businesses Use Charity Golf Tournament Leads</h2>
<p>Companies selling auction items or sponsorship opportunities typically use golf tournament leads to:</p>
<ul>
<li>Donate travel packages</li>
<li>Provide sports memorabilia</li>
<li>Sponsor holes or tournaments</li>
<li>Offer luxury experiences</li>
<li>Sell raffle prizes</li>
</ul>
<p>Because these events are planned months in advance, reaching out early dramatically improves your chances of participation.</p>

<h2>The Problem With Finding Golf Fundraisers</h2>
<p>Traditionally, finding charity golf tournaments required hours of manual searching: scanning nonprofit websites, searching event calendars, digging through social media, and checking community boards.</p>
<p>This process is slow and unreliable. Many events never appear in standard search results.</p>

<h2>The Faster Way to Find Charity Golf Tournament Leads</h2>
<p>Platforms like Auction Finder allow businesses to instantly search nonprofit organizations and discover upcoming fundraising events. With the right tools, you can:</p>
<ul>
<li>Search millions of nonprofits</li>
<li>Identify upcoming golf tournaments</li>
<li>Find auction and sponsorship opportunities</li>
<li>Export contact information for organizers</li>
</ul>
<p>Instead of spending hours searching online, you can locate opportunities in minutes.</p>

<h2>Start Finding Charity Golf Tournament Leads</h2>
<p>If your business sells travel packages, auction items, sponsorships, or experiences, charity golf tournaments can become a consistent source of new opportunities.</p>
<p>Instead of waiting to discover events by chance, you can proactively find them and reach out to the organizers directly.</p>
""",
    "charity-gala-events": """
<p>Nonprofit gala events are among the most important fundraising activities organizations host each year.</p>
<p>From black-tie charity galas to annual fundraising dinners, these events generate millions of dollars for nonprofit organizations through ticket sales, sponsorships, and auctions.</p>
<p>For businesses offering travel packages, memorabilia, sponsorships, or luxury experiences, nonprofit galas present an incredible opportunity.</p>
<p>But first, you need to find them.</p>

<h2>What Are Nonprofit Gala Events?</h2>
<p>A nonprofit gala is a formal fundraising event typically held annually by charitable organizations. These events often include:</p>
<ul>
<li>Silent auctions</li>
<li>Live auctions</li>
<li>Donor recognition</li>
<li>Sponsorship opportunities</li>
<li>Fundraising appeals</li>
</ul>
<p>Galas attract major donors and corporate sponsors, making them ideal venues for high-value auction items.</p>

<h2>Who Plans Nonprofit Galas?</h2>
<p>These events are typically managed by:</p>
<ul>
<li>Development directors</li>
<li>Fundraising managers</li>
<li>Nonprofit event planners</li>
<li>Gala committees</li>
<li>Board members</li>
</ul>
<p>These decision-makers determine which auction items, sponsors, and vendors participate in the event.</p>

<h2>Why Businesses Seek Nonprofit Gala Leads</h2>
<p>Businesses seek nonprofit gala leads to:</p>
<ul>
<li>Donate travel experiences</li>
<li>Contribute sports memorabilia</li>
<li>Sponsor fundraising events</li>
<li>Provide auction packages</li>
</ul>
<p>Because gala auctions often include premium experiences, these events can produce high returns for vendors.</p>

<h2>The Difficulty of Finding Gala Events</h2>
<p>Many nonprofit galas are announced locally or through limited channels. They may appear on nonprofit websites, event calendars, local news, or social media &mdash; but these announcements can be scattered across thousands of organizations.</p>
<p>Without a centralized source, finding these events becomes extremely time-consuming.</p>

<h2>How Auction Finder Simplifies Finding Gala Leads</h2>
<p>Auction Finder makes it possible to discover nonprofit fundraising events in minutes. Using the platform, businesses can:</p>
<ul>
<li>Search nonprofit databases</li>
<li>Identify upcoming gala events</li>
<li>Locate auction opportunities</li>
<li>Find contact information for organizers</li>
</ul>
<p>Instead of manually researching nonprofits, you can instantly identify events that match your target audience.</p>

<h2>Discover Nonprofit Gala Leads Today</h2>
<p>If your business participates in fundraising auctions or sponsorship opportunities, nonprofit galas represent one of the most profitable event categories.</p>
<p>By identifying gala events early, you can secure valuable exposure and generate new business opportunities.</p>
""",
    "charity-events-near-me": """
<p>Charity banquets are a cornerstone of nonprofit fundraising.</p>
<p>These formal dinner events bring together donors, sponsors, and community leaders to support charitable causes while raising funds through auctions, sponsorships, and raffles.</p>
<p>For companies offering auction items, experiences, or sponsorship opportunities, charity banquets represent an excellent opportunity to connect with nonprofits.</p>

<h2>What Is a Charity Banquet?</h2>
<p>A charity banquet is a fundraising dinner typically hosted by nonprofit organizations. These events often feature:</p>
<ul>
<li>Silent auctions</li>
<li>Raffles</li>
<li>Guest speakers</li>
<li>Fundraising appeals</li>
<li>Sponsorship recognition</li>
</ul>
<p>Banquets often attract major donors and corporate supporters, making them high-value fundraising opportunities.</p>

<h2>Who Organizes Charity Banquets?</h2>
<p>Charity banquets are usually organized by:</p>
<ul>
<li>Nonprofit development teams</li>
<li>Fundraising coordinators</li>
<li>Event planning committees</li>
<li>Board members</li>
</ul>
<p>These individuals oversee fundraising activities and decide which auction items or sponsors participate.</p>

<h2>Why Businesses Participate in Banquets</h2>
<p>Businesses participate in charity banquets to:</p>
<ul>
<li>Promote their brand</li>
<li>Support nonprofit missions</li>
<li>Generate exposure among donors</li>
<li>Offer auction items</li>
</ul>
<p>For companies selling travel packages or luxury experiences, banquets often provide ideal fundraising environments.</p>

<h2>Finding Charity Banquet Events</h2>
<p>Charity banquet events are often announced months in advance but can be difficult to locate. Many nonprofits publish event announcements on their own websites or newsletters, making discovery challenging.</p>

<h2>Discover Banquet Events Faster</h2>
<p>Using platforms like Auction Finder, businesses can identify nonprofit fundraising events across the country. Instead of manually researching organizations, you can quickly find charity banquet events and connect with the organizers.</p>
""",
    "charity-fundraising-auctions": """
<p>Benefit fundraisers are events designed to raise money for nonprofit causes through auctions, sponsorships, and donations.</p>
<p>These events can include:</p>
<ul>
<li>Benefit dinners</li>
<li>Fundraising galas</li>
<li>Charity concerts</li>
<li>Auctions and raffles</li>
</ul>
<p>For businesses offering auction items or sponsorship opportunities, benefit events provide consistent opportunities to participate in fundraising efforts.</p>

<h2>Why Benefit Events Matter</h2>
<p>Benefit fundraisers often attract donors, community leaders, and corporate sponsors. These attendees are typically engaged supporters of the organization and are willing to bid on auction items or support sponsorship packages.</p>

<h2>Finding Benefit Fundraiser Leads</h2>
<p>Businesses often struggle to locate benefit events because they are hosted by thousands of nonprofit organizations nationwide. Event announcements may be scattered across websites and local event calendars.</p>

<h2>Using Technology to Discover Benefit Events</h2>
<p>With platforms like Auction Finder, businesses can search nonprofit organizations and identify benefit fundraisers across the country. Instead of manual research, you can quickly identify fundraising opportunities and connect with event organizers.</p>
""",
    "charity-auctions-near-me": """
<p>Silent auctions are one of the most common fundraising activities used by nonprofit organizations.</p>
<p>These auctions allow attendees to bid on items throughout an event, generating funds for charitable causes.</p>
<p>Businesses that donate auction items or experiences often seek silent auction event leads to connect with nonprofits hosting fundraising events.</p>

<h2>What Is a Silent Auction?</h2>
<p>A silent auction allows attendees to place bids on items without a live auctioneer. These auctions typically feature:</p>
<ul>
<li>Travel packages</li>
<li>Sports memorabilia</li>
<li>Luxury experiences</li>
<li>Restaurant gift cards</li>
<li>Artwork and collectibles</li>
</ul>
<p>Silent auctions can generate significant revenue for nonprofit organizations.</p>

<h2>Who Runs Silent Auctions?</h2>
<p>Silent auctions are typically managed by:</p>
<ul>
<li>Nonprofit development directors</li>
<li>Fundraising coordinators</li>
<li>Event committees</li>
</ul>
<p>These individuals select auction items and manage vendor participation.</p>

<h2>How Businesses Use Silent Auction Leads</h2>
<p>Businesses use silent auction leads to:</p>
<ul>
<li>Donate items to nonprofits</li>
<li>Promote their brand to donors</li>
<li>Build relationships with nonprofit organizations</li>
</ul>

<h2>Finding Silent Auction Events</h2>
<p>Because silent auctions are hosted by thousands of nonprofits each year, locating them manually can be challenging.</p>
<p>Tools like Auction Finder simplify this process by allowing businesses to search nonprofit organizations and identify upcoming events quickly.</p>

<h2>Discover Silent Auction Opportunities</h2>
<p>If your business offers auction items or sponsorship opportunities, silent auctions represent a powerful way to connect with nonprofit organizations and donors.</p>
<p>Auction Finder makes it possible to find these events quickly and efficiently.</p>
""",
}


def _blog_card_html(article):
    return f'''<article class="group overflow-hidden border-2 border-black bg-white shadow-soft transition hover:-translate-y-1 hover:shadow-card">
  <a href="/{article['slug']}" class="block">
    <img src="{article['thumb']}" alt="{article['title']}" class="h-[250px] w-full border-b-2 border-black object-cover"/>
  </a>
  <div class="p-6">
    <div class="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs font-bold uppercase tracking-[0.16em] text-black/55">
      <span>{article['date']}</span><span>&bull;</span><span>By {article['author']}</span>
    </div>
    <h3 class="mt-4 text-2xl font-black leading-tight"><a href="/{article['slug']}" class="transition group-hover:text-black/70">{article['title']}</a></h3>
    <p class="mt-4 text-base leading-7 text-black/72">{article['summary']}</p>
    <a href="/{article['slug']}" class="mt-5 inline-flex items-center border border-black bg-brand-500 px-4 py-2 text-sm font-black text-black transition hover:bg-brand-400">Read Article</a>
  </div>
</article>'''


def _blog_related_card_html(article):
    return f'''<article class="overflow-hidden border-2 border-black bg-white shadow-soft">
  <a href="/{article['slug']}"><img src="{article['thumb']}" alt="{article['title']}" class="h-[250px] w-full border-b-2 border-black object-cover"/></a>
  <div class="p-6">
    <div class="text-xs font-bold uppercase tracking-[0.16em] text-black/55">{article['date']} &bull; {article['author']}</div>
    <h3 class="mt-4 text-xl font-black leading-tight"><a href="/{article['slug']}" class="hover:text-black/70 transition">{article['title']}</a></h3>
    <p class="mt-3 text-sm leading-7 text-black/72">{article['summary']}</p>
  </div>
</article>'''


def _blog_article_page(article):
    body = BLOG_ARTICLE_BODIES[article["slug"]]
    by_slug = {a["slug"]: a for a in BLOG_ARTICLES}
    related_cards = "\n".join(_blog_related_card_html(by_slug[s]) for s in article["related"] if s in by_slug)

    return (BLOG_HEAD
        + f'<title>{article["seo_title"]} | Auction Finder</title>\n'
        + f'<meta name="description" content="{article["summary"]}">\n'
        + f'<meta name="keywords" content="{article["seo_title"]}, nonprofit auction leads, charity fundraising events, {article["category"]}, charity events near me, fundraising auction, golf auction, silent auction fundraising, upcoming auction, charity auctions near me, charity fundraising auctions, charity gala events, charity golf outing, charity golf tournaments, charity silent auction, corporate sponsorship nonprofit, auction finder">\n'
        + f'<meta name="author" content="{article["author"]}">\n'
        + f'<meta property="og:title" content="{article["seo_title"]} | Auction Finder">\n'
        + f'<meta property="og:description" content="{article["summary"]}">\n'
        + f'<meta property="og:type" content="article">\n'
        + f'<meta property="og:url" content="https://auctionintel.app/{article["slug"]}">\n'
        + f'<meta property="og:image" content="https://auctionintel.app{article["hero"]}">\n'
        + f'<link rel="canonical" href="https://auctionintel.app/{article["slug"]}">\n'
        + f'''<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "{article["seo_title"]}",
  "description": "{article["summary"]}",
  "image": "https://auctionintel.app{article["hero"]}",
  "author": {{"@type": "Person", "name": "{article["author"]}"}},
  "publisher": {{
    "@type": "Organization",
    "name": "Auction Intel",
    "logo": {{"@type": "ImageObject", "url": "https://auctionintel.app/static/logo_light.png"}}
  }},
  "datePublished": "{article["date"]}",
  "url": "https://auctionintel.app/{article["slug"]}",
  "mainEntityOfPage": {{"@type": "WebPage", "@id": "https://auctionintel.app/{article["slug"]}"}}
}}
</script>
'''
        + BLOG_STYLES + BLOG_NAV
        + f'''
<section class="py-24">
  <div class="mx-auto max-w-5xl px-6 lg:px-8">
    <div class="border-2 border-black bg-white p-8 shadow-card sm:p-12">
      <div class="max-w-3xl">
        <p class="section-kicker text-sm font-black text-black/60">{article["category"].upper()}</p>
        <h1 class="mt-4 text-4xl font-black tracking-tight sm:text-5xl">{article["title"]}</h1>
        <div class="mt-6 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm font-semibold text-black/65">
          <span>Published {article["date"]}</span>
          <span>&bull;</span>
          <span>By {article["author"]}</span>
          <span>&bull;</span>
          <span>{article["read_time"]}</span>
          <span>&bull;</span>
          <span>Category: {article["category"]}</span>
        </div>
      </div>
      <div class="mt-10 flex justify-center">
        <img src="{article["hero"]}" alt="{article["title"]}" class="h-auto w-full max-w-[675px] border-2 border-black object-cover"/>
      </div>
      <article class="prose prose-lg mt-12 max-w-none prose-headings:font-black prose-headings:text-black prose-p:text-black/78 prose-a:text-black prose-strong:text-black">
        {body}
        <div class="mt-10 border-2 border-black bg-brand-50 p-8 text-center">
          <p class="text-xl font-black text-black">Ready to find {article["category"].lower()} leads?</p>
          <p class="mt-3 text-base text-black/70">Start discovering nonprofit fundraising events with Auction Finder.</p>
          <a href="/register" class="mt-5 inline-flex items-center border-2 border-black bg-brand-500 px-8 py-3 text-sm font-black text-black transition hover:bg-brand-400">Start Free &rarr;</a>
        </div>
      </article>
    </div>

    <div class="mt-10">
      <div class="flex items-end justify-between gap-4">
        <div>
          <p class="section-kicker text-sm font-black text-black/60">MORE ARTICLES</p>
          <h2 class="mt-3 text-3xl font-black tracking-tight">Keep reading</h2>
        </div>
        <a href="/blog" class="hidden border border-black bg-white px-4 py-2 text-sm font-black text-black transition hover:bg-black hover:text-white sm:inline-flex">View All Posts</a>
      </div>
      <div class="mt-8 grid gap-8 md:grid-cols-3">
        {related_cards}
      </div>
    </div>
  </div>
</section>
'''
        + BLOG_FOOTER)


def _blog_index_page():
    cards = "\n".join(_blog_card_html(a) for a in BLOG_ARTICLES)
    return (BLOG_HEAD
        + '<title>Blog | Auction Finder</title>\n'
        + '<meta name="description" content="Insights and guides for nonprofit auction prospecting, charity golf tournaments, galas, banquets, benefit fundraisers, and silent auctions.">\n'
        + '<meta name="keywords" content="nonprofit auction blog, charity event guides, fundraising auction tips, nonprofit gala prospecting, golf tournament leads, silent auction strategies, benefit fundraiser outreach, charity events near me, fundraising auction, golf auction, silent auction fundraising, upcoming auction, charity auctions near me, charity fundraising auctions, charity gala events, charity golf outing, charity golf tournaments, charity silent auction, corporate sponsorship nonprofit, auction finder">\n'
        + '<meta name="author" content="Auction Intel">\n'
        + '<meta property="og:title" content="Blog | Auction Finder">\n'
        + '<meta property="og:description" content="Insights and guides for nonprofit auction prospecting, charity golf tournaments, galas, banquets, benefit fundraisers, and silent auctions.">\n'
        + '<meta property="og:type" content="website">\n'
        + '<meta property="og:url" content="https://auctionintel.app/blog">\n'
        + '<link rel="canonical" href="https://auctionintel.app/blog">\n'
        + '''<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Blog",
  "name": "Auction Finder Blog",
  "url": "https://auctionintel.app/blog",
  "description": "Insights and guides for nonprofit auction prospecting, charity golf tournaments, galas, banquets, benefit fundraisers, and silent auctions.",
  "publisher": {
    "@type": "Organization",
    "name": "Auction Intel",
    "url": "https://auctionintel.app",
    "logo": {"@type": "ImageObject", "url": "https://auctionintel.app/static/logo_light.png"}
  }
}
</script>
'''
        + BLOG_STYLES + BLOG_NAV
        + f'''
<section class="border-t-2 border-black bg-paper py-24">
  <div class="mx-auto max-w-7xl px-6 lg:px-8">
    <div class="max-w-3xl">
      <p class="section-kicker text-sm font-black text-black/60">BLOG</p>
      <h1 class="mt-4 text-4xl font-black tracking-tight sm:text-5xl">Insights for auction outreach teams.</h1>
      <p class="mt-5 max-w-2xl text-lg leading-8 text-black/70">Guides, strategies, and industry knowledge to help you find nonprofit fundraising events and connect with the right decision-makers.</p>
    </div>
    <div class="mt-14 grid gap-8 md:grid-cols-2 xl:grid-cols-3">
      {cards}
    </div>
  </div>
</section>
'''
        + BLOG_FOOTER)


@app.route("/blog")
def blog_index():
    return _blog_index_page()

@app.route("/charity-golf-tournament-leads")
def redirect_golf():
    return redirect("/charity-golf-outing", code=301)

@app.route("/nonprofit-gala-leads")
def redirect_gala():
    return redirect("/charity-gala-events", code=301)

@app.route("/charity-banquet-events")
def redirect_banquet():
    return redirect("/charity-events-near-me", code=301)

@app.route("/benefit-fundraiser-leads")
def redirect_benefit():
    return redirect("/charity-fundraising-auctions", code=301)

@app.route("/silent-auction-event-leads")
def redirect_silent():
    return redirect("/charity-auctions-near-me", code=301)

@app.route("/charity-golf-outing")
def blog_golf():
    return _blog_article_page(BLOG_ARTICLES[0])

@app.route("/charity-gala-events")
def blog_gala():
    return _blog_article_page(BLOG_ARTICLES[1])

@app.route("/charity-events-near-me")
def blog_banquet():
    return _blog_article_page(BLOG_ARTICLES[2])

@app.route("/charity-fundraising-auctions")
def blog_benefit():
    return _blog_article_page(BLOG_ARTICLES[3])

@app.route("/charity-auctions-near-me")
def blog_silent():
    return _blog_article_page(BLOG_ARTICLES[4])

@app.errorhandler(404)
def page_not_found(e):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Page Not Found - Auction Finder</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #000; color: #f5f5f5; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 40px 20px; }
  .logo { margin-bottom: 48px; }
  .logo img { height: 48px; }
  .code { font-size: 120px; font-weight: 900; color: #eab308; line-height: 1; letter-spacing: -4px; }
  .message { font-size: 22px; font-weight: 600; color: #d4d4d4; margin-top: 16px; }
  .sub { font-size: 15px; color: #737373; margin-top: 12px; max-width: 420px; line-height: 1.6; }
  .buttons { margin-top: 40px; display: flex; gap: 16px; flex-wrap: wrap; justify-content: center; }
  .btn { padding: 14px 32px; border-radius: 10px; font-size: 15px; font-weight: 600; text-decoration: none; transition: all 0.2s; }
  .btn-primary { background: #ffd900; color: #000; }
  .btn-primary:hover { background: #eab308; transform: translateY(-1px); }
  .btn-secondary { border: 1px solid #333; color: #d4d4d4; }
  .btn-secondary:hover { border-color: #eab308; color: #eab308; }
</style>
</head>
<body>
  <div class="logo"><a href="/"><img src="/static/logo_dark.png" alt="Auction Finder"></a></div>
  <div class="code">404</div>
  <p class="message">This page doesn't exist.</p>
  <p class="sub">The page you're looking for may have been moved or no longer exists. Let's get you back on track.</p>
  <div class="buttons">
    <a href="/" class="btn btn-primary">Back to Home</a>
    <a href="/blog" class="btn btn-secondary">Read the Blog</a>
    <a href="/register" class="btn btn-secondary">Start Free</a>
  </div>
</body>
</html>""", 404

@app.route("/robots.txt")
def robots_txt():
    from flask import Response
    txt = """User-agent: *
Allow: /
Allow: /blog
Allow: /charity-golf-outing
Allow: /charity-gala-events
Allow: /charity-events-near-me
Allow: /charity-fundraising-auctions
Allow: /charity-auctions-near-me
Allow: /register
Allow: /login
Allow: /terms
Allow: /privacy-policy
Allow: /refund-policy
Disallow: /database
Disallow: /results
Disallow: /api/
Disallow: /admin
Disallow: /wallet
Disallow: /settings
Sitemap: https://auctionintel.app/sitemap.xml
"""
    return Response(txt, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    from flask import Response
    pages = [
        {"loc": "https://auctionintel.app/", "priority": "1.0"},
        {"loc": "https://auctionintel.app/blog", "priority": "0.8"},
        {"loc": "https://auctionintel.app/login", "priority": "0.5"},
        {"loc": "https://auctionintel.app/register", "priority": "0.6"},
        {"loc": "https://auctionintel.app/terms", "priority": "0.3"},
        {"loc": "https://auctionintel.app/privacy-policy", "priority": "0.3"},
        {"loc": "https://auctionintel.app/refund-policy", "priority": "0.3"},
    ]
    for article in BLOG_ARTICLES:
        pages.append({"loc": f'https://auctionintel.app/{article["slug"]}', "priority": "0.7"})
    urls = ""
    for p in pages:
        urls += f'  <url>\n    <loc>{p["loc"]}</loc>\n    <priority>{p["priority"]}</priority>\n  </url>\n'
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}</urlset>'''
    return Response(xml, mimetype="application/xml")

# ─── Run ──────────────────────────────────────────────────────────────────────

# ─── App Initialization (runs under both gunicorn and direct execution) ──────

if not os.environ.get("POE_API_KEY"):
    print("WARNING: POE_API_KEY environment variable is not set.", file=sys.stderr)

init_db()
print("Database initialized.", file=sys.stderr)

cleanup_stale_running_jobs()
cleanup_expired_jobs()
flush_uncertain_cache()
cleanup_expired_cache()
cleanup_old_job_results()


# ─── REST API v1 ─────────────────────────────────────────────────────────────

@app.route("/api/v1/search", methods=["POST"])
@api_auth
def api_v1_search():
    """Submit domains and start a research job. Returns job_id."""
    user_id = request._api_user_id
    is_admin = request._api_is_admin
    is_trial = request._api_is_trial

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    raw_domains = data.get("domains", [])
    if isinstance(raw_domains, str):
        raw_domains = [d.strip() for d in raw_domains.replace(",", "\n").split("\n") if d.strip()]

    nonprofits = parse_input("\n".join(raw_domains))
    if not nonprofits:
        return jsonify({"error": "No valid domains provided"}), 400

    valid_tiers = {"decision_maker", "outreach_ready", "event_verified"}
    selected_tiers = [t for t in data.get("selected_tiers", list(valid_tiers)) if t in valid_tiers]
    if not selected_tiers:
        selected_tiers = list(valid_tiers)

    # Balance check (skip for admin)
    if not is_admin:
        balance = get_balance(user_id)
        count = len(nonprofits)
        fee_per = get_research_fee_cents(count)
        ESTIMATED_HIT_RATE = 0.55
        ESTIMATED_AVG_LEAD_CENTS = 125
        research_cost = count * fee_per
        estimated_lead_cost = int(count * ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
        estimated_total = research_cost + estimated_lead_cost
        if balance < estimated_total:
            cost_per_np = fee_per + int(ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
            max_affordable = max(1, balance // cost_per_np) if cost_per_np > 0 else count
            return jsonify({
                "error": f"Insufficient balance (${balance/100:.2f}). Estimated cost: ${estimated_total/100:.2f}. Can afford ~{max_affordable} domains.",
                "affordable_count": max_affordable,
                "balance_cents": balance,
                "estimated_cost_cents": estimated_total,
            }), 402

    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
    progress_q = EventQueue()

    jobs[job_id] = {
        "status": "running",
        "nonprofits": nonprofits,
        "progress_queue": progress_q,
        "results": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "total": len(nonprofits),
    }

    create_search_job(user_id, job_id, len(nonprofits))

    _user_email = getattr(request, "_api_email", "")
    thread = threading.Thread(
        target=_job_worker,
        args=(nonprofits, job_id, progress_q),
        kwargs={"user_id": user_id, "is_admin": is_admin, "is_trial": is_trial, "selected_tiers": selected_tiers, "user_email": _user_email},
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "total_domains": len(nonprofits),
        "status": "running",
    })


@app.route("/api/v1/status/<job_id>")
@api_auth
def api_v1_status(job_id):
    """Get job progress: processed/total, found count, ETA, balance."""
    user_id = request._api_user_id
    is_admin = request._api_is_admin

    job = jobs.get(job_id)
    if not job:
        # Check DB for completed/failed jobs
        db_job = get_search_job(job_id)
        if db_job:
            resp = {
                "job_id": job_id,
                "status": db_job["status"],
                "total": db_job["nonprofit_count"] or 0,
                "processed": db_job["nonprofit_count"] or 0,
                "found": db_job["found_count"] or 0,
            }
            if not is_admin:
                resp["balance_cents"] = get_balance(user_id)
            return jsonify(resp)
        return jsonify({"error": "Job not found"}), 404

    # Count progress from event queue
    events = job["progress_queue"].events
    processed = sum(1 for e in events if e and e.get("type") == "result")
    found = sum(1 for e in events if e and e.get("type") == "result" and e.get("status") in ("found", "3rdpty_found"))
    decision_makers = sum(1 for e in events if e and e.get("type") == "result" and e.get("tier") == "decision_maker")
    total = len(job.get("nonprofits", []))

    # Find latest ETA
    eta_remaining = None
    for e in reversed(events):
        if e and e.get("type") == "eta":
            eta_remaining = e.get("remaining")
            break

    resp = {
        "job_id": job_id,
        "status": job["status"],
        "total": total,
        "processed": processed,
        "found": found,
        "decision_makers": decision_makers,
        "eta_seconds": eta_remaining,
    }
    if not is_admin:
        resp["balance_cents"] = get_balance(user_id)

    if job["status"] == "complete" and job.get("results"):
        resp["summary"] = job["results"].get("summary", {})

    return jsonify(resp)


@app.route("/api/v1/results/<job_id>")
@api_auth
def api_v1_results(job_id):
    """Download results in csv, json, or xlsx format."""
    fmt = request.args.get("format", "csv")
    if fmt not in ("csv", "json", "xlsx"):
        return jsonify({"error": "Invalid format. Use csv, json, or xlsx."}), 400

    # Try in-memory job first
    job = jobs.get(job_id)
    if job and job["status"] != "complete":
        # Job still running — return partial results from job_results table
        results = get_completed_results(job_id)
        if not results:
            return jsonify({"error": "Job still running, no results yet"}), 202

    # Try disk, then DB
    filepath = os.path.join(RESULTS_DIR, f"{job_id}.{fmt}")
    if os.path.exists(filepath):
        return send_file(
            filepath,
            as_attachment=True,
            download_name=f"auction_results_{job_id}.{fmt}",
        )

    content = get_result_file(job_id, fmt)
    if content is None:
        return jsonify({"error": "Results not found"}), 404

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


@app.route("/api/v1/stop/<job_id>", methods=["POST"])
@api_auth
def api_v1_stop(job_id):
    """Stop a running job."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found or already finished"}), 404
    job = jobs[job_id]
    if job.get("status") != "running":
        return jsonify({"error": "Job not running"}), 400
    job["stop_requested"] = True
    return jsonify({"ok": True, "message": "Stop requested. Job will finish current domain and halt."})


@app.route("/api/v1/resume/<job_id>", methods=["POST"])
@api_auth
def api_v1_resume(job_id):
    """Resume an interrupted job. Creates a new child job for remaining domains."""
    user_id = request._api_user_id
    is_admin = request._api_is_admin
    is_trial = request._api_is_trial

    # Load original domains
    original_domains = get_job_input_domains(job_id)
    if not original_domains:
        return jsonify({"error": "Cannot resume: original domain list not found for this job."}), 404

    # Load completed domains
    completed = get_completed_domains(job_id)
    remaining = [d for d in original_domains if d not in completed]

    if not remaining:
        return jsonify({"error": "Nothing to resume — all domains already processed.", "completed": len(completed)}), 200

    # Check for selected_tiers from request body
    data = request.get_json(silent=True) or {}
    valid_tiers = {"decision_maker", "outreach_ready", "event_verified"}
    selected_tiers = [t for t in data.get("selected_tiers", list(valid_tiers)) if t in valid_tiers]
    if not selected_tiers:
        selected_tiers = list(valid_tiers)

    # Balance check (skip for admin)
    if not is_admin:
        balance = get_balance(user_id)
        count = len(remaining)
        fee_per = get_research_fee_cents(count)
        ESTIMATED_HIT_RATE = 0.55
        ESTIMATED_AVG_LEAD_CENTS = 125
        estimated_total = count * fee_per + int(count * ESTIMATED_HIT_RATE * ESTIMATED_AVG_LEAD_CENTS)
        if balance < estimated_total:
            return jsonify({
                "error": f"Insufficient balance (${balance/100:.2f}) for {count} remaining domains.",
                "remaining_count": count,
                "balance_cents": balance,
            }), 402

    # Create new child job
    new_job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
    progress_q = EventQueue()

    jobs[new_job_id] = {
        "status": "running",
        "nonprofits": remaining,
        "progress_queue": progress_q,
        "results": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "resumed_from": job_id,
    }

    create_search_job(user_id, new_job_id, len(remaining))
    # Mark resumed_from in DB
    try:
        from db import _get_conn
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE search_jobs SET resumed_from = %s WHERE job_id = %s", (job_id, new_job_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[RESUME] Failed to set resumed_from: {e}", flush=True)

    _user_email = getattr(request, "_api_email", "")
    thread = threading.Thread(
        target=_job_worker,
        args=(remaining, new_job_id, progress_q),
        kwargs={"user_id": user_id, "is_admin": is_admin, "is_trial": is_trial, "selected_tiers": selected_tiers, "user_email": _user_email},
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": new_job_id,
        "parent_job_id": job_id,
        "completed_in_parent": len(completed),
        "remaining": len(remaining),
        "status": "running",
    })


# ─── API Key Management ─────────────────────────────────────────────────────

@app.route("/settings/api-keys")
@login_required
def api_keys_page():
    """Page to manage API keys."""
    user_id = session["user_id"]
    user = _current_user()
    keys = get_user_api_keys(user_id)

    key_rows = ""
    for k in keys:
        status = "Active" if k["is_active"] else "Revoked"
        status_class = "color: #10b981" if k["is_active"] else "color: #ef4444"
        revoke_btn = ""
        if k["is_active"]:
            revoke_btn = f'<form method="post" action="/settings/api-keys/revoke/{k["id"]}" style="display:inline"><button type="submit" style="background:#ef4444;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer">Revoke</button></form>'
        key_rows += f"""<tr>
            <td style="padding:8px;border-bottom:1px solid #374151">{html_escape(k.get("label","") or "—")}</td>
            <td style="padding:8px;border-bottom:1px solid #374151;font-family:monospace">{k["key_hash"]}</td>
            <td style="padding:8px;border-bottom:1px solid #374151"><span style="{status_class}">{status}</span></td>
            <td style="padding:8px;border-bottom:1px solid #374151">{k["created_at"]}</td>
            <td style="padding:8px;border-bottom:1px solid #374151">{revoke_btn}</td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><title>API Keys - Auction Finder</title>
    <style>body{{background:#000000;color:#e2e8f0;font-family:system-ui;margin:0;padding:20px}}
    .container{{max-width:800px;margin:0 auto}}
    h1{{color:#eab308}}
    table{{width:100%;border-collapse:collapse;margin:20px 0}}
    th{{text-align:left;padding:8px;border-bottom:2px solid #262626;color:#a3a3a3}}
    .btn{{background:#eab308;color:#000;border:none;padding:8px 20px;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600}}
    .btn:hover{{background:#facc15}}
    input[type=text]{{background:#171717;border:1px solid #262626;color:#e2e8f0;padding:8px 12px;border-radius:4px;width:200px}}
    .new-key-box{{background:#064e3b;border:1px solid #10b981;padding:16px;border-radius:8px;margin:16px 0;font-family:monospace;word-break:break-all}}
    a{{color:#eab308}}
    </style></head><body>
    <div class="container">
    <h1>API Keys</h1>
    <p><a href="/database">&larr; Back to Dashboard</a></p>
    <p>Use API keys to authenticate with the <code>/api/v1/</code> endpoints from scripts.</p>

    <form method="post" action="/settings/api-keys/create" style="margin:20px 0">
        <input type="text" name="label" placeholder="Key label (optional)">
        <button type="submit" class="btn">Generate New Key</button>
    </form>

    {"" if not session.get("_new_api_key") else f'<div class="new-key-box"><strong>New API Key (copy now — shown only once):</strong><br><br>{session.pop("_new_api_key")}</div>'}

    <table>
    <tr><th>Label</th><th>Key (hash)</th><th>Status</th><th>Created</th><th>Action</th></tr>
    {key_rows if key_rows else "<tr><td colspan='5' style='padding:8px;color:#94a3b8'>No API keys yet.</td></tr>"}
    </table>
    </div></body></html>"""
    return html


@app.route("/settings/api-keys/create", methods=["POST"])
@login_required
def api_keys_create():
    """Generate a new API key."""
    user_id = session["user_id"]
    label = request.form.get("label", "").strip()[:100]
    plaintext = create_api_key(user_id, label)
    session["_new_api_key"] = plaintext
    return redirect(url_for("api_keys_page"))


@app.route("/settings/api-keys/revoke/<int:key_id>", methods=["POST"])
@login_required
def api_keys_revoke(key_id):
    """Revoke an API key."""
    user_id = session["user_id"]
    revoke_api_key(key_id, user_id)
    return redirect(url_for("api_keys_page"))


# ─── API Documentation ────────────────────────────────────────────────────────

@app.route("/settings/api-docs")
@login_required
def api_docs_page():
    html = API_DOCS_HTML
    html = _inject_sidebar(html, "api-docs")
    html = _inject_nav_badge(html)
    html = html.replace("{{EMAIL}}", html_escape(session.get("email", "")))
    return html


# ─── Newsletter Signup ────────────────────────────────────────────────────────

@app.route("/newsletter", methods=["POST"])
def newsletter_submit():
    email = (request.form.get("email", "") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Please enter a valid email."}), 400
    try:
        path = os.path.join(os.path.dirname(__file__), "newsletter_signups.csv")
        is_new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if is_new:
                f.write("created_at,email\n")
            f.write(f"{datetime.now(timezone.utc).isoformat()},{email}\n")
        print(f"[NEWSLETTER] New signup: {email}", flush=True)
    except Exception as e:
        print(f"[NEWSLETTER ERROR] {e}", flush=True)
        return jsonify({"error": "Subscription failed. Please try again."}), 500
    return jsonify({"ok": True, "message": "Thanks — you are subscribed."})


# ─── Test Email Route (Admin Only) ────────────────────────────────────────────

@app.route("/api/test-emails", methods=["POST"])
@login_required
def test_emails_route():
    """Send all 20 email templates with dummy data to a specified address. Admin only."""
    user = _current_user()
    if not user or not user.get("is_admin"):
        return jsonify({"error": "Admin only"}), 403

    target = request.json.get("email", "blake@auctionintel.us") if request.is_json else "blake@auctionintel.us"
    sent = []
    errors = []

    test_calls = [
        ("01_welcome", lambda: emails.send_welcome(target, is_trial=True)),
        ("02_verify", lambda: emails.send_verification_email(target, "https://auctionintel.app/verify?token=TEST_TOKEN_123")),
        ("03_password_reset", lambda: emails.send_password_reset(target, "https://auctionintel.app/reset?token=RESET_TOKEN_456")),
        ("04_job_complete", lambda: emails.send_job_complete(target, "test1234-5678-abcd", 50, 12, 8, 1400)),
        ("05_funds_receipt", lambda: emails.send_funds_receipt(target, 5000, 7500, "Visa ending 4242", "pi_test_123", "Mar 01, 2026")),
        ("06_results_expiring", lambda: emails.send_results_expiring_7_days(target, "test1234-5678-abcd")),
        ("07_ticket_created", lambda: emails.send_ticket_created(42, "Test Support Ticket", target, "This is a test support ticket message.")),
        ("08_ticket_reply", lambda: emails.send_ticket_reply_to_user(target, 42, "Test Support Ticket", "Thanks for reaching out! We are looking into this.")),
        ("09_credit_exhausted", lambda: emails.send_credit_exhausted(target)),
        ("10_low_balance", lambda: emails.send_low_balance_warning(target, 350)),
        ("11_trial_expiring", lambda: emails.send_trial_expiring(target, 2, 800)),
        ("12_exclusive_lead", lambda: emails.send_exclusive_lead_confirmed(target, "Denver Children's Foundation", "Spring Gala & Auction 2026", 250)),
        ("13_refund", lambda: emails.send_refund_issued(target, 500, "Duplicate charge", 5500)),
        ("14_search_stopped", lambda: emails.send_search_stopped(target, "test1234-5678-abcd", 35, 100, 12, 700)),
        ("15_payment_failed", lambda: emails.send_payment_failed(target, 5000, "Card declined by issuer")),
        ("16_we_miss_you", lambda: emails.send_we_miss_you(target, 2500, "Jan 15, 2026")),
        ("17_drip_day1", lambda: emails.send_drip_day1_how_it_works(target)),
        ("18_drip_day3", lambda: emails.send_drip_day3_first_search(target)),
        ("19_drip_day5", lambda: emails.send_drip_day5_social_proof(target)),
        ("20_drip_day7", lambda: emails.send_drip_day7_trial_ended(target)),
    ]

    for name, fn in test_calls:
        try:
            fn()
            sent.append(name)
            print(f"[TEST EMAIL] Sent {name} to {target}", flush=True)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[TEST EMAIL ERROR] {name}: {e}", flush=True)

    return jsonify({"sent": len(sent), "errors": errors, "target": target})


# ─── Test Landing Pages ──────────────────────────────────────────────────────

@app.route("/landing-test")
def landing_test():
    return LANDING_TEST_HTML


@app.route("/landing-test-2")
def landing_test_2():
    """New editorial-style landing page."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auction_intel_landing_page.html")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[LANDING] Error loading landing-test-2: {e}", flush=True)
        return "Landing page not found", 404

LANDING_TEST_HTML = """<!doctype html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Auction Finder — Find nonprofit auction events</title>
  <meta name="description" content="Find nonprofit auction leads, charity gala contacts, and fundraising event data at scale. Get verified decision-maker emails for nonprofit auctions, golf tournaments, and sponsorship opportunities.">
  <meta name="keywords" content="nonprofit auction leads, nonprofit gala leads, charity golf tournament sponsorship contacts, nonprofit event contact database, fundraising event lead list, nonprofit gala contact list, fundraising event leads, sponsorship opportunities nonprofit, silent auction events, charity auction database, nonprofit event leads, find charity galas, upcoming auction events, charity events near me, fundraising auction, golf auction, silent auction fundraising, upcoming auction, charity auctions near me, charity fundraising auctions, charity gala events, charity golf outing, charity golf tournaments, charity silent auction, corporate sponsorship nonprofit, auction finder">
  <meta property="og:title" content="Auction Finder — Find nonprofit auction events">
  <meta property="og:description" content="AI-powered nonprofit event discovery. Find verified auction leads, gala contacts, and fundraising event data with decision-maker emails.">
  <meta property="og:type" content="website">
  <meta property="og:url" content="https://auctionintel.app">
  <link rel="icon" type="image/png" href="/static/favicon.png">

  <!-- Tailwind CDN -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              50:'#fffbea',100:'#fff3c4',200:'#ffe58a',300:'#ffe44d',400:'#ffd900',
              500:'#ffd900',600:'#d9b800',700:'#b49600',800:'#8f7600',900:'#6f5c00'
            }
          }
        }
      }
    }
  </script>

  <!-- Flowbite (collapse + accordion) -->
  <script src="https://cdn.jsdelivr.net/npm/flowbite@2.5.2/dist/flowbite.min.js"></script>

  <style>
    .ai-grid {
      background-image:
        radial-gradient(circle at 20% 10%, rgba(255,217,0,0.14), transparent 35%),
        radial-gradient(circle at 80% 20%, rgba(59,130,246,0.10), transparent 40%),
        radial-gradient(circle at 50% 80%, rgba(168,85,247,0.10), transparent 45%),
        linear-gradient(to bottom, #050505, #000000 35%, #000000);
    }
    .ai-card {
      background: rgba(10,10,10,0.75);
      border: 1px solid rgba(38,38,38,0.9);
      box-shadow: 0 12px 30px rgba(0,0,0,0.45);
      backdrop-filter: blur(10px);
    }
  </style>
</head>

<body class="ai-grid text-white">
  <!-- HEADER -->
  <header class="sticky top-0 z-50 border-b border-neutral-800/80 bg-black/70 backdrop-blur">
    <nav class="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 lg:px-6">
      <a href="/" class="flex items-center gap-3">
        <img src="/static/logo_light.png" alt="Auction Finder" class="h-9 w-auto">
      </a>

      <div class="hidden items-center gap-7 lg:flex">
        <a href="#how-it-works" class="text-sm text-neutral-300 hover:text-white">How It Works</a>
        <a href="#features" class="text-sm text-neutral-300 hover:text-white">Features</a>
        <a href="#pricing" class="text-sm text-neutral-300 hover:text-white">Pricing</a>
        <a href="#faq" class="text-sm text-neutral-300 hover:text-white">FAQ</a>
      </div>

      <div class="flex items-center gap-3">
        <a href="/login"
           class="hidden sm:inline-flex items-center justify-center rounded-lg border border-neutral-700 px-4 py-2 text-sm text-neutral-200 hover:border-brand-500 hover:text-brand-200">
          Log In
        </a>
        <a href="/register"
           class="inline-flex items-center justify-center rounded-lg bg-brand-500 px-4 py-2 text-sm font-semibold text-black hover:bg-brand-400">
          Start Free
        </a>

        <button data-collapse-toggle="mobile-nav" type="button"
                class="inline-flex items-center justify-center rounded-lg border border-neutral-800 p-2 text-neutral-300 hover:bg-neutral-900 hover:text-white lg:hidden"
                aria-controls="mobile-nav" aria-expanded="false">
          <span class="sr-only">Open menu</span>
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16"/>
          </svg>
        </button>
      </div>
    </nav>

    <div id="mobile-nav" class="hidden border-t border-neutral-800 bg-black/80">
      <div class="mx-auto max-w-7xl px-4 py-3 space-y-2">
        <a href="#how-it-works" class="block rounded-lg px-3 py-2 text-sm text-neutral-200 hover:bg-neutral-900">How It Works</a>
        <a href="#features" class="block rounded-lg px-3 py-2 text-sm text-neutral-200 hover:bg-neutral-900">Features</a>
        <a href="#pricing" class="block rounded-lg px-3 py-2 text-sm text-neutral-200 hover:bg-neutral-900">Pricing</a>
        <a href="#faq" class="block rounded-lg px-3 py-2 text-sm text-neutral-200 hover:bg-neutral-900">FAQ</a>
        <a href="/login" class="block rounded-lg px-3 py-2 text-sm text-neutral-200 hover:bg-neutral-900">Log In</a>
      </div>
    </div>
  </header>

  <!-- HERO -->
  <section class="mx-auto max-w-7xl px-4 pt-12 pb-10 lg:px-6 lg:pt-16">
    <div class="grid items-start gap-8 lg:grid-cols-12">
      <div class="lg:col-span-7">
        <h1 class="mt-2 text-4xl font-extrabold tracking-tight sm:text-5xl">
          Find nonprofit auction events.
        </h1>
        <p class="mt-2 text-xl font-semibold text-neutral-200">
          Verified. Exportable. Ready to contact.
        </p>

        <p class="mt-4 max-w-2xl text-base leading-relaxed text-neutral-300">
          Auction Finder's Auction Finder Research Engine scans nonprofit websites to find upcoming fundraising
          events with a live auction, a silent auction, or both. Get high-quality leads with a verified event page link,
          the event date, event-level contacts (email/phone), the auction type, and more.
        </p>

        <div class="mt-7 flex flex-col gap-3 sm:flex-row sm:items-center">
          <a href="/register"
             class="inline-flex items-center justify-center rounded-lg bg-brand-500 px-5 py-3 text-sm font-semibold text-black hover:bg-brand-400">
            Get Started Free
          </a>
          <a href="#how-it-works"
             class="inline-flex items-center justify-center rounded-lg border border-neutral-700 px-5 py-3 text-sm font-semibold text-neutral-200 hover:border-neutral-500">
            See How It Works
          </a>
        </div>

        <div class="mt-10 grid grid-cols-2 gap-4 border-t border-neutral-900 pt-6 sm:grid-cols-4 sm:max-w-2xl">
          <div>
            <div class="text-2xl font-extrabold">300K+</div>
            <div class="text-xs text-neutral-400">Nonprofits in database</div>
          </div>
          <div>
            <div class="text-2xl font-extrabold">Up to 1,000</div>
            <div class="text-xs text-neutral-400">Nonprofits per search</div>
          </div>
          <div>
            <div class="text-2xl font-extrabold">40+</div>
            <div class="text-xs text-neutral-400">Filters for targeting</div>
          </div>
          <div>
            <div class="text-2xl font-extrabold">$20</div>
            <div class="text-xs text-neutral-400">Free credit</div>
          </div>
        </div>
      </div>

      <!-- Right signup box (wired to /register) -->
      <div class="lg:col-span-5">
        <div class="ai-card rounded-2xl p-5 sm:p-6">
          <p class="text-sm font-semibold text-neutral-200">Create your free account</p>
          <p class="mt-1 text-xs text-neutral-400">No credit card required.</p>

          <form action="/register" method="POST" class="mt-5 space-y-3">
            <div>
              <label class="mb-1 block text-xs font-medium text-neutral-300">Company</label>
              <input name="company" type="text" required placeholder="Your company name"
                class="w-full rounded-lg border border-neutral-800 bg-black/60 px-3 py-2 text-sm text-white placeholder:text-neutral-600 focus:border-brand-500 focus:ring-0" />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-neutral-300">Work email</label>
              <input name="email" type="email" required placeholder="name@company.com"
                class="w-full rounded-lg border border-neutral-800 bg-black/60 px-3 py-2 text-sm text-white placeholder:text-neutral-600 focus:border-brand-500 focus:ring-0" />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-neutral-300">Phone</label>
              <input name="phone" type="tel" required placeholder="303-555-1234"
                class="w-full rounded-lg border border-neutral-800 bg-black/60 px-3 py-2 text-sm text-white placeholder:text-neutral-600 focus:border-brand-500 focus:ring-0" />
            </div>
            <div class="grid gap-3 sm:grid-cols-2">
              <div>
                <label class="mb-1 block text-xs font-medium text-neutral-300">Password</label>
                <input name="password" type="password" required placeholder="Min 6 characters"
                  class="w-full rounded-lg border border-neutral-800 bg-black/60 px-3 py-2 text-sm text-white placeholder:text-neutral-600 focus:border-brand-500 focus:ring-0" />
              </div>
              <div>
                <label class="mb-1 block text-xs font-medium text-neutral-300">Confirm</label>
                <input name="confirm" type="password" required placeholder="Repeat password"
                  class="w-full rounded-lg border border-neutral-800 bg-black/60 px-3 py-2 text-sm text-white placeholder:text-neutral-600 focus:border-brand-500 focus:ring-0" />
              </div>
            </div>

            <button type="submit"
              class="w-full rounded-lg bg-brand-500 px-4 py-2.5 text-sm font-semibold text-black hover:bg-brand-400">
              Create account
            </button>

            <p class="text-center text-xs text-neutral-500">
              Already have an account?
              <a href="/login" class="text-brand-300 hover:text-brand-200">Log in</a>
            </p>
          </form>
        </div>
      </div>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- FEATURES / HOW IT WORKS -->
  <section id="how-it-works" class="mx-auto max-w-7xl px-4 py-14 lg:px-6">
    <div class="grid gap-10 lg:grid-cols-12">
      <div class="lg:col-span-7">
        <h2 class="text-3xl font-extrabold tracking-tight">How it works</h2>
        <p class="mt-3 max-w-2xl text-neutral-300">
          Stop manually researching one organization at a time. Auction Finder runs research in batches and returns verified, outreach-ready leads.
        </p>
      </div>

      <div class="lg:col-span-5">
        <div class="ai-card rounded-2xl p-5">
          <p class="text-xs font-semibold uppercase tracking-wider text-neutral-400">HOW IT WORKS</p>
          <div class="mt-4 space-y-3">
            <div class="flex items-start gap-4 rounded-xl border border-neutral-800 bg-black/40 p-4">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-black">1</span>
              <div>
                <p class="text-sm font-semibold">Build your list</p>
                <p class="mt-1 text-xs text-neutral-400">Filter nonprofits by location, revenue range, and event category — or paste your own domains.</p>
              </div>
            </div>
            <div class="flex items-start gap-4 rounded-xl border border-neutral-800 bg-black/40 p-4">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-black">2</span>
              <div>
                <p class="text-sm font-semibold">Run Auction Finder</p>
                <p class="mt-1 text-xs text-neutral-400">We scan nonprofit websites, trusted event platforms, and the broader web to locate upcoming events with auction activity.</p>
              </div>
            </div>
            <div class="flex items-start gap-4 rounded-xl border border-neutral-800 bg-black/40 p-4">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-black">3</span>
              <div>
                <p class="text-sm font-semibold">Verify the event page</p>
                <p class="mt-1 text-xs text-neutral-400">Every billable lead must include a real event page URL plus supporting evidence pulled from that page.</p>
              </div>
            </div>
            <div class="flex items-start gap-4 rounded-xl border border-neutral-800 bg-black/40 p-4">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-black">4</span>
              <div>
                <p class="text-sm font-semibold">Export &amp; start outreach</p>
                <p class="mt-1 text-xs text-neutral-400">Download results as CSV / XLSX / JSON and start contacting the right people.</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- FEATURES2 -->
  <section id="features" class="mx-auto max-w-7xl px-4 py-14 lg:px-6">
    <div class="text-center">
      <p class="text-xs font-semibold uppercase tracking-wider text-brand-300">FEATURES</p>
      <h2 class="mt-2 text-3xl font-extrabold tracking-tight">Built to close more consignments</h2>
      <p class="mt-3 text-neutral-300">Spend less time researching and more time closing. Auction Finder delivers verified opportunities at scale — with proof.</p>
    </div>

    <div class="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
      <div class="ai-card rounded-2xl p-6">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10 text-brand-400">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>
        </div>
        <h3 class="mt-3 text-sm font-semibold">Deep Verification (not snippets)</h3>
        <p class="mt-2 text-xs leading-relaxed text-neutral-400">A multi-phase process checks the actual event page and pulls evidence text. If there is no verified event page URL it is not a billable lead.</p>
      </div>
      <div class="ai-card rounded-2xl p-6">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10 text-brand-400">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4"/></svg>
        </div>
        <h3 class="mt-3 text-sm font-semibold">Premium Nonprofit Database</h3>
        <p class="mt-2 text-xs leading-relaxed text-neutral-400">Search 300K+ nonprofits by state, revenue range, and event category — including gala, auction, golf, dinner, and 20+ more.</p>
      </div>
      <div class="ai-card rounded-2xl p-6">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10 text-brand-400">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        </div>
        <h3 class="mt-3 text-sm font-semibold">Batch Research at Scale</h3>
        <p class="mt-2 text-xs leading-relaxed text-neutral-400">Run up to 1,000 organizations per search and watch results stream in as each organization is processed.</p>
      </div>
      <div class="ai-card rounded-2xl p-6">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10 text-brand-400">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
        </div>
        <h3 class="mt-3 text-sm font-semibold">Rich Lead Records</h3>
        <p class="mt-2 text-xs leading-relaxed text-neutral-400">Each lead can include event title, date, event page URL, auction type, confidence score, contact name, email, phone, and supporting evidence.</p>
      </div>
      <div class="ai-card rounded-2xl p-6">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10 text-brand-400">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M9 19l3 3m0 0l3-3m-3 3V10"/></svg>
        </div>
        <h3 class="mt-3 text-sm font-semibold">Export Anywhere + 180-Day Storage</h3>
        <p class="mt-2 text-xs leading-relaxed text-neutral-400">Download CSV/JSON/XLSX anytime. Results stay available for 180 days for re-download.</p>
      </div>
      <div class="ai-card rounded-2xl p-6">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10 text-brand-400">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        </div>
        <h3 class="mt-3 text-sm font-semibold">Verified Tiers + Fair Billing</h3>
        <p class="mt-2 text-xs leading-relaxed text-neutral-400">You only pay for leads with a verified event page link. Pricing is tiered by completeness so you never overpay.</p>
      </div>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- CTA -->
  <section class="mx-auto max-w-7xl px-4 py-14 lg:px-6">
    <div class="grid items-center gap-10 lg:grid-cols-12">
      <div class="lg:col-span-7">
        <p class="text-xs font-semibold uppercase tracking-wider text-brand-300">FREE TRIAL</p>
        <h2 class="mt-2 text-3xl font-extrabold tracking-tight">$20 in free credit — on us</h2>
        <p class="mt-3 max-w-2xl text-neutral-300">Covers searches and results. No credit card required.</p>
        <a href="/register" class="mt-6 inline-flex items-center justify-center rounded-lg bg-brand-500 px-6 py-3 text-sm font-semibold text-black hover:bg-brand-400">
          Start Your Free Trial
        </a>
      </div>
      <div class="lg:col-span-5 flex items-center justify-center">
        <img src="/static/free-trial.png" alt="7 days free trial + $20 free credit" class="w-full max-w-md h-auto" />
      </div>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- PRICING -->
  <section id="pricing" class="mx-auto max-w-7xl px-4 py-14 lg:px-6">
    <div class="text-center">
      <p class="text-xs font-semibold uppercase tracking-wider text-brand-300">PRICING</p>
      <h2 class="mt-2 text-3xl font-extrabold tracking-tight">Simple, transparent pricing</h2>
      <p class="mt-3 text-neutral-300">All tiers require a verified event page link. Wallet-based billing — no subscriptions, no commitments.</p>
    </div>

    <div class="mt-10 grid gap-4 lg:grid-cols-3">
      <div class="ai-card rounded-2xl p-7">
        <p class="text-sm font-semibold text-neutral-200">Decision Maker</p>
        <div class="mt-3 flex items-end gap-2">
          <span class="text-4xl font-extrabold">$1.75</span>
          <span class="pb-1 text-sm text-neutral-400">/ lead</span>
        </div>
        <ul class="mt-5 space-y-3 text-sm text-neutral-300">
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Named contact + verified email</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Event title &amp; date</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Verified event page link</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Email deliverability confirmed</li>
        </ul>
        <a href="/register" class="mt-6 inline-flex w-full items-center justify-center rounded-lg bg-brand-500 px-5 py-2.5 text-sm font-semibold text-black hover:bg-brand-400">
          Create Account
        </a>
      </div>

      <div class="ai-card rounded-2xl p-7">
        <p class="text-sm font-semibold text-neutral-200">Outreach Ready</p>
        <div class="mt-3 flex items-end gap-2">
          <span class="text-4xl font-extrabold">$1.25</span>
          <span class="pb-1 text-sm text-neutral-400">/ lead</span>
        </div>
        <ul class="mt-5 space-y-3 text-sm text-neutral-300">
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Verified email address</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Event title &amp; date</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Verified event page link</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Email deliverability confirmed</li>
        </ul>
        <a href="/register" class="mt-6 inline-flex w-full items-center justify-center rounded-lg border border-neutral-700 px-5 py-2.5 text-sm font-semibold text-neutral-100 hover:border-neutral-500">
          Create Account
        </a>
      </div>

      <div class="ai-card rounded-2xl p-7">
        <p class="text-sm font-semibold text-neutral-200">Event Verified</p>
        <div class="mt-3 flex items-end gap-2">
          <span class="text-4xl font-extrabold">$0.75</span>
          <span class="pb-1 text-sm text-neutral-400">/ lead</span>
        </div>
        <ul class="mt-5 space-y-3 text-sm text-neutral-300">
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Event title &amp; date</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>Verified event page link</li>
          <li class="flex items-center gap-2"><svg class="h-4 w-4 shrink-0 text-neutral-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg><span class="text-neutral-500">No contact info</span></li>
        </ul>
        <a href="/register" class="mt-6 inline-flex w-full items-center justify-center rounded-lg border border-neutral-700 px-5 py-2.5 text-sm font-semibold text-neutral-100 hover:border-neutral-500">
          Create Account
        </a>
      </div>
    </div>

    <div class="mx-auto mt-8 max-w-4xl text-center text-sm text-neutral-400">
      <p><span class="font-semibold text-neutral-200">No event page link = no lead charge.</span></p>
      <p class="mt-1">Research fee: $0.04/nonprofit ($0.03 at 10K+, $0.02 at 50K+) — charged whether or not a lead is found.</p>
    </div>

    <div class="mx-auto mt-10 grid max-w-4xl gap-4">
      <div class="ai-card rounded-2xl p-6">
        <p class="text-sm font-semibold text-neutral-200">Choose Your Tiers Before Each Search</p>
        <p class="mt-2 text-sm text-neutral-400">Before every search, a popup lets you pick which lead tiers you want. Only pay for the tiers you select — unselected tiers are excluded from your results and never billed.</p>
      </div>
      <div class="ai-card rounded-2xl p-6">
        <p class="text-sm font-semibold text-neutral-200">Priority Lead Lock — $2.50</p>
        <p class="mt-2 text-sm text-neutral-400">Lock any event lead so it stops being sold to new customers going forward. For $2.50, the lead is pulled from future search results — giving you a head start over the competition. This is the total price and replaces the standard lead tier fee.</p>
      </div>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- FAQ -->
  <section id="faq" class="mx-auto max-w-4xl px-4 py-14 lg:px-6">
    <div class="text-center">
      <p class="text-xs font-semibold uppercase tracking-wider text-brand-300">FAQ</p>
      <h2 class="mt-2 text-3xl font-extrabold tracking-tight">Quick answers to common questions</h2>
      <p class="mt-3 text-neutral-300">Need more help? <a href="/support" class="text-brand-300 hover:text-brand-200 font-semibold">Open a support ticket</a>.</p>
    </div>

    <div class="mt-10" id="accordion-flush" data-accordion="collapse">
      <h3 id="faq-h-1"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-1" aria-expanded="false" aria-controls="faq-b-1"><span>What types of events does Auction Finder find?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-1" class="hidden" aria-labelledby="faq-h-1"><div class="pb-5 text-sm text-neutral-400">Live auction events, silent auction events, or events that include both.</div></div>

      <h3 id="faq-h-2"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-2" aria-expanded="false" aria-controls="faq-b-2"><span>How does the research engine verify that an event actually exists?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-2" class="hidden" aria-labelledby="faq-h-2"><div class="pb-5 text-sm text-neutral-400">Every billable lead must include a real event page URL plus supporting evidence pulled from that page.</div></div>

      <h3 id="faq-h-3"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-3" aria-expanded="false" aria-controls="faq-b-3"><span>What if a nonprofit does not have an upcoming auction?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-3" class="hidden" aria-labelledby="faq-h-3"><div class="pb-5 text-sm text-neutral-400">No verified event page link means no lead charge.</div></div>

      <h3 id="faq-h-4"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-4" aria-expanded="false" aria-controls="faq-b-4"><span>How do I add funds to my account?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-4" class="hidden" aria-labelledby="faq-h-4"><div class="pb-5 text-sm text-neutral-400">Top up your wallet from the Wallet page in your dashboard.</div></div>

      <h3 id="faq-h-5"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-5" aria-expanded="false" aria-controls="faq-b-5"><span>What is included in the premium nonprofit database?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-5" class="hidden" aria-labelledby="faq-h-5"><div class="pb-5 text-sm text-neutral-400">300K+ nonprofits with filters for state, revenue range, and event categories.</div></div>

      <h3 id="faq-h-6"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-6" aria-expanded="false" aria-controls="faq-b-6"><span>How long are results stored?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-6" class="hidden" aria-labelledby="faq-h-6"><div class="pb-5 text-sm text-neutral-400">Results remain available for 180 days for re-download.</div></div>

      <h3 id="faq-h-7"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-7" aria-expanded="false" aria-controls="faq-b-7"><span>Can I research nonprofits not in the database?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-7" class="hidden" aria-labelledby="faq-h-7"><div class="pb-5 text-sm text-neutral-400">Yes — paste your own domains and run research in batches.</div></div>

      <h3 id="faq-h-8"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-8" aria-expanded="false" aria-controls="faq-b-8"><span>What is the difference between lead tiers?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-8" class="hidden" aria-labelledby="faq-h-8"><div class="pb-5 text-sm text-neutral-400">Tiers are priced by completeness: event verified, outreach ready, decision maker.</div></div>

      <h3 id="faq-h-9"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-9" aria-expanded="false" aria-controls="faq-b-9"><span>Is mailing address included?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-9" class="hidden" aria-labelledby="faq-h-9"><div class="pb-5 text-sm text-neutral-400">When available from the source or trusted records, it can be included.</div></div>

      <h3 id="faq-h-10"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-10" aria-expanded="false" aria-controls="faq-b-10"><span>How does tier selection work?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-10" class="hidden" aria-labelledby="faq-h-10"><div class="pb-5 text-sm text-neutral-400">Before every search, pick the tiers you want — only selected tiers are billed.</div></div>

      <h3 id="faq-h-11"><button type="button" class="flex w-full items-center justify-between border-b border-neutral-900 py-5 text-left text-sm font-semibold text-neutral-200" data-accordion-target="#faq-b-11" aria-expanded="false" aria-controls="faq-b-11"><span>What is a Priority Lead Lock?</span><svg data-accordion-icon class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h3>
      <div id="faq-b-11" class="hidden" aria-labelledby="faq-h-11"><div class="pb-5 text-sm text-neutral-400">Lock any event lead so it stops being sold to new customers going forward. $2.50 replaces the standard tier fee.</div></div>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- NEWSLETTER -->
  <section id="newsletter" class="mx-auto max-w-4xl px-4 py-14 lg:px-6">
    <div class="ai-card rounded-2xl p-8 text-center">
      <h2 class="text-2xl font-extrabold">Sign up for our newsletter</h2>
      <p class="mt-2 text-sm text-neutral-300">Stay up to date with the roadmap progress, announcements and exclusive discounts.</p>

      <form id="newsletterForm" class="mx-auto mt-6 flex max-w-xl flex-col gap-3 sm:flex-row">
        <input id="newsletterEmail" name="email" type="email" required placeholder="Enter your email"
               class="w-full flex-1 rounded-lg border border-neutral-800 bg-black/60 px-4 py-3 text-sm text-white placeholder:text-neutral-600 focus:border-brand-500 focus:ring-0" />
        <button type="submit"
           class="inline-flex items-center justify-center rounded-lg bg-brand-500 px-6 py-3 text-sm font-semibold text-black hover:bg-brand-400">
          Subscribe
        </button>
      </form>

      <p id="newsletterMsg" class="mt-3 text-xs text-neutral-500"></p>
      <p class="mt-3 text-xs text-neutral-500">We care about the protection of your data. Read our <a class="text-brand-300 hover:text-brand-200" href="/privacy">Privacy Policy</a>.</p>
    </div>
  </section>

  <!-- SPACE -->
  <div class="mx-auto max-w-7xl px-4 lg:px-6"><div class="h-px w-full bg-neutral-900"></div></div>

  <!-- FOOTER -->
  <footer class="border-t border-neutral-900 bg-black/70">
    <div class="mx-auto max-w-7xl px-4 py-10 lg:px-6">
      <div class="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
        <div>
          <a href="/" class="flex items-center gap-3">
            <img src="/static/logo_dark.png" alt="Auction Finder" class="h-9 w-auto">
          </a>
          <p class="mt-3 max-w-sm text-sm text-neutral-400">Event-driven sales intelligence with verified nonprofit fundraising leads. Timing precision that turns research into revenue.</p>
        </div>

        <div class="grid grid-cols-2 gap-8 sm:grid-cols-3">
          <div>
            <p class="text-xs font-semibold uppercase tracking-wider text-neutral-500">Contact</p>
            <div class="mt-3 space-y-2">
              <a class="block text-sm text-neutral-300 hover:text-white" href="mailto:support@auctionintel.us">support@auctionintel.us</a>
              <span class="block text-sm text-neutral-400">303-719-4851</span>
            </div>
          </div>
          <div>
            <p class="text-xs font-semibold uppercase tracking-wider text-neutral-500">Legal</p>
            <div class="mt-3 space-y-2">
              <a class="block text-sm text-neutral-300 hover:text-white" href="/terms">Terms of Service</a>
              <a class="block text-sm text-neutral-300 hover:text-white" href="/privacy">Privacy Policy</a>
              <a class="block text-sm text-neutral-300 hover:text-white" href="/do-not-sell">Do Not Sell My Info</a>
              <a class="block text-sm text-neutral-300 hover:text-white" href="/contact">Contact / DMCA</a>
            </div>
          </div>
          <div>
            <p class="text-xs font-semibold uppercase tracking-wider text-neutral-500">Start</p>
            <div class="mt-3 space-y-2">
              <a class="block text-sm text-neutral-300 hover:text-white" href="/register">Create your free account</a>
              <a class="block text-sm text-neutral-300 hover:text-white" href="/login">Log in</a>
              <a class="block text-sm text-neutral-300 hover:text-white" href="/blog">Blog</a>
            </div>
          </div>
        </div>
      </div>

      <div class="mt-10 border-t border-neutral-900 pt-6">
        <p class="text-xs text-neutral-500">&copy; 2026 Auction Finder. All rights reserved.</p>
      </div>
    </div>
  </footer>

  <script>
    (function () {
      var form = document.getElementById('newsletterForm');
      var email = document.getElementById('newsletterEmail');
      var msg = document.getElementById('newsletterMsg');
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        msg.textContent = '';
        fetch('/newsletter', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'email=' + encodeURIComponent(email.value.trim())
        })
        .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, data: d }; }); })
        .then(function (r) {
          if (!r.ok) throw new Error(r.data.error || 'Subscription failed');
          msg.textContent = 'Thanks! You are subscribed.';
          msg.className = 'mt-3 text-xs text-green-400';
          email.value = '';
        })
        .catch(function (err) {
          msg.textContent = err.message || 'Subscription failed';
          msg.className = 'mt-3 text-xs text-red-400';
        });
      });
    })();
  </script>
</body>
</html>"""


# ─── Drip Campaign Scheduler ─────────────────────────────────────────────────

def _run_drip_campaign():
    """Check trial users and send drip emails based on days since signup.
    Schedule: day 1 = how it works, day 3 = first search nudge,
              day 5 = social proof, day 7 = trial ended."""
    DRIP_SCHEDULE = [
        (1, "drip_day1", emails.send_drip_day1_how_it_works),
        (3, "drip_day3", emails.send_drip_day3_first_search),
        (5, "drip_day5", emails.send_drip_day5_social_proof),
        (7, "drip_day7", emails.send_drip_day7_trial_ended),
    ]
    try:
        users = get_trial_users_for_drip()
        sent_count = 0
        for user in users:
            days = float(user.get("days_since_signup", 0))
            already_sent = get_drips_sent(user["id"])
            for trigger_day, drip_key, send_fn in DRIP_SCHEDULE:
                if days >= trigger_day and drip_key not in already_sent:
                    try:
                        send_fn(user["email"])
                        record_drip_sent(user["id"], drip_key)
                        sent_count += 1
                        print(f"[DRIP] Sent {drip_key} to {user['email']}", flush=True)
                    except Exception as e:
                        print(f"[DRIP] Error sending {drip_key} to {user['email']}: {e}", flush=True)
        if sent_count:
            print(f"[DRIP] Cycle complete: {sent_count} email(s) sent", flush=True)
    except Exception as e:
        print(f"[DRIP] Scheduler error: {e}", flush=True)


def _run_trial_expiring():
    """Send trial expiring email to users whose trial ends in 1-3 days."""
    try:
        users = get_expiring_trial_users()
        for user in users:
            days = float(user.get("days_since_signup", 0))
            days_left = max(1, 7 - int(days))
            already_sent = get_drips_sent(user["id"])
            if "trial_expiring" not in already_sent:
                try:
                    balance = get_balance(user["id"])
                    emails.send_trial_expiring(user["email"], days_left, balance)
                    record_drip_sent(user["id"], "trial_expiring")
                    print(f"[SCHEDULER] Sent trial_expiring to {user['email']} ({days_left} days left)", flush=True)
                except Exception as e:
                    print(f"[SCHEDULER] Error sending trial_expiring to {user['email']}: {e}", flush=True)
    except Exception as e:
        print(f"[SCHEDULER] Trial expiring check error: {e}", flush=True)


def _run_we_miss_you():
    """Send re-engagement email to users inactive 30+ days."""
    try:
        users = get_inactive_users(30)
        for user in users:
            already_sent = get_drips_sent(user["id"])
            if "we_miss_you" not in already_sent:
                try:
                    balance = get_balance(user["id"])
                    last_search = str(user.get("last_search_at", ""))
                    emails.send_we_miss_you(user["email"], balance, last_search)
                    record_drip_sent(user["id"], "we_miss_you")
                    print(f"[SCHEDULER] Sent we_miss_you to {user['email']}", flush=True)
                except Exception as e:
                    print(f"[SCHEDULER] Error sending we_miss_you to {user['email']}: {e}", flush=True)
    except Exception as e:
        print(f"[SCHEDULER] We miss you check error: {e}", flush=True)


def _drip_scheduler_loop():
    """Background loop — runs drip check every hour."""
    while True:
        time.sleep(3600)  # 1 hour
        _run_drip_campaign()
        _run_trial_expiring()
        _run_we_miss_you()


# Run once at startup, then hourly in background
_run_drip_campaign()
_drip_thread = threading.Thread(target=_drip_scheduler_loop, daemon=True)
_drip_thread.start()
print("Drip campaign scheduler started (hourly).", file=sys.stderr)

print(f"Stripe configured: {'Yes' if stripe.api_key else 'No (set STRIPE_SECRET_KEY)'}", file=sys.stderr)
print(f"Emailable configured: {'Yes (' + EMAILABLE_API_KEY[:8] + '...)' if EMAILABLE_API_KEY else 'No (set EMAILABLE_API_KEY)'}", file=sys.stderr)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"AUCTIONFINDER Web UI starting on http://localhost:{port}", file=sys.stderr)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
