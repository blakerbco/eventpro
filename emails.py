"""
AUCTIONFINDER — Email Templates & Sending

All transactional emails use the Auction Intel branded template
and are sent via Resend API.
"""

import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Auction Intel Admin <admin@auctionintel.app>")
DOMAIN = os.environ.get("APP_DOMAIN", "http://localhost:5000")
LOGO_URL = "https://content.app-sources.com/s/18152366670198361/uploads/RC/dark-logo-1104068.png"


def _base_template(title: str, heading: str, body_html: str, cta_text: str = "", cta_url: str = "") -> str:
    """Wrap email content in the branded Auction Intel template."""
    cta_block = ""
    if cta_text and cta_url:
        cta_block = f"""
            <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin: 30px 0;">
                <tr>
                    <td align="center" style="border-radius: 4px;" bgcolor="#FFD700">
                        <a href="{cta_url}" target="_blank" style="font-size: 16px; font-family: Arial, sans-serif; color: #000000; text-decoration: none; border-radius: 4px; padding: 12px 25px; border: 1px solid #FFD700; display: inline-block; font-weight: bold;">{cta_text}</a>
                    </td>
                </tr>
            </table>"""

    return f"""<!DOCTYPE html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <!--[if mso]><style type="text/css">body, table, td {{font-family: Arial, Helvetica, sans-serif !important;}}</style><![endif]-->
</head>
<body style="margin: 0; padding: 0; background-color: #1a1a1a;">
    <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: #1a1a1a;">
        <tr>
            <td align="center" style="padding: 20px 0;">
                <table role="presentation" width="600" border="0" cellspacing="0" cellpadding="0">
                    <!-- Header -->
                    <tr>
                        <td align="left" style="padding: 20px; background-color: #000000;">
                            <img src="{LOGO_URL}" alt="Auction Intel" width="150" style="display: block;">
                        </td>
                    </tr>
                    <!-- Main Content -->
                    <tr>
                        <td style="background-color: #222222; padding: 40px 30px;">
                            <h1 style="color: #ffffff; font-family: Arial, sans-serif; margin: 0 0 20px 0;">{heading}</h1>
                            {body_html}
                            {cta_block}
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 20px; text-align: center; background-color: #222222; border-radius: 0 0 8px 8px;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; line-height: 20px; color: #888888; font-family: Arial, sans-serif;">
                                &copy; 2025 Auction Intel. All rights reserved.
                            </p>
                            <p style="margin: 0; font-size: 12px; line-height: 20px; color: #666666; font-family: Arial, sans-serif;">
                                7050 W 120th Ave Suite 50B<br>
                                Broomfield, CO 80020
                            </p>
                            <p style="margin: 20px 0 0 0; font-size: 12px; line-height: 20px; color: #666666; font-family: Arial, sans-serif;">
                                This is an automated message, please do not reply directly to this email.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body></html>"""


def _p(text: str) -> str:
    """Wrap text in a styled paragraph."""
    return f'<p style="color: #e0e0e0; font-family: Arial, sans-serif; font-size: 16px; line-height: 24px; margin: 0 0 16px 0;">{text}</p>'


def _send(to: str, subject: str, html: str):
    """Send email via Resend. Fails silently to avoid breaking app flow."""
    try:
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
    except Exception:
        pass


# ─── Email Functions ──────────────────────────────────────────────────────────

def send_welcome(email: str, is_trial: bool = False):
    """Send welcome email on registration."""
    trial_note = ""
    if is_trial:
        trial_note = _p(
            "Your free trial is active with <strong>$50.00 in credit</strong> "
            "— that's up to 150 nonprofit searches included. Your trial lasts 7 days, "
            "so dive in and start finding auction leads right away."
        )

    body = (
        _p("Welcome to Auction Intel — the fastest way to find nonprofit auction events and verified contact details.")
        + _p(
            "Your account is ready to go. Here's what you can do:"
        )
        + """
        <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin: 0 0 20px 0;">
            <tr><td style="padding: 6px 0; color: #e0e0e0; font-family: Arial, sans-serif; font-size: 15px;">&#x2022; &nbsp;Search our IRS nonprofit database with over 1.8 million organizations</td></tr>
            <tr><td style="padding: 6px 0; color: #e0e0e0; font-family: Arial, sans-serif; font-size: 15px;">&#x2022; &nbsp;Run AI-powered auction event research on any list of nonprofits</td></tr>
            <tr><td style="padding: 6px 0; color: #e0e0e0; font-family: Arial, sans-serif; font-size: 15px;">&#x2022; &nbsp;Get verified contacts, event dates, and auction details delivered as downloadable reports</td></tr>
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
        ),
    )


def send_password_reset(email: str, reset_url: str):
    """Send password reset link."""
    body = (
        _p("We received a request to reset the password for your Auction Intel account.")
        + _p("Click the button below to set a new password. This link will expire in <strong>15 minutes</strong>.")
        + _p(
            '<span style="color: #888888; font-size: 14px;">'
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
        ),
    )


def send_job_complete(email: str, job_id: str, nonprofit_count: int,
                      found_count: int, billable_count: int, total_cost_cents: int):
    """Send search job completion receipt."""
    total_cost = f"${total_cost_cents / 100:.2f}"

    body = (
        _p(f"Your auction research job <strong>{job_id[:8]}</strong> has finished processing.")
        + f"""
        <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin: 0 0 24px 0; width: 100%;">
            <tr>
                <td style="padding: 12px 16px; background-color: #1a1a1a; border-radius: 6px;">
                    <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width: 100%;">
                        <tr>
                            <td style="padding: 8px 0; color: #a0a0a0; font-family: Arial, sans-serif; font-size: 14px;">Nonprofits Searched</td>
                            <td align="right" style="padding: 8px 0; color: #ffffff; font-family: Arial, sans-serif; font-size: 14px; font-weight: bold;">{nonprofit_count}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #a0a0a0; font-family: Arial, sans-serif; font-size: 14px;">Auction Events Found</td>
                            <td align="right" style="padding: 8px 0; color: #ffffff; font-family: Arial, sans-serif; font-size: 14px; font-weight: bold;">{found_count}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #a0a0a0; font-family: Arial, sans-serif; font-size: 14px;">Billable Leads</td>
                            <td align="right" style="padding: 8px 0; color: #ffffff; font-family: Arial, sans-serif; font-size: 14px; font-weight: bold;">{billable_count}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; border-top: 1px solid #333333; color: #a0a0a0; font-family: Arial, sans-serif; font-size: 14px;">Total Charged</td>
                            <td align="right" style="padding: 8px 0; border-top: 1px solid #333333; color: #FFD700; font-family: Arial, sans-serif; font-size: 16px; font-weight: bold;">{total_cost}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
        """
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
        ),
    )


def send_funds_receipt(email: str, amount_cents: int, new_balance_cents: int):
    """Send receipt when user adds funds via Stripe."""
    amount = f"${amount_cents / 100:.2f}"
    balance = f"${new_balance_cents / 100:.2f}"

    body = (
        _p("Your wallet has been topped up successfully. Here are your transaction details:")
        + f"""
        <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin: 0 0 24px 0; width: 100%;">
            <tr>
                <td style="padding: 12px 16px; background-color: #1a1a1a; border-radius: 6px;">
                    <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width: 100%;">
                        <tr>
                            <td style="padding: 8px 0; color: #a0a0a0; font-family: Arial, sans-serif; font-size: 14px;">Amount Added</td>
                            <td align="right" style="padding: 8px 0; color: #4ade80; font-family: Arial, sans-serif; font-size: 16px; font-weight: bold;">+{amount}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; border-top: 1px solid #333333; color: #a0a0a0; font-family: Arial, sans-serif; font-size: 14px;">New Balance</td>
                            <td align="right" style="padding: 8px 0; border-top: 1px solid #333333; color: #FFD700; font-family: Arial, sans-serif; font-size: 16px; font-weight: bold;">{balance}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
        """
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
        ),
    )


def send_results_expiring(email: str, job_id: str, time_remaining: str, expires_label: str):
    """Send results expiration warning.

    time_remaining: human-readable string like "10 days", "7 days", "72 hours", "24 hours"
    expires_label: short label for subject like "10 Days", "7 Days", "72 Hours", "24 Hours"
    """
    urgency_color = "#f87171" if "hour" in time_remaining.lower() else "#FFD700"

    body = (
        _p(f"Your research results from job <strong>{job_id[:8]}</strong> will be permanently deleted in <strong style=\"color: {urgency_color};\">{time_remaining}</strong>.")
        + _p("Once expired, these results cannot be recovered. If you haven't already, please download your data now.")
        + _p(
            '<span style="color: #888888; font-size: 14px;">'
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
        _p(f"A new support ticket has been submitted by <strong>{user_email}</strong>.")
        + _p(f"<strong>Subject:</strong> {subject}")
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
        ),
    )


def send_ticket_reply_to_user(user_email: str, ticket_id: int, subject: str, reply_message: str):
    """Notify user when admin replies to their ticket."""
    body = (
        _p(f"You have a new reply on your support ticket <strong>#{ticket_id}</strong>.")
        + _p(f"<strong>Subject:</strong> {subject}")
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
        ),
    )
