"""
AUCTIONFINDER — Email Templates & Sending

Loads final HTML templates from final_email_template_series/ folder.
Dynamic values are swapped via .replace() on {placeholder} strings.
Sent via Resend API.
"""

import os
from datetime import datetime, timezone
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Auction Intel Admin <admin@auctionintel.app>")
DOMAIN = os.environ.get("APP_DOMAIN", "https://auctionintel.app")

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "final_email_template_series")

# Map logical names to actual filenames (some have typos/spaces)
_FILENAMES = {
    "01_welcome": "01_welcome_email.html",
    "02_verify": "02_preview_email.html",
    "03_password_reset": "03_forgot_password_email.html",
    "04_job_complete": "04_job_complete_email.html",
    "05_funds_receipt": "05_payment_recieved_email.html",
    "06_results_expiring": "06_results_expiring .html",
    "07_ticket_created": "07_ticket_created.html",
    "08_ticket_reply": "08 tickey reply.html",
    "09_credit_exhausted": "09_credt_exhausted.html",
    "10_low_balance": "10_low_blance warning.html",
    "11_trial_expiring": "11_trial_expire_soon.html",
    "12_exclusive_lead": "12_exclusive_lead_confirmed.html",
    "13_refund": "13_refund_issued.html",
    "14_search_stopped": "14_search_stopped.html",
    "15_payment_failed": "15_payment_failed.html",
    "16_we_miss_you": "16_we_miss_you.html",
    "17_drip_day1": "17_drip_day1.html",
    "18_drip_day3": "18_drip_free creditwaiting day 3.html",
    "19_drip_day5": "19_drip_day5_soical_proof.html",
    "20_drip_day7": "20_free_trial_ended_day_7.html",
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%b %d, %Y")


def _load(key: str) -> str:
    """Load an HTML template by logical key."""
    filename = _FILENAMES.get(key, "")
    if not filename:
        print(f"[EMAIL ERROR] Unknown template key: {key}", flush=True)
        return ""
    path = os.path.join(_TEMPLATE_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[EMAIL ERROR] Could not load template {filename}: {type(e).__name__}: {e}", flush=True)
        return ""


def _send(to: str, subject: str, html: str):
    """Send email via Resend. Logs errors to avoid silent failures."""
    if not html:
        print(f"[EMAIL ERROR] Empty template for '{subject}' to {to}, skipping send", flush=True)
        return
    try:
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        print(f"[EMAIL] Sent '{subject}' to {to}", flush=True)
    except Exception as e:
        print(f"[EMAIL ERROR] Failed to send '{subject}' to {to}: {type(e).__name__}: {e}", flush=True)


# ─── Transactional Emails (01–08) ────────────────────────────────────────────

def send_welcome(email: str, is_trial: bool = False):
    """01 — Welcome email on registration."""
    html = _load("01_welcome")
    html = html.replace("DASHBOARD_URL", f"{DOMAIN}/database")
    _send(email, "Welcome to Auction Intel", html)


def send_verification_email(email: str, verify_url: str):
    """02 — Email verification link."""
    html = _load("02_verify")
    html = html.replace("DASHBOARD_URL", verify_url)
    _send(email, "Verify Your Email — Auction Intel", html)


def send_password_reset(email: str, reset_url: str):
    """03 — Password reset link."""
    html = _load("03_password_reset")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", reset_url)
    _send(email, "Reset Your Auction Intel Password", html)


def send_job_complete(email: str, job_id: str, nonprofit_count: int,
                      found_count: int, billable_count: int, total_cost_cents: int):
    """04 — Search job completion receipt."""
    html = _load("04_job_complete")
    html = html.replace("{date}", _today())
    html = html.replace("{job_id}", job_id[:8])
    html = html.replace("{cta_url}", f"{DOMAIN}/results")
    _send(email, f"Research Complete — {billable_count} Billable Leads Found", html)


def send_funds_receipt(email: str, amount_cents: int, new_balance_cents: int,
                       payment_method: str = "Card", transaction_id: str = "",
                       processed_at: str = ""):
    """05 — Payment receipt when user adds funds."""
    amount = f"${amount_cents / 100:.2f}"
    html = _load("05_funds_receipt")
    html = html.replace("{date}", _today())
    html = html.replace("{amount}", amount)
    html = html.replace("{payment_method}", payment_method)
    html = html.replace("{transaction_id}", transaction_id or "N/A")
    html = html.replace("{processed_at}", processed_at or _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, f"Payment Received — {amount} Added to Your Wallet", html)


def send_results_expiring(email: str, job_id: str, time_remaining: str, expires_label: str):
    """06 — Results expiration warning."""
    html = _load("06_results_expiring")
    html = html.replace("{date}", _today())
    html = html.replace("{job_id}", job_id[:8])
    html = html.replace("{days_remaining}", time_remaining)
    html = html.replace("{cta_url}", f"{DOMAIN}/results")
    _send(email, f"Results Expiring in {expires_label} — Download Now", html)


def send_results_expiring_10_days(email: str, job_id: str):
    send_results_expiring(email, job_id, "10", "10 Days")

def send_results_expiring_7_days(email: str, job_id: str):
    send_results_expiring(email, job_id, "7", "7 Days")

def send_results_expiring_72_hours(email: str, job_id: str):
    send_results_expiring(email, job_id, "3", "72 Hours")

def send_results_expiring_24_hours(email: str, job_id: str):
    send_results_expiring(email, job_id, "1", "24 Hours")


def send_ticket_created(ticket_id: int, subject: str, user_email: str, message: str):
    """07 — Notify admin of new support ticket."""
    html = _load("07_ticket_created")
    html = html.replace("{date}", _today())
    html = html.replace("{ticket_number}", str(ticket_id))
    html = html.replace("{user_email}", user_email)
    html = html.replace("{subject}", subject)
    html = html.replace("{message}", message)
    html = html.replace("{cta_url}", f"{DOMAIN}/support/{ticket_id}")
    _send("admin@auctionintel.us", f"New Support Ticket #{ticket_id}: {subject}", html)


def send_ticket_reply_to_user(user_email: str, ticket_id: int, subject: str, reply_message: str):
    """08 — Notify user of admin reply on their ticket."""
    html = _load("08_ticket_reply")
    html = html.replace("{date}", _today())
    html = html.replace("{ticket_number}", str(ticket_id))
    html = html.replace("{subject}", subject)
    html = html.replace("{reply}", reply_message)
    html = html.replace("{cta_url}", f"{DOMAIN}/support/{ticket_id}")
    _send(user_email, f"Reply on Ticket #{ticket_id}: {subject}", html)


# ─── Notification Emails (09–16) ─────────────────────────────────────────────

def send_credit_exhausted(email: str):
    """09 — Balance hit $0."""
    html = _load("09_credit_exhausted")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, "Your Auction Intel Balance Has Run Out", html)


def send_low_balance_warning(email: str, balance_cents: int):
    """10 — Balance running low."""
    html = _load("10_low_balance")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, f"Low Balance Warning — ${balance_cents / 100:.2f} Remaining", html)


def send_trial_expiring(email: str, days_left: int, credit_remaining_cents: int = 0, expires_date: str = ""):
    """11 — Trial ending soon."""
    html = _load("11_trial_expiring")
    html = html.replace("{date}", _today())
    html = html.replace("{days_remaining}", str(days_left))
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, f"Your Free Trial Ends in {days_left} Day{'s' if days_left != 1 else ''}", html)


def send_exclusive_lead_confirmed(email: str, nonprofit_name: str = "", event_title: str = "", lock_fee_cents: int = 0):
    """12 — Lead locked confirmation."""
    html = _load("12_exclusive_lead")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/results")
    _send(email, f"Lead Locked — {nonprofit_name or 'Exclusive Lead'}", html)


def send_refund_issued(email: str, refund_cents: int = 0, reason: str = "", new_balance_cents: int = 0):
    """13 — Refund applied to wallet."""
    html = _load("13_refund")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, f"Refund Issued — ${refund_cents / 100:.2f} Added to Your Wallet", html)


def send_search_stopped(email: str, job_id: str, processed: int = 0, total: int = 0,
                         found: int = 0, charged_cents: int = 0):
    """14 — Search stopped mid-batch."""
    html = _load("14_search_stopped")
    html = html.replace("{date}", _today())
    html = html.replace("{job_id}", job_id[:8])
    html = html.replace("{cta_url}", f"{DOMAIN}/results")
    _send(email, f"Search Stopped — {processed}/{total} Processed", html)


def send_payment_failed(email: str, amount_cents: int = 0, reason: str = "Card declined by issuer"):
    """15 — Payment failed notification."""
    amount = f"${amount_cents / 100:.2f}"
    html = _load("15_payment_failed")
    html = html.replace("{date}", _today())
    html = html.replace("{amount}", amount)
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, "Payment Failed — Auction Intel", html)


def send_we_miss_you(email: str, balance_cents: int = 0, last_search_date: str = ""):
    """16 — Re-engagement after 30+ days inactive."""
    html = _load("16_we_miss_you")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/database")
    _send(email, "We Miss You — Auction Intel", html)


# ─── Drip Campaign (17–20) ───────────────────────────────────────────────────

def send_drip_day1_how_it_works(email: str):
    """17 — Day 1 after signup: how the platform works."""
    html = _load("17_drip_day1")
    html = html.replace("{date}", _today())
    html = html.replace("{trial_credit}", "$20.00")
    html = html.replace("{cta_url}", f"{DOMAIN}/database")
    _send(email, "How Auction Intel Works — 3 Simple Steps", html)


def send_drip_day3_first_search(email: str, credit_remaining: str = "$20.00", days_left: int = 4):
    """18 — Day 3 after signup: nudge to run first search."""
    html = _load("18_drip_day3")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/database")
    _send(email, f"Your {credit_remaining} Trial Credit Expires in {days_left} Days", html)


def send_drip_day5_social_proof(email: str, days_left: int = 2):
    """19 — Day 5 after signup: social proof + urgency."""
    html = _load("19_drip_day5")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/database")
    _send(email, "Users Are Finding 15-25% Auction Hit Rates", html)


def send_drip_day7_trial_ended(email: str, credit_removed_cents: int = 1420):
    """20 — Day 7: trial expired, invite to add funds."""
    html = _load("20_drip_day7")
    html = html.replace("{date}", _today())
    html = html.replace("{cta_url}", f"{DOMAIN}/wallet")
    _send(email, "Your Free Trial Has Ended", html)
