"""
AUCTIONFINDER — Email Templates & Sending

All transactional emails use the Auction Intel professional branded template
and are sent via Resend API.
"""

import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Auction Intel Admin <admin@auctionintel.app>")
DOMAIN = os.environ.get("APP_DOMAIN", "http://localhost:5000")
LOGO_URL = "https://content.app-sources.com/s/18152366670198361/uploads/RC/5-2077317.svg"


# ─── Professional CSS (shared across all email templates) ────────────────────

_PRO_CSS = """
/* Reset */
.email_body,
.email_section table,
.email_section td,
.email_section a {
  -webkit-text-size-adjust: 100%;
  -ms-text-size-adjust: 100%; }

.email_section table,
.email_section td {
  mso-table-lspace: 0pt;
  mso-table-rspace: 0pt; }

.email_section a,
.email_section a span,
.content_section a:visited,
.content_section a:visited span,
.email_section address {
  text-decoration: none; }

.email_section img {
  -ms-interpolation-mode: bicubic;
  border: 0;
  height: auto;
  line-height: 100%;
  outline: none;
  text-decoration: none;
  -ms-interpolation-mode: bicubic; }

.email_body {
  height: 100% !important;
  margin: 0 !important;
  padding: 0 !important;
  width: 100% !important; }

.email_html,
.email_body {
  min-width: 100%; }

/* Grid: Email Wrapper */
.email_bg {
  font-size: 0;
  text-align: center;
  line-height: 100%; }

/* Grid: Email Section */
.content_section {
  max-width: 800px;
  Margin: 0 auto; }

.content_section_xs {
  max-width: 416px;
  Margin: 0 auto; }

/* Grid: Row */
.content_cell,
.column_row {
  font-size: 0;
  text-align: center; }

.column_row {
  max-width: 624px;
  Margin: 0 auto; }

.column_row:after {
  content: "";
  display: table;
  clear: both; }

/* Grid: Columns */
.column {
  vertical-align: top; }

.column_cell {
  vertical-align: top; }

.column_inline {
  width: auto;
  Margin: 0 auto;
  clear: both; }

/* Grid: Columns */
.col_1,
.col_2,
.col_3,
.col_50,
.col_order,
.col_order_xs {
  vertical-align: top;
  display: inline-block;
  width: 100%; }

.col_1 {
  max-width: 208px; }

.col_2 {
  max-width: 312px; }

.col_3 {
  max-width: 416px; }

.col_50 {
  max-width: 400px; }

.col_order {
  max-width: 156px; }

/* Typography: Fallback font family  */
.email_section a,
.email_section a:visited,
.email_section p,
.email_section li,
.email_section h1,
.email_section h2,
.email_section h3,
.email_section h4,
.email_section h5,
.email_section h6 {
  color: inherit;
  font-family: Arial, Helvetica, sans-serif;
  Margin-top: 0px;
  Margin-bottom: 0px;
  word-break: break-word; }

/* Typography: Default Sizes  */
.email_section p,
.email_section li {
  font-size: 16px;
  line-height: 26px; }

.email_section .text_lead {
  font-size: 19px;
  line-height: 31px; }

.email_section .text_xs {
  font-size: 14px;
  line-height: 22px; }

/* Typography: Headings  */
.email_section h1 {
  font-size: 38px;
  line-height: 48px; }

.email_section h2 {
  font-size: 28px;
  line-height: 38px; }

.email_section h3 {
  font-size: 21px;
  line-height: 28px; }

.email_section h4 {
  font-size: 18px;
  line-height: 24px; }

.email_section h5 {
  font-size: 16px;
  line-height: 21px; }

.email_section h6 {
  font-size: 12px;
  line-height: 14px;
  text-transform: uppercase;
  letter-spacing: 1px; }

/* Typography: Bold  */
.ebutton a,
.ebutton_xs a,
.email_section h1,
.email_section h2,
.email_section h3,
.email_section h4,
.email_section h5,
.email_section h6,
.email_section strong {
  font-weight: bold; }

/* Typography: Display  */
.column_cell .text_display {
  font-weight: lighter;
  color: inherit; }

/* Typography: Buttons  */
.ebutton td {
  font-size: 16px; }

.ebutton_xs td {
  font-size: 14px; }

/* Colors: Backgrounds */
.bg_primary {
  background-color: #2376dc; }

.bg_secondary {
  background-color: #e5e9ee; }

.email_html,
.email_body,
.bg_light {
  background-color: #d1deec; }

.bg_dark {
  background-color: #212121; }

.bg_white {
  background-color: #ffffff; }

.bg_facebook {
  background-color: #3664a2; }

.bg_twitter {
  background-color: #1a8bf0; }

.bg_instagram {
  background-color: #cf2896; }

.bg_youtube {
  background-color: #e62d28; }

.bg_google {
  background-color: #de5347; }

.bg_pinterest {
  background-color: #be2026; }

/* Colors: Text */
.text_primary,
.content_cell .text_primary,
.text_primary span,
.text_primary a {
  color: #2376dc; }

.text_secondary,
.content_cell .text_secondary,
.text_secondary span,
.text_secondary a {
  color: #959ba0; }

.text_light,
.content_cell .text_light,
.text_light span,
.text_light a {
  color: #d1deec; }

.text_dark,
.content_cell .text_dark,
.text_dark span,
.text_dark a {
  color: #333333; }

.text_white,
.content_cell .text_white,
.text_white span,
.text_white a,
.ebutton a,
.ebutton span,
.ebutton_xs a,
.ebutton_xs span {
  color: #ffffff; }

/* Colors: Border */
.bt_primary {
  border-top: 4px solid #2376dc; }

.bt_dark {
  border-top: 4px solid #212121; }

.bt_white {
  border-top: 4px solid #ffffff; }

.bb_white {
  border-bottom: 1px solid #ffffff; }

.bg_primary .bb_white,
.bg_dark .bb_white {
  opacity: .25; }

.bb_light {
  border-bottom: 1px solid #dee0e1; }

.bt_light {
  border-top: 1px solid #dee0e1; }

/* Colors: Shadow */
.shadow_primary,
.ebutton .bg_primary:hover,
.ebutton_xs .bg_primary:hover {
  -webkit-box-shadow: 0 0 12px 0 #2376dc;
  box-shadow: 0 0 12px 0 #2376dc; }

.shadow_dark,
.ebutton .bg_dark:hover,
.ebutton_xs .bg_dark:hover {
  -webkit-box-shadow: 0 0 12px 0 #212121;
  box-shadow: 0 0 12px 0 #212121; }

/* Backgrounds: Image Default */
.bg_center {
  background-repeat: no-repeat;
  background-position: 50% 0; }

/* Extras: Email Summary */
.email_summary {
  display: none;
  font-size: 1px;
  line-height: 1px;
  max-height: 0px;
  max-width: 0px;
  opacity: 0;
  overflow: hidden; }

/* Rounded Corners */
.brounded_top {
  border-radius: 4px 4px 0 0; }

.brounded_bottom {
  border-radius: 0 0 4px 4px; }

.brounded_left {
  border-radius: 0 0 0 4px; }

.brounded_right {
  border-radius: 0 0 4px 0; }

.brounded,
.ebutton td,
.ebutton_xs td {
  border-radius: 4px; }

.brounded_circle {
  border-radius: 50%; }

/* Text: Alignment */
.text_center {
  text-align: center; }

.text_center .imgr img {
  margin-left: auto;
  margin-right: auto; }

.text_left {
  text-align: left; }

.text_right {
  text-align: right; }

/* Text: Links */
.text_del {
  text-decoration: line-through; }

.text_link a {
  text-decoration: underline; }

.text_link .text_adr {
  text-decoration: none; }

/* Padding Utilities */
.py {
  padding-top: 16px;
  padding-bottom: 16px; }

.py_xs {
  padding-top: 8px;
  padding-bottom: 8px; }

.py_md {
  padding-top: 32px;
  padding-bottom: 32px; }

.py_lg {
  padding-top: 64px;
  padding-bottom: 64px; }

.px {
  padding-left: 16px;
  padding-right: 16px; }

.px_xs {
  padding-left: 8px;
  padding-right: 8px; }

.px_md {
  padding-left: 32px;
  padding-right: 32px; }

.px_lg {
  padding-left: 64px;
  padding-right: 64px; }

.pt {
  padding-top: 16px; }

.pt_xs {
  padding-top: 8px; }

.pt_md {
  padding-top: 32px; }

.pt_lg {
  padding-top: 64px; }

.pb {
  padding-bottom: 16px; }

.pb_xs {
  padding-bottom: 8px; }

.pb_md {
  padding-bottom: 32px; }

.pb_lg {
  padding-bottom: 64px; }

.pl {
  padding-left: 16px; }

.pl_xs {
  padding-left: 8px; }

.pl_lg {
  padding-left: 32px; }

.pr {
  padding-right: 16px; }

.pr_xs {
  padding-right: 8px; }

.pr_lg {
  padding-right: 32px; }

/* Margin Utilities */
.content_cell .mt_0 {
  margin-top: 0px; }

.content_cell .mt_xs {
  margin-top: 8px; }

.content_cell .mt {
  margin-top: 16px; }

.content_cell .mt_md {
  margin-top: 32px; }

.content_cell .mt_lg {
  margin-top: 64px; }

.content_cell .mb_0 {
  margin-bottom: 0px; }

.content_cell .mb_xs {
  margin-bottom: 8px; }

.content_cell .mb {
  margin-bottom: 16px; }

.content_cell .mb_md {
  margin-bottom: 32px; }

.content_cell .mb_lg {
  margin-bottom: 64px; }

/* Images */
.content_cell .img_inline,
.content_cell .img_full {
  clear: both;
  line-height: 100%; }

.content_cell .img_full {
  font-size: 0 !important; }

.img_full img {
  display: block;
  width: 100%;
  Margin: 0px auto;
  height: auto; }

/* Buttons */
.ebutton td,
.ebutton_xs td {
  line-height: normal;
  text-align: center;
  font-weight: bold;
  -webkit-transition: box-shadow .25s;
  transition: box-shadow .25s; }

.ebutton[align=center],
.ebutton_xs[align=center] {
  margin: 0 auto; }

.ebutton td {
  padding: 13px 24px; }

.ebutton_xs td {
  padding: 10px 20px; }

@media only screen {
  /* Web fonts (latin only) */
  @font-face {
    font-family: 'Open Sans';
    font-style: normal;
    font-weight: 300;
    src: local("Open Sans Light"), local("OpenSans-Light"), url(https://fonts.gstatic.com/s/opensans/v14/DXI1ORHCpsQm3Vp6mXoaTegdm0LZdjqr5-oayXSOefg.woff2) format("woff2");
    unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02C6, U+02DA, U+02DC, U+2000-206F, U+2074, U+20AC, U+2212, U+2215; }
  @font-face {
    font-family: 'Open Sans';
    font-style: normal;
    font-weight: 400;
    src: local("Open Sans Regular"), local("OpenSans-Regular"), url(https://fonts.gstatic.com/s/opensans/v14/cJZKeOuBrn4kERxqtaUH3VtXRa8TVwTICgirnJhmVJw.woff2) format("woff2");
    unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02C6, U+02DA, U+02DC, U+2000-206F, U+2074, U+20AC, U+2212, U+2215; }
  @font-face {
    font-family: 'Open Sans';
    font-style: normal;
    font-weight: 700;
    src: local("Open Sans Bold"), local("OpenSans-Bold"), url(https://fonts.gstatic.com/s/opensans/v14/k3k702ZOKiLJc3WVjuplzOgdm0LZdjqr5-oayXSOefg.woff2) format("woff2");
    unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02C6, U+02DA, U+02DC, U+2000-206F, U+2074, U+20AC, U+2212, U+2215; }
  .column_cell a,
  .column_cell p,
  .column_cell li,
  .column_cell h1,
  .column_cell h2,
  .column_cell h3,
  .column_cell h4,
  .column_cell h5,
  .column_cell h6 {
    font-family: "Open Sans", sans-serif !important; }
  .column_cell .text_display {
    font-weight: 300 !important; }
  .column_cell p,
  .column_cell li {
    font-weight: 400 !important; }
  .ebutton a,
  .ebutton span,
  .ebutton_xs a,
  .ebutton_xs span,
  .column_cell h1,
  .column_cell h2,
  .column_cell h3,
  .column_cell h4,
  .column_cell h5,
  .column_cell h6,
  .column_cell strong {
    font-weight: 700 !important; }
  /* Button Enhancement */
  .ebutton td,
  .ebutton_xs td {
    padding: 0 !important; }
  .ebutton a {
    padding: 13px 24px !important; }
  .ebutton_xs a {
    padding: 10px 20px !important; }
  .ebutton a,
  .ebutton_xs a {
    display: block !important; }
  /* Grid Fix */
  .col_50 {
    max-width: 50% !important;
    float: left; } }

@media (max-width: 689px) {
  /* Columns */
  .col_1,
  .col_2,
  .col_3,
  .col_50,
  .col_order {
    display: block !important;
    max-width: 100% !important; }
  /* Content Alignment */
  .mobile_center {
    text-align: center !important; }
  .mobile_center .ebutton,
  .mobile_center .ebutton_xs,
  .mobile_center .column_inline {
    float: none !important;
    margin: 0 auto !important;
    width: auto !important;
    min-width: 0 !important;
    display: inline-block !important; }
  /* Mobile Padding */
  .mob_py {
    padding-top: 16px !important;
    padding-bottom: 16px !important; }
  .mob_py_0 {
    padding-top: 0 !important;
    padding-bottom: 0 !important; }
  .mob_py_md {
    padding-top: 32px !important;
    padding-bottom: 32px !important; }
  .mob_pt_0 {
    padding-top: 0 !important; }
  .mob_pt {
    padding-top: 16px !important; }
  .mob_pb {
    padding-bottom: 16px !important; }
  .mobile_hide {
    max-height: 0 !important;
    display: none !important;
    mso-hide: all !important;
    overflow: hidden !important;
    font-size: 0 !important; }
  /* Rounded Corners */
  .brounded_left {
    border-radius: 0 !important; }
  .brounded_right {
    border-radius: 0 0 4px 4px !important; }
  /* Images */
  .img_inline img {
    width: 100% !important;
    height: auto !important; } }


/* ===== Auction Intel Email Theme ===== */
.email_html, .email_body, .email_bg, .bg_light { background-color:#f5f5f0 !important; }
.bg_white { background-color:#000000 !important; }

.text_dark, .content_cell .text_dark, .text_dark span { color:#ffffff !important; }
.text_secondary, .content_cell .text_secondary, .text_secondary span { color:rgba(255,255,255,.78) !important; }

.bt_primary { border-top: 4px solid #f5c542 !important; }
.text_primary, .content_cell .text_primary, .text_primary span, .text_primary a { color:#f5c542 !important; }

.bg_primary { background-color:#f5c542 !important; }
.text_white, .content_cell .text_white, .text_white span, .text_white a { color:#000000 !important; }

.bb_light { border-bottom: 1px solid rgba(255,255,255,.18) !important; }
.bt_light { border-top: 1px solid rgba(255,255,255,.18) !important; }

.brounded_top { border-radius:16px 16px 0 0 !important; }
.brounded_bottom { border-radius:0 0 16px 16px !important; }

a { color:#f5c542; }
"""


# ─── Template & Helpers ──────────────────────────────────────────────────────

def _base_template(title: str, heading: str, body_html: str,
                   cta_text: str = "", cta_url: str = "",
                   summary: str = "") -> str:
    """Wrap email content in the professional Auction Intel template."""
    cta_block = ""
    if cta_text and cta_url:
        cta_block = (
            '<table class="ebutton" role="presentation" align="left" cellspacing="0" cellpadding="0" border="0" style="Margin:24px 0 10px 0;">'
            "<tbody><tr>"
            '<td class="bg_primary text_white" style="border-radius:12px;">'
            f'<a href="{cta_url}" target="_blank" style="display:inline-block; font-weight:800;">{cta_text}</a>'
            "</td></tr></tbody></table>"
            '<p class="text_xs text_secondary" style="Margin:0; clear:both;">'
            f'If the button doesn\'t work, open: <a href="{cta_url}" class="text_primary" style="text-decoration:underline;">{cta_url}</a>'
            "</p>"
        )

    summary_div = ""
    lead_block = ""
    if summary:
        summary_div = f'<div class="email_summary">{summary}</div>'
        lead_block = f'<p class="text_lead text_secondary mb" style="Margin-bottom:16px;">{summary}</p>'

    return (
        '<!doctype html>'
        '<html class="email_html" lang="en" style="min-width:100%;background-color:#f5f5f0;">'
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
        '<body class="email_body" style="-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;min-width:100%;background-color:#f5f5f0;height:100% !important;margin:0 !important;padding:0 !important;width:100% !important;">'
        + summary_div +

        # ── Header ──
        '<table class="email_section" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="email_bg bg_light px pt_md" style="font-size:0;text-align:center;line-height:100%;">'
        '<table class="content_section" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="content_cell bg_white brounded_top bt_primary px py" style="font-size:0;text-align:center;">'
        '<div class="column_row" style="font-size:0;text-align:center;max-width:624px;Margin:0 auto;">'

        # Logo
        '<div class="col_50" style="vertical-align:top;display:inline-block;width:100%;max-width:400px;">'
        '<table class="column" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="column_cell px py_xs text_left mobile_center">'
        '<p class="img_inline" style="line-height:100%;clear:both;Margin:0;">'
        f'<a href="https://auctionintel.app" target="_blank"><img src="{LOGO_URL}" width="140" alt="Auction Intel" style="max-width:140px;"></a>'
        "</p></td></tr></tbody></table></div>"

        # Notification label
        '<div class="col_50" style="vertical-align:top;display:inline-block;width:100%;max-width:400px;">'
        '<table class="column" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="column_cell px py_xs text_right mobile_center text_secondary">'
        '<p class="text_xs text_secondary" style="Margin:0;">Auction Intel Notification</p>'
        "</td></tr></tbody></table></div>"

        "</div></td></tr></tbody></table></td></tr></tbody></table>"

        # ── Body ──
        '<table class="email_section" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="email_bg bg_light px" style="font-size:0;text-align:center;line-height:100%;">'
        '<table class="content_section" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="content_cell bg_white px py_md" style="font-size:0;text-align:center;">'
        '<div class="column_row" style="font-size:0;text-align:center;max-width:624px;Margin:0 auto;">'
        '<table class="column" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="column_cell px text_left text_dark">'
        f'<h2 class="mb_xs" style="Margin-bottom:8px;">{heading}</h2>'
        + lead_block
        + body_html
        + cta_block +
        "</td></tr></tbody></table></div></td></tr>"

        # ── Footer ──
        "<tr>"
        '<td class="content_cell bg_white brounded_bottom px py_md bt_light" style="font-size:0;text-align:center;">'
        '<div class="column_row" style="font-size:0;text-align:center;max-width:624px;Margin:0 auto;">'
        '<table class="column" role="presentation" align="center" width="100%" cellspacing="0" cellpadding="0" border="0"><tbody><tr>'
        '<td class="column_cell px text_center text_secondary">'
        '<p class="text_xs text_secondary mb_xs" style="Margin-bottom:8px;">&copy; 2026 Auction Intel. All rights reserved.</p>'
        '<p class="text_xs text_secondary mb_xs" style="Margin-bottom:8px;">7050 W 120th Ave Suite 50B<br>Broomfield, CO 80020</p>'
        '<p class="text_xs text_secondary" style="Margin-bottom:0;">'
        'Support: <a href="mailto:support@auctionintel.us" class="text_primary" style="text-decoration:underline;">support@auctionintel.us</a>'
        " &nbsp;&bull;&nbsp; 303-719-4851</p>"
        '<p class="text_xs text_secondary mt" style="Margin-top:16px;">This is an automated message. Please do not reply directly to this email.</p>'
        "</td></tr></tbody></table></div></td></tr>"

        "</tbody></table></td></tr></tbody></table>"
        "</body></html>"
    )


def _p(text: str) -> str:
    """Wrap text in a styled paragraph (CSS classes handle font/color)."""
    return f'<p class="mb" style="Margin-bottom:16px;">{text}</p>'


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
            <tr><td style="padding:6px 0; font-size:15px;">&#x2022; &nbsp;Search our IRS nonprofit database with over 1.8 million organizations</td></tr>
            <tr><td style="padding:6px 0; font-size:15px;">&#x2022; &nbsp;Run AI-powered auction event research on any list of nonprofits</td></tr>
            <tr><td style="padding:6px 0; font-size:15px;">&#x2022; &nbsp;Get verified contacts, event dates, and auction details delivered as downloadable reports</td></tr>
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
            '<span class="text_xs text_secondary">'
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
            '<span class="text_xs text_secondary">'
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
        """
        <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 24px 0; width:100%;">
            <tr>
                <td style="padding:12px 16px; border-radius:6px;">
                    <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width:100%;">
                        <tr>
                            <td style="padding:8px 0; font-size:14px;">Nonprofits Searched</td>
                            <td align="right" style="padding:8px 0; font-size:14px; font-weight:bold;">"""
        + str(nonprofit_count)
        + """</td>
                        </tr>
                        <tr>
                            <td style="padding:8px 0; font-size:14px;">Auction Events Found</td>
                            <td align="right" style="padding:8px 0; font-size:14px; font-weight:bold;">"""
        + str(found_count)
        + """</td>
                        </tr>
                        <tr>
                            <td style="padding:8px 0; font-size:14px;">Billable Leads</td>
                            <td align="right" style="padding:8px 0; font-size:14px; font-weight:bold;">"""
        + str(billable_count)
        + """</td>
                        </tr>
                        <tr>
                            <td style="padding:8px 0; border-top:1px solid #333333; font-size:14px;">Total Charged</td>
                            <td align="right" class="text_primary" style="padding:8px 0; border-top:1px solid #333333; font-size:16px; font-weight:bold;">"""
        + total_cost
        + """</td>
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
            summary=f"Your auction research job {job_id[:8]} has finished processing.",
        ),
    )


def send_funds_receipt(email: str, amount_cents: int, new_balance_cents: int):
    """Send receipt when user adds funds via Stripe."""
    amount = f"${amount_cents / 100:.2f}"
    balance = f"${new_balance_cents / 100:.2f}"

    body = (
        """
        <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 0 24px 0; width:100%;">
            <tr>
                <td style="padding:12px 16px; border-radius:6px;">
                    <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="width:100%;">
                        <tr>
                            <td style="padding:8px 0; font-size:14px;">Amount Added</td>
                            <td align="right" style="padding:8px 0; font-size:16px; font-weight:bold;">+"""
        + amount
        + """</td>
                        </tr>
                        <tr>
                            <td style="padding:8px 0; border-top:1px solid #333333; font-size:14px;">New Balance</td>
                            <td align="right" class="text_primary" style="padding:8px 0; border-top:1px solid #333333; font-size:16px; font-weight:bold;">"""
        + balance
        + """</td>
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
            '<span class="text_xs text_secondary">'
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
