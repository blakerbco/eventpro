"""
AUCTIONFINDER — Email Templates & Sending

All transactional emails use the Auction Intel professional branded template
and are sent via Resend API. All visual styles are INLINED for email client
compatibility. The <style> block only handles @font-face and @media queries.
"""

import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Auction Intel Admin <admin@auctionintel.app>")
DOMAIN = os.environ.get("APP_DOMAIN", "http://localhost:5000")
LOGO_URL = "https://content.app-sources.com/s/18152366670198361/uploads/RC/5-2077317.svg"

# Minimal CSS — only what can't be inlined (@font-face, @media, preheader hide)
_PRO_CSS = """
.email_summary{display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;}
body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}
table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}
img{-ms-interpolation-mode:bicubic;border:0;height:auto;line-height:100%;outline:none;text-decoration:none;}
@media only screen{
@font-face{font-family:'Open Sans';font-style:normal;font-weight:400;src:local("Open Sans Regular"),local("OpenSans-Regular"),url(https://fonts.gstatic.com/s/opensans/v14/cJZKeOuBrn4kERxqtaUH3VtXRa8TVwTICgirnJhmVJw.woff2) format("woff2");}
@font-face{font-family:'Open Sans';font-style:normal;font-weight:700;src:local("Open Sans Bold"),local("OpenSans-Bold"),url(https://fonts.gstatic.com/s/opensans/v14/k3k702ZOKiLJc3WVjuplzOgdm0LZdjqr5-oayXSOefg.woff2) format("woff2");}
td,p,a,li,h1,h2,h3,h4,h5,h6,strong{font-family:"Open Sans",Arial,Helvetica,sans-serif !important;}
}
@media(max-width:689px){
.col_50{display:block !important;max-width:100% !important;}
.mobile_center{text-align:center !important;}
}
"""


# ─── Template & Helpers ──────────────────────────────────────────────────────

def _base_template(title: str, heading: str, body_html: str,
                   cta_text: str = "", cta_url: str = "",
                   summary: str = "") -> str:
    """Wrap email content in the professional Auction Intel template.
    All visual styles are inlined for maximum email client compatibility."""

    cta_block = ""
    if cta_text and cta_url:
        cta_block = (
            '<table role="presentation" align="left" cellspacing="0" cellpadding="0" border="0" style="Margin:24px 0 10px 0;">'
            "<tbody><tr>"
            '<td style="background-color:#f5c542;border-radius:12px;text-align:center;font-weight:bold;">'
            f'<a href="{cta_url}" target="_blank" style="display:inline-block;font-weight:800;color:#000000;text-decoration:none;padding:13px 24px;font-family:Arial,Helvetica,sans-serif;font-size:16px;">{cta_text}</a>'
            "</td></tr></tbody></table>"
            '<p style="Margin:0;clear:both;font-size:14px;line-height:22px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">'
            f'If the button doesn\'t work, open: <a href="{cta_url}" style="color:#f5c542;text-decoration:underline;">{cta_url}</a>'
            "</p>"
        )

    summary_div = ""
    lead_block = ""
    if summary:
        summary_div = f'<div class="email_summary">{summary}</div>'
        lead_block = (
            '<p style="Margin-top:0;Margin-bottom:16px;font-size:19px;line-height:31px;'
            f'color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">{summary}</p>'
        )

    return (
        '<!doctype html>'
        '<html lang="en" style="min-width:100%;background-color:#f5f5f0;">'
        "<head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, shrink-to-fit=no">'
        '<meta name="format-detection" content="telephone=no">'
        '<meta name="format-detection" content="date=no">'
        '<meta name="format-detection" content="address=no">'
        '<meta name="format-detection" content="email=no">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        '<meta name="x-apple-disable-message-reformatting">'
        f"<title>{title}</title>"
        '<style type="text/css">'
        + _PRO_CSS +
        "</style></head>"

        # Body
        '<body style="-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;'
        'min-width:100%;background-color:#f5f5f0;height:100% !important;'
        'margin:0 !important;padding:0 !important;width:100% !important;">'
        + summary_div +

        # ── Header ──
        '<table role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td style="font-size:0;text-align:center;line-height:100%;background-color:#f5f5f0;padding:32px 16px 0 16px;">'
        '<table role="presentation" align="center" style="max-width:800px;Margin:0 auto;" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td style="font-size:0;text-align:center;background-color:#000000;border-radius:16px 16px 0 0;border-top:4px solid #f5c542;padding:16px;">'
        '<div style="font-size:0;text-align:center;max-width:624px;Margin:0 auto;">'

        # Logo
        '<div class="col_50" style="vertical-align:top;display:inline-block;width:100%;max-width:400px;">'
        '<table role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="mobile_center" style="vertical-align:top;padding:8px 16px;text-align:left;">'
        '<p style="line-height:100%;clear:both;Margin:0;">'
        f'<a href="https://auctionintel.app" target="_blank"><img src="{LOGO_URL}" width="140" alt="Auction Intel" style="max-width:140px;"></a>'
        "</p></td></tr></tbody></table></div>"

        # Notification label
        '<div class="col_50" style="vertical-align:top;display:inline-block;width:100%;max-width:400px;">'
        '<table role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="mobile_center" style="vertical-align:top;padding:8px 16px;text-align:right;color:rgba(255,255,255,.78);">'
        '<p style="Margin:0;font-size:14px;line-height:22px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">Auction Intel Notification</p>'
        "</td></tr></tbody></table></div>"

        "</div></td></tr></tbody></table></td></tr></tbody></table>"

        # ── Body ──
        '<table role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td style="font-size:0;text-align:center;line-height:100%;background-color:#f5f5f0;padding:0 16px;">'
        '<table role="presentation" align="center" style="max-width:800px;Margin:0 auto;" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td style="font-size:0;text-align:center;background-color:#000000;padding:32px 16px;">'
        '<div style="font-size:0;text-align:center;max-width:624px;Margin:0 auto;">'
        '<table role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td style="vertical-align:top;padding:0 16px;text-align:left;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">'

        # Heading
        f'<h2 style="Margin-top:0;Margin-bottom:8px;font-size:28px;line-height:38px;font-weight:bold;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">{heading}</h2>'
        + lead_block
        + body_html
        + cta_block +

        "</td></tr></tbody></table></div></td></tr>"

        # ── Footer ──
        "<tr>"
        '<td style="font-size:0;text-align:center;background-color:#000000;border-radius:0 0 16px 16px;border-top:1px solid rgba(255,255,255,.18);padding:32px 16px;">'
        '<div style="font-size:0;text-align:center;max-width:624px;Margin:0 auto;">'
        '<table role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td style="vertical-align:top;padding:0 16px;text-align:center;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">'

        '<p style="Margin-top:0;Margin-bottom:8px;font-size:14px;line-height:22px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">&copy; 2026 Auction Intel. All rights reserved.</p>'
        '<p style="Margin-top:0;Margin-bottom:8px;font-size:14px;line-height:22px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">7050 W 120th Ave Suite 50B<br>Broomfield, CO 80020</p>'
        '<p style="Margin-top:0;Margin-bottom:0;font-size:14px;line-height:22px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">'
        'Support: <a href="mailto:support@auctionintel.us" style="color:#f5c542;text-decoration:underline;">support@auctionintel.us</a>'
        " &nbsp;&bull;&nbsp; 303-719-4851</p>"
        '<p style="Margin-top:16px;Margin-bottom:0;font-size:14px;line-height:22px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">This is an automated message. Please do not reply directly to this email.</p>'

        "</td></tr></tbody></table></div></td></tr>"
        "</tbody></table></td></tr></tbody></table>"
        "</body></html>"
    )


def _p(text: str) -> str:
    """Wrap text in a styled paragraph with all styles inlined."""
    return (
        '<p style="Margin-top:0;Margin-bottom:16px;font-size:16px;line-height:26px;'
        f'color:#ffffff;font-family:Arial,Helvetica,sans-serif;">{text}</p>'
    )


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


# ─── Email Functions ──────────────────────────────────────────────────────────

def send_welcome(email: str, is_trial: bool = False):
    """Send welcome email on registration."""
    trial_note = ""
    if is_trial:
        trial_note = _p(
            "Your free trial is active with <strong>$20.00 in credit</strong> "
            "— covers both search fees and lead results. Your trial lasts 7 days, "
            "so dive in and start finding auction leads right away."
        )

    body = (
        """
        <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 20px 0;">
            <tr><td style="padding:6px 0;font-size:15px;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">&#x2022; &nbsp;Search our IRS nonprofit database with over 1.8 million organizations</td></tr>
            <tr><td style="padding:6px 0;font-size:15px;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">&#x2022; &nbsp;Run AI-powered auction event research on any list of nonprofits</td></tr>
            <tr><td style="padding:6px 0;font-size:15px;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">&#x2022; &nbsp;Get verified contacts, event dates, and auction details delivered as downloadable reports</td></tr>
        </table>
        """
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
            '<span style="font-size:14px;line-height:22px;color:rgba(255,255,255,.78);">'
            "If you didn't create an account, you can safely ignore this email.</span>"
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
            '<span style="font-size:14px;line-height:22px;color:rgba(255,255,255,.78);">'
            "If you didn't request a password reset, you can safely ignore this email. "
            "Your password will remain unchanged.</span>"
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

    body = (
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 24px 0;width:100%;">'
        "<tr>"
        '<td style="padding:12px 16px;border-radius:6px;">'
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width:100%;">'
        "<tr>"
        '<td style="padding:8px 0;font-size:14px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">Nonprofits Searched</td>'
        f'<td align="right" style="padding:8px 0;font-size:14px;font-weight:bold;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">{nonprofit_count}</td>'
        "</tr><tr>"
        '<td style="padding:8px 0;font-size:14px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">Auction Events Found</td>'
        f'<td align="right" style="padding:8px 0;font-size:14px;font-weight:bold;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">{found_count}</td>'
        "</tr><tr>"
        '<td style="padding:8px 0;font-size:14px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">Billable Leads</td>'
        f'<td align="right" style="padding:8px 0;font-size:14px;font-weight:bold;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">{billable_count}</td>'
        "</tr><tr>"
        '<td style="padding:8px 0;border-top:1px solid #333333;font-size:14px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">Total Charged</td>'
        f'<td align="right" style="padding:8px 0;border-top:1px solid #333333;font-size:16px;font-weight:bold;color:#f5c542;font-family:Arial,Helvetica,sans-serif;">{total_cost}</td>'
        "</tr></table></td></tr></table>"
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

    body = (
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 24px 0;width:100%;">'
        "<tr>"
        '<td style="padding:12px 16px;border-radius:6px;">'
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width:100%;">'
        "<tr>"
        '<td style="padding:8px 0;font-size:14px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">Amount Added</td>'
        f'<td align="right" style="padding:8px 0;font-size:16px;font-weight:bold;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">+{amount}</td>'
        "</tr><tr>"
        '<td style="padding:8px 0;border-top:1px solid #333333;font-size:14px;color:rgba(255,255,255,.78);font-family:Arial,Helvetica,sans-serif;">New Balance</td>'
        f'<td align="right" style="padding:8px 0;border-top:1px solid #333333;font-size:16px;font-weight:bold;color:#f5c542;font-family:Arial,Helvetica,sans-serif;">{balance}</td>'
        "</tr></table></td></tr></table>"
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
    """Send results expiration warning.

    time_remaining: human-readable string like "10 days", "7 days", "72 hours", "24 hours"
    expires_label: short label for subject like "10 Days", "7 Days", "72 Hours", "24 Hours"
    """
    body = (
        _p("Once expired, these results cannot be recovered. If you haven't already, please download your data now.")
        + _p(
            '<span style="font-size:14px;line-height:22px;color:rgba(255,255,255,.78);">'
            "We retain all research results for 180 days from the date they were generated.</span>"
        )
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


# ─── Convenience Wrappers for Expiration Reminders ────────────────────────────

def send_results_expiring_10_days(email: str, job_id: str):
    send_results_expiring(email, job_id, "10 days", "10 Days")


def send_results_expiring_7_days(email: str, job_id: str):
    send_results_expiring(email, job_id, "7 days", "7 Days")


def send_results_expiring_72_hours(email: str, job_id: str):
    send_results_expiring(email, job_id, "72 hours", "72 Hours")


def send_results_expiring_24_hours(email: str, job_id: str):
    send_results_expiring(email, job_id, "24 hours", "24 Hours")


# ─── Support Ticket Emails ────────────────────────────────────────────────────

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
