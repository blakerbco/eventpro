"""
AUCTIONFINDER — Email Templates & Sending

All transactional and drip-campaign emails use the designer's mobile-ready
template (inline styles, tiny @media block). Sent via Resend API.
"""

import os
from datetime import datetime, timezone
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Auction Intel Admin <admin@auctionintel.app>")
DOMAIN = os.environ.get("APP_DOMAIN", "http://localhost:5000")
LOGO_URL = "https://content.app-sources.com/s/18152366670198361/uploads/RC/5-2077317.svg"

# Minimal CSS — only @media for mobile (everything else is inlined)
_PRO_CSS = """
@media screen and (max-width:620px){
.container{width:100% !important;max-width:100% !important;}
.px{padding-left:16px !important;padding-right:16px !important;}
.pt{padding-top:16px !important;}
.pb{padding-bottom:16px !important;}
.btn a{display:block !important;width:100% !important;box-sizing:border-box !important;text-align:center !important;}
}
"""

# Inline style shortcuts
_FS = "font-family:Arial,Helvetica,sans-serif;"
_CS = "color:rgba(255,255,255,.78);"
_CW = "color:#ffffff;"
_CG = "color:#f5c542;"


# ─── Template & Helpers ──────────────────────────────────────────────────────

def _base_template(title: str, heading: str, body_html: str,
                   cta_text: str = "", cta_url: str = "",
                   summary: str = "") -> str:
    """Wrap email content in the designer's mobile-ready Auction Intel template."""
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")

    cta_block = ""
    if cta_text and cta_url:
        cta_block = (
            '<table role="presentation" class="btn" cellspacing="0" cellpadding="0" border="0" style="margin:18px 0 10px 0;">'
            "<tr>"
            '<td bgcolor="#f5c542" style="border-radius:12px;">'
            f'<a href="{cta_url}" target="_blank" style="display:inline-block;padding:12px 16px;{_FS}font-size:14px;font-weight:800;color:#000000;text-decoration:none;border-radius:12px;">'
            f"{cta_text}</a>"
            "</td></tr></table>"
            f'<p style="margin:0;{_FS}font-size:12px;line-height:18px;{_CS}">'
            f'If the button doesn\'t work, open: <a href="{cta_url}" style="{_CG}text-decoration:underline;">{cta_url}</a></p>'
        )

    summary_div = ""
    lead_block = ""
    if summary:
        summary_div = (
            '<div style="display:none;font-size:1px;color:#f5f5f0;line-height:1px;'
            f'max-height:0;max-width:0;opacity:0;overflow:hidden;">{summary}</div>'
        )
        lead_block = f'<p style="margin:0 0 16px 0;{_FS}font-size:14px;line-height:20px;{_CS}">{summary}</p>'

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="x-ua-compatible" content="ie=edge">'
        '<meta name="x-apple-disable-message-reformatting">'
        f"<title>{title}</title>"
        '<style>' + _PRO_CSS + '</style>'
        '<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>'
        "<o:AllowPNG/></o:OfficeDocumentSettings></xml><![endif]-->"
        "</head>"
        '<body style="margin:0;padding:0;background:#f5f5f0;">'
        + summary_div +
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f5f5f0;"><tr>'
        '<td align="center" class="px pt pb" style="padding:26px 12px;">'

        # Container
        '<table role="presentation" class="container" width="600" cellspacing="0" cellpadding="0" border="0" style="width:600px;max-width:600px;">'

        # Above-card bar
        '<tr><td class="px" style="padding:0 0 12px 0;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        f'<td align="left" style="{_FS}font-size:12px;color:#000;">Auction Intel Notification</td>'
        f'<td align="right" style="{_FS}font-size:12px;color:#000;">{today}</td>'
        "</tr></table></td></tr>"

        # Black card
        '<tr><td style="background:#000000;border-radius:16px;overflow:hidden;border:1px solid rgba(0,0,0,.15);">'

        # Card header — logo + label
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        '<td class="px" style="padding:18px;border-top:4px solid #f5c542;border-bottom:1px solid rgba(255,255,255,.18);">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        '<td align="left" style="padding:0;">'
        f'<a href="https://auctionintel.app" target="_blank" style="text-decoration:none;">'
        f'<img src="{LOGO_URL}" width="150" alt="Auction Intel" style="display:block;border:0;outline:none;text-decoration:none;height:auto;max-width:150px;"></a></td>'
        f'<td align="right" style="padding:0;{_FS}font-size:12px;{_CS}">Auction Intel Notification</td>'
        "</tr></table></td></tr></table>"

        # Card body
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        '<td class="px" style="padding:22px 18px 18px 18px;">'
        f'<h1 style="margin:0 0 10px 0;{_FS}font-size:24px;line-height:30px;{_CW}">{heading}</h1>'
        + lead_block
        + body_html
        + cta_block +
        "</td></tr></table>"

        # Card footer
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        '<td class="px" style="padding:16px 18px;border-top:1px solid rgba(255,255,255,.18);">'
        f'<p style="margin:0 0 8px 0;{_FS}font-size:11px;line-height:16px;{_CS}">&copy; 2026 Auction Intel. All rights reserved.</p>'
        f'<p style="margin:0 0 8px 0;{_FS}font-size:11px;line-height:16px;{_CS}">7050 W 120th Ave Suite 50B<br>Broomfield, CO 80020</p>'
        f'<p style="margin:0;{_FS}font-size:11px;line-height:16px;{_CS}">'
        f'Support: <a href="mailto:support@auctionintel.us" style="{_CG}text-decoration:underline;">support@auctionintel.us</a> &bull; 303-719-4851</p>'
        f'<p style="margin:12px 0 0 0;{_FS}font-size:10px;line-height:15px;{_CS}">This is an automated message. Please do not reply directly to this email.</p>'
        "</td></tr></table>"

        # Close card, container, outer table
        "</td></tr></table></td></tr></table>"
        "</body></html>"
    )


def _p(text: str) -> str:
    """Paragraph with designer's inline styles."""
    return f'<p style="margin:0 0 14px 0;{_FS}font-size:14px;line-height:20px;{_CS}">{text}</p>'


def _send(to: str, subject: str, html: str):
    """Send email via Resend. Logs errors to avoid silent failures."""
    try:
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
    except Exception as e:
        print(f"[EMAIL ERROR] Failed to send '{subject}' to {to}: {type(e).__name__}: {e}", flush=True)


# ─── Transactional Emails (01–08) ────────────────────────────────────────────

def send_welcome(email: str, is_trial: bool = False):
    """Send welcome email on registration."""
    trial_note = ""
    if is_trial:
        trial_note = _p(
            'Your free trial is active with <strong style="color:#f5c542;">$20.00 in credit</strong> '
            "— covers both search fees and lead results. Your trial lasts 7 days, "
            "so dive in and start finding auction leads right away."
        )

    body = (
        _p("Your account is ready to go. Here's what you can do:")
        + f'<p style="margin:0 0 6px 0;{_FS}font-size:14px;line-height:20px;{_CS}">'
        '&bull; &nbsp;Search our IRS nonprofit database with over 1.8 million organizations</p>'
        + f'<p style="margin:0 0 6px 0;{_FS}font-size:14px;line-height:20px;{_CS}">'
        '&bull; &nbsp;Run AI-powered auction event research on any list of nonprofits</p>'
        + f'<p style="margin:0 0 14px 0;{_FS}font-size:14px;line-height:20px;{_CS}">'
        '&bull; &nbsp;Get verified contacts, event dates, and auction details as downloadable reports</p>'
        + trial_note
        + _p("If you have any questions, just reply to this email — we're happy to help.")
    )

    _send(
        email,
        "Welcome to Auction Intel",
        _base_template(
            "Welcome to Auction Intel",
            "Welcome to Auction Intel",
            body,
            cta_text="Access Your Dashboard",
            cta_url=f"{DOMAIN}/database",
            summary="Welcome to Auction Intel — the fastest way to find nonprofit auction events and verified contact details.",
        ),
    )


def send_verification_email(email: str, verify_url: str):
    """Send email verification link to new user."""
    body = (
        _p("Please verify your email address by clicking the button below. This link will expire in <strong>24 hours</strong>.")
        + _p(
            "If you didn't create an account, you can safely ignore this email."
        )
    )
    _send(
        email,
        "Verify Your Email — Auction Intel",
        _base_template(
            "Verify Your Email",
            "Verify Your Email",
            body,
            cta_text="Verify Email",
            cta_url=verify_url,
            summary="Thanks for signing up for Auction Intel!",
        ),
    )


def send_password_reset(email: str, reset_url: str):
    """Send password reset link."""
    body = (
        _p("Click the button below to set a new password. This link will expire in <strong>15 minutes</strong>.")
        + _p(
            "If you didn't request a password reset, you can safely ignore this email. "
            "Your password will remain unchanged."
        )
    )
    _send(
        email,
        "Reset Your Auction Intel Password",
        _base_template(
            "Reset Your Password",
            "Reset Your Password",
            body,
            cta_text="Reset Password",
            cta_url=reset_url,
            summary="We received a request to reset the password for your Auction Intel account.",
        ),
    )


def send_job_complete(email: str, job_id: str, nonprofit_count: int,
                      found_count: int, billable_count: int, total_cost_cents: int):
    """Send search job completion receipt."""
    total_cost = f"${total_cost_cents / 100:.2f}"
    _row = f"padding:8px 0;{_FS}font-size:14px;"

    body = (
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 18px 0;width:100%;">'
        "<tr><td style=\"padding:12px 16px;border-radius:8px;background:rgba(255,255,255,.06);\">"
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width:100%;">'
        f'<tr><td style="{_row}{_CS}">Nonprofits Searched</td>'
        f'<td align="right" style="{_row}font-weight:bold;{_CW}">{nonprofit_count}</td></tr>'
        f'<tr><td style="{_row}{_CS}">Auction Events Found</td>'
        f'<td align="right" style="{_row}font-weight:bold;{_CW}">{found_count}</td></tr>'
        f'<tr><td style="{_row}{_CS}">Billable Leads</td>'
        f'<td align="right" style="{_row}font-weight:bold;{_CW}">{billable_count}</td></tr>'
        f'<tr><td style="{_row}border-top:1px solid rgba(255,255,255,.18);{_CS}">Total Charged</td>'
        f'<td align="right" style="{_row}border-top:1px solid rgba(255,255,255,.18);font-size:16px;font-weight:bold;{_CG}">{total_cost}</td></tr>'
        "</table></td></tr></table>"
        + _p("Your results are ready to download and will be available for 180 days.")
    )

    _send(
        email,
        f"Research Complete — {found_count} Auction Events Found",
        _base_template(
            "Research Complete",
            "Your Research Is Complete",
            body,
            cta_text="Download Results",
            cta_url=f"{DOMAIN}/results",
            summary=f"Your auction research job {job_id[:8]} has finished processing.",
        ),
    )


def send_funds_receipt(email: str, amount_cents: int, new_balance_cents: int):
    """Send receipt when user adds funds via Stripe."""
    amount = f"${amount_cents / 100:.2f}"
    balance = f"${new_balance_cents / 100:.2f}"
    _row = f"padding:8px 0;{_FS}font-size:14px;"

    body = (
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 18px 0;width:100%;">'
        "<tr><td style=\"padding:12px 16px;border-radius:8px;background:rgba(255,255,255,.06);\">"
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width:100%;">'
        f'<tr><td style="{_row}{_CS}">Amount Added</td>'
        f'<td align="right" style="{_row}font-size:16px;font-weight:bold;{_CW}">+{amount}</td></tr>'
        f'<tr><td style="{_row}border-top:1px solid rgba(255,255,255,.18);{_CS}">New Balance</td>'
        f'<td align="right" style="{_row}border-top:1px solid rgba(255,255,255,.18);font-size:16px;font-weight:bold;{_CG}">{balance}</td></tr>'
        "</table></td></tr></table>"
        + _p("Your funds are available immediately. You can start running auction research right away.")
    )

    _send(
        email,
        f"Payment Received — {amount} Added to Your Wallet",
        _base_template(
            "Payment Receipt",
            "Payment Received",
            body,
            cta_text="View Wallet",
            cta_url=f"{DOMAIN}/wallet",
            summary="Your wallet has been topped up successfully. Here are your transaction details:",
        ),
    )


def send_results_expiring(email: str, job_id: str, time_remaining: str, expires_label: str):
    """Send results expiration warning."""
    body = (
        _p("Once expired, these results cannot be recovered. If you haven't already, please download your data now.")
        + _p("We retain all research results for 180 days from the date they were generated.")
    )
    _send(
        email,
        f"Results Expiring in {expires_label} — Download Now",
        _base_template(
            "Results Expiring Soon",
            "Your Results Are Expiring Soon",
            body,
            cta_text="Download Results",
            cta_url=f"{DOMAIN}/results",
            summary=f"Your research results from job {job_id[:8]} will be permanently deleted in {time_remaining}.",
        ),
    )


def send_results_expiring_10_days(email: str, job_id: str):
    send_results_expiring(email, job_id, "10 days", "10 Days")

def send_results_expiring_7_days(email: str, job_id: str):
    send_results_expiring(email, job_id, "7 days", "7 Days")

def send_results_expiring_72_hours(email: str, job_id: str):
    send_results_expiring(email, job_id, "72 hours", "72 Hours")

def send_results_expiring_24_hours(email: str, job_id: str):
    send_results_expiring(email, job_id, "24 hours", "24 Hours")


def send_ticket_created(ticket_id: int, subject: str, user_email: str, message: str):
    """Notify admin when a new support ticket is created."""
    body = (
        _p(f"<strong>Subject:</strong> {subject}")
        + _p(f"<strong>Message:</strong><br>{message}")
    )
    _send(
        "admin@auctionintel.us",
        f"New Support Ticket #{ticket_id}: {subject}",
        _base_template(
            "New Support Ticket",
            f"Ticket #{ticket_id}",
            body,
            cta_text="View Ticket",
            cta_url=f"{DOMAIN}/support/{ticket_id}",
            summary=f"A new support ticket has been submitted by {user_email}.",
        ),
    )


def send_ticket_reply_to_user(user_email: str, ticket_id: int, subject: str, reply_message: str):
    """Notify user when admin replies to their ticket."""
    body = (
        _p(f"<strong>Subject:</strong> {subject}")
        + _p(f"<strong>Reply:</strong><br>{reply_message}")
    )
    _send(
        user_email,
        f"Reply on Ticket #{ticket_id}: {subject}",
        _base_template(
            "Support Ticket Reply",
            "New Reply on Your Ticket",
            body,
            cta_text="View Conversation",
            cta_url=f"{DOMAIN}/support/{ticket_id}",
            summary=f"You have a new reply on your support ticket #{ticket_id}.",
        ),
    )


# ─── Drip Campaign (17–20) ───────────────────────────────────────────────────

def send_drip_day1_how_it_works(email: str):
    """Day 1 after signup — explain how the platform works in 3 steps."""
    body = (
        f'<p style="margin:0 0 4px 0;{_FS}font-size:14px;line-height:20px;{_CW}">'
        f'<strong style="{_CG}">Step 1</strong> &mdash; <strong>Build Your List</strong></p>'
        + _p(
            "Search our IRS database of 1.8 million+ nonprofits. Filter by state, city, "
            "NTEE category, or revenue size. Select the organizations you want to research "
            "and send them to our AI search tool."
        )
        + f'<p style="margin:0 0 4px 0;{_FS}font-size:14px;line-height:20px;{_CW}">'
        f'<strong style="{_CG}">Step 2</strong> &mdash; <strong>AI Researches Each Nonprofit</strong></p>'
        + _p(
            "Our AI scans the web for upcoming auction and gala events — surfacing event names, "
            "dates, venues, organizer names, and verified contact information for each nonprofit "
            "in your list."
        )
        + f'<p style="margin:0 0 4px 0;{_FS}font-size:14px;line-height:20px;{_CW}">'
        f'<strong style="{_CG}">Step 3</strong> &mdash; <strong>Download Your Leads</strong></p>'
        + _p(
            "Results are delivered as a clean, downloadable spreadsheet with event details, "
            "contact emails, event URLs, and lead quality ratings — ready for outreach."
        )
        + _p(
            'You have <strong style="color:#f5c542;">$20.00 in free credit</strong> to get started — '
            "enough to research hundreds of nonprofits. There's nothing to lose."
        )
    )

    _send(
        email,
        "How Auction Intel Works — 3 Simple Steps",
        _base_template(
            "How Auction Intel Works",
            "How Auction Intel Works",
            body,
            cta_text="Browse the Nonprofit Database",
            cta_url=f"{DOMAIN}/database",
            summary="Finding nonprofit auction events has never been easier. Here's the process in three simple steps.",
        ),
    )


def send_drip_day3_first_search(email: str):
    """Day 3 after signup — nudge to run first search before credit expires."""
    body = (
        _p("<strong>Here's the fastest way to get started:</strong>")
        + _p(
            "Run a small test search of 20\u201330 nonprofits. It costs less than $1, "
            "takes about 2 minutes, and gives you a clear picture of the leads "
            "Auction Intel can deliver."
        )
        + _p(
            "<strong>Pro tip:</strong> Filter by your state and a specific nonprofit category — "
            "like <em>Arts &amp; Culture</em>, <em>Education</em>, or <em>Health</em> — "
            "to get the most relevant results for your business."
        )
        + _p("Your trial credit expires in 4 days. Give it a try.")
    )

    _send(
        email,
        "Your $20 Trial Credit Expires in 4 Days",
        _base_template(
            "Your Free Credit Is Waiting",
            "Your Free Credit Is Waiting",
            body,
            cta_text="Run Your First Search",
            cta_url=f"{DOMAIN}/database",
            summary="You signed up a few days ago but haven't run your first search yet. Your $20.00 in free credit expires soon — don't let it go to waste.",
        ),
    )


def send_drip_day5_social_proof(email: str):
    """Day 5 after signup — social proof and urgency with 2 days left."""
    body = (
        _p(
            "Users who target specific regions and nonprofit categories typically find "
            "confirmed auction events for "
            '<strong style="color:#f5c542;">15\u201325%</strong> '
            "of organizations in their list. That means a search of 100 nonprofits often surfaces "
            "15 to 25 verified, actionable leads — complete with event details and contact information."
        )
        + _p(
            "<strong>What makes the difference:</strong> Searching by state and NTEE category. "
            "For example, <em>\u201cColorado Arts &amp; Culture\u201d</em> or "
            "<em>\u201cTexas Education\u201d</em> organizations consistently surface "
            "the most actionable leads."
        )
        + _p(
            "Your trial credit expires in <strong>2 days</strong>. "
            "There's nothing to lose — use it while you can."
        )
    )

    _send(
        email,
        "Users Are Finding 15–25% Auction Hit Rates",
        _base_template(
            "See What Others Are Finding",
            "See What Others Are Finding",
            body,
            cta_text="Start Searching Now",
            cta_url=f"{DOMAIN}/database",
            summary="Auction Intel users are discovering nonprofit auction events across the country every day.",
        ),
    )


def send_drip_day7_trial_ended(email: str):
    """Day 7 — trial has expired, invite to add funds."""
    body = (
        _p(
            "Your account is still active — you can add funds anytime and pick up right "
            "where you left off. There are no subscriptions or monthly fees. You only pay "
            "for the leads you use, and purchased credit <strong>never expires</strong>."
        )
        + _p("Any results you downloaded during your trial are yours to keep.")
    )

    _send(
        email,
        "Your Free Trial Has Ended",
        _base_template(
            "Your Free Trial Has Ended",
            "Your Free Trial Has Ended",
            body,
            cta_text="Add Funds to Continue",
            cta_url=f"{DOMAIN}/wallet",
            summary="Your 7-day free trial has expired and any remaining trial credit has been removed from your wallet.",
        ),
    )
