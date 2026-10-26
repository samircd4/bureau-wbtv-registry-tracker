#!/usr/bin/env python3
"""
notification.py

Email notification module for Bureau Wbtv Registry Monitor.
Provides functions to construct beautifully formatted (plain-text and HTML)
email alerts and send them via SMTP (e.g. Gmail App Password).
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from fetch_all_languages import extract_record_csv_fields

# Load .env file from project root
base_dir = Path(__file__).resolve().parent
load_dotenv(base_dir / ".env")

logger = logging.getLogger("notification")


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:.1f}s"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h {remaining_minutes}m {int(remaining_seconds)}s"


def send_email_notification(
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    recipients: Optional[list[str] | str] = None,
    sender: Optional[str] = None,
    password: Optional[str] = None,
    smtp_server: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> bool:
    """
    Sends an email notification via SMTP_SSL.

    Parameters:
        subject: Email subject line.
        body: Plain-text body content.
        html_body: Optional rich HTML body content.
        recipients: Target email address(es) as a list or comma-separated string.
                    Defaults to RECIPIENT_EMAIL or EMAIL_RECEIVER from .env.
        sender: Sender email address.
                Defaults to SENDER_EMAIL or EMAIL_SENDER from .env.
        password: App password for the sender account.
                  Defaults to EMAIL_PASSWORD from .env.
        smtp_server: SMTP server host (default: smtp.gmail.com).
        smtp_port: SSL port (default: 465).

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    sender_addr = (
        sender
        or os.getenv("SENDER_EMAIL")
        or os.getenv("EMAIL_SENDER")
        or ""
    ).strip()

    pass_val = (
        password
        or os.getenv("EMAIL_PASSWORD")
        or ""
    ).strip()

    # Normalize recipients
    if recipients is None:
        recipients_raw = (
            os.getenv("RECIPIENT_EMAIL")
            or os.getenv("EMAIL_RECEIVER")
            or ""
        )
    elif isinstance(recipients, str):
        recipients_raw = recipients
    else:
        recipients_raw = ",".join(recipients)

    recipient_list = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    if not sender_addr or not pass_val or not recipient_list:
        logger.warning(
            "Email notification aborted: missing credentials (sender='%s', recipients=%s, password_configured=%s).",
            sender_addr or "<empty>",
            recipient_list or "<empty>",
            bool(pass_val),
        )
        return False

    em = EmailMessage()
    em["From"] = sender_addr
    em["To"] = ", ".join(recipient_list)
    em["Subject"] = subject
    em.set_content(body)

    if html_body:
        em.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as smtp:
            smtp.login(sender_addr, pass_val)
            smtp.send_message(em)
        logger.info(
            "Email notification successfully sent to %s with subject '%s'.",
            ", ".join(recipient_list),
            subject,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send email notification to %s: %s", ", ".join(recipient_list), exc)
        return False


def format_email_alert(
    new_items: list[tuple[str, dict[str, Any]]],
    elapsed_seconds: Optional[float] = None,
) -> tuple[str, str, str]:
    """
    Formats detected new registrations into:
    1. A clear subject line.
    2. A clean plain-text body.
    3. A modern, responsive HTML body.

    Returns:
        tuple (subject, plain_text_body, html_body)
    """
    total = len(new_items)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_str = f"Scan duration: {format_duration(elapsed_seconds)}" if elapsed_seconds is not None else ""

    # Subject line
    if total == 1:
        lang_name, record = new_items[0]
        fields = extract_record_csv_fields(record, language_name=lang_name)
        name = fields.get("Name") or "Professional"
        role = fields.get("Role") or "Tolk"
        subject = f"[Bureau Wbtv Alert] New Registration: {name} ({lang_name} - {role})"
    else:
        subject = f"[Bureau Wbtv Alert] {total} New Registration(s) Detected"

    # Plain text body
    text_lines: list[str] = [
        "=" * 60,
        "BUREAU WBTV: NEW REGISTRATION ALERT",
        "=" * 60,
        f"Detected {total} new registration(s).",
        f"Time: {now_str}" + (f" | {scan_str}" if scan_str else ""),
        "",
    ]

    if total <= 5:
        for idx, (lang_name, record) in enumerate(new_items, start=1):
            fields = extract_record_csv_fields(record, language_name=lang_name)
            name = fields.get("Name") or "Unknown Name"
            role = fields.get("Role") or "Unknown Role"
            level = fields.get("Level") or "-"
            wbtv = fields.get("Wbtv Number") or "-"
            city = fields.get("City") or "-"
            email = fields.get("Email") or "-"
            phone = fields.get("Telephone 1") or "-"

            text_lines.append(f"{idx}. {name}")
            text_lines.append(f"   Language: {lang_name}")
            text_lines.append(f"   Role:     {role} (Level: {level})")
            text_lines.append(f"   Wbtv No:  {wbtv}")
            text_lines.append(f"   City:     {city}")
            text_lines.append(f"   Email:    {email}")
            text_lines.append(f"   Phone:    {phone}")
            text_lines.append("")
    else:
        lang_counts: dict[str, int] = {}
        for lang_name, _ in new_items:
            lang_counts[lang_name] = lang_counts.get(lang_name, 0) + 1

        text_lines.append("Breakdown by Language:")
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
            text_lines.append(f"  - {lang}: {count} new registration(s)")
        text_lines.append("")

        text_lines.append("Registered Professionals:")
        max_preview = 15
        for idx, (lang_name, record) in enumerate(new_items[:max_preview], start=1):
            fields = extract_record_csv_fields(record, language_name=lang_name)
            name = fields.get("Name") or "Unknown Name"
            role = fields.get("Role") or "Tolk"
            level = fields.get("Level") or ""
            level_str = f" ({level})" if level and level != "-" else ""
            wbtv = fields.get("Wbtv Number") or ""
            wbtv_str = f" [Wbtv: {wbtv}]" if wbtv and wbtv != "-" else ""
            text_lines.append(f"  {idx}. {name} - {lang_name} ({role}{level_str}){wbtv_str}")

        if total > max_preview:
            text_lines.append(f"  ... and {total - max_preview} more.")
        text_lines.append("")
        text_lines.append("Note: Full contact details (email, phone, address) saved in all_languages_results.xlsx.")

    text_lines.append("-" * 60)
    text_lines.append("This is an automated notification from Bureau Wbtv Registry Monitor.")
    plain_text_body = "\n".join(text_lines)

    # HTML body
    if total <= 5:
        cards_html: list[str] = []
        for idx, (lang_name, record) in enumerate(new_items, start=1):
            fields = extract_record_csv_fields(record, language_name=lang_name)
            name = fields.get("Name") or "Unknown Name"
            role = fields.get("Role") or "Tolk"
            level = fields.get("Level") or "-"
            wbtv = fields.get("Wbtv Number") or "-"
            city = fields.get("City") or "-"
            email = fields.get("Email") or "-"
            phone = fields.get("Telephone 1") or "-"

            email_display = (
                f'<a href="mailto:{email}" style="color: #2563eb; text-decoration: none;">{email}</a>'
                if email and email != "-"
                else '<span style="color: #94a3b8;">-</span>'
            )
            phone_display = (
                f'<a href="tel:{phone}" style="color: #2563eb; text-decoration: none;">{phone}</a>'
                if phone and phone != "-"
                else '<span style="color: #94a3b8;">-</span>'
            )

            role_badge_bg = "#dbeafe" if "tolk" in role.lower() else "#dcfce7"
            role_badge_fg = "#1e40af" if "tolk" in role.lower() else "#166534"

            card = f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px 20px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid #f1f5f9; padding-bottom: 8px;">
                <span style="font-size: 16px; font-weight: bold; color: #0f172a;">#{idx}. {name}</span>
                <div>
                  <span style="background-color: {role_badge_bg}; color: {role_badge_fg}; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 9999px; text-transform: uppercase;">{role}</span>
                  <span style="background-color: #f1f5f9; color: #475569; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 9999px; margin-left: 4px;">{level}</span>
                </div>
              </div>
              <table style="width: 100%; border-collapse: collapse; font-size: 13px; color: #334155;">
                <tr>
                  <td style="padding: 4px 0; width: 120px; font-weight: 600; color: #64748b;">Language:</td>
                  <td style="padding: 4px 0; font-weight: 600; color: #0f172a;">{lang_name}</td>
                </tr>
                <tr>
                  <td style="padding: 4px 0; font-weight: 600; color: #64748b;">Wbtv Number:</td>
                  <td style="padding: 4px 0;">{wbtv}</td>
                </tr>
                <tr>
                  <td style="padding: 4px 0; font-weight: 600; color: #64748b;">City / Region:</td>
                  <td style="padding: 4px 0;">{city}</td>
                </tr>
                <tr>
                  <td style="padding: 4px 0; font-weight: 600; color: #64748b;">Email Address:</td>
                  <td style="padding: 4px 0;">{email_display}</td>
                </tr>
                <tr>
                  <td style="padding: 4px 0; font-weight: 600; color: #64748b;">Phone Number:</td>
                  <td style="padding: 4px 0;">{phone_display}</td>
                </tr>
              </table>
            </div>
            """
            cards_html.append(card)
        records_content = "".join(cards_html)
    else:
        # Summary Overview for > 5 records
        lang_counts = {}
        for lang_name, _ in new_items:
            lang_counts[lang_name] = lang_counts.get(lang_name, 0) + 1

        lang_rows = "".join(
            f'<tr><td style="padding: 8px 12px; border-bottom: 1px solid #f1f5f9; font-weight: 600; color: #0f172a;">{l}</td>'
            f'<td style="padding: 8px 12px; border-bottom: 1px solid #f1f5f9; text-align: right; font-weight: bold; color: #2563eb;">{c}</td></tr>'
            for l, c in sorted(lang_counts.items(), key=lambda x: -x[1])
        )

        max_preview = 15
        preview_rows = "".join(
            f'<tr><td style="padding: 6px 12px; border-bottom: 1px solid #f1f5f9; color: #64748b;">{i}</td>'
            f'<td style="padding: 6px 12px; border-bottom: 1px solid #f1f5f9; font-weight: 600; color: #0f172a;">{extract_record_csv_fields(rec, language_name=l).get("Name") or "Unknown"}</td>'
            f'<td style="padding: 6px 12px; border-bottom: 1px solid #f1f5f9; color: #334155;">{l}</td>'
            f'<td style="padding: 6px 12px; border-bottom: 1px solid #f1f5f9; color: #475569;">{rec.get("role") or "Tolk"}</td></tr>'
            for i, (l, rec) in enumerate(new_items[:max_preview], start=1)
        )

        more_note = (
            f'<p style="color: #64748b; font-size: 12px; margin-top: 8px;">... and {total - max_preview} more records.</p>'
            if total > max_preview
            else ""
        )

        records_content = f"""
        <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px 20px; margin-bottom: 16px;">
          <h3 style="margin-top: 0; margin-bottom: 12px; color: #0f172a; font-size: 15px; font-weight: 700;">Breakdown by Language</h3>
          <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <thead>
              <tr style="background-color: #f8fafc;">
                <th style="padding: 8px 12px; text-align: left; color: #475569; font-weight: 600; border-bottom: 2px solid #e2e8f0;">Language</th>
                <th style="padding: 8px 12px; text-align: right; color: #475569; font-weight: 600; border-bottom: 2px solid #e2e8f0;">New Records</th>
              </tr>
            </thead>
            <tbody>
              {lang_rows}
            </tbody>
          </table>
        </div>

        <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px 20px; margin-bottom: 16px;">
          <h3 style="margin-top: 0; margin-bottom: 12px; color: #0f172a; font-size: 15px; font-weight: 700;">Registered Professionals (Preview)</h3>
          <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
            <thead>
              <tr style="background-color: #f8fafc;">
                <th style="padding: 6px 12px; text-align: left; color: #475569; border-bottom: 2px solid #e2e8f0;">#</th>
                <th style="padding: 6px 12px; text-align: left; color: #475569; border-bottom: 2px solid #e2e8f0;">Name</th>
                <th style="padding: 6px 12px; text-align: left; color: #475569; border-bottom: 2px solid #e2e8f0;">Language</th>
                <th style="padding: 6px 12px; text-align: left; color: #475569; border-bottom: 2px solid #e2e8f0;">Role</th>
              </tr>
            </thead>
            <tbody>
              {preview_rows}
            </tbody>
          </table>
          {more_note}
        </div>

        <div style="background-color: #eff6ff; border-left: 4px solid #3b82f6; padding: 12px 16px; border-radius: 4px; margin-bottom: 16px;">
          <p style="margin: 0; font-size: 13px; color: #1e40af;">
            <strong>Note:</strong> Full contact details (email, phone, address, website) have been saved and enriched in <code>all_languages_results.xlsx</code>.
          </p>
        </div>
        """

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="margin: 0; padding: 20px; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 640px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05); border: 1px solid #e2e8f0;">
    
    <!-- Top Header Banner -->
    <div style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); padding: 24px 28px; color: #ffffff;">
      <div style="display: inline-block; background-color: rgba(239, 68, 68, 0.2); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5; font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 9999px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px;">
        Registry Alert
      </div>
      <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.2px;">
        Bureau Wbtv: New Registration Detected
      </h1>
      <p style="margin: 6px 0 0 0; font-size: 13px; color: #94a3b8;">
        Detected <strong>{total}</strong> new record(s) on zoekeentolkvertaler.bureauwbtv.nl
      </p>
    </div>

    <!-- Metadata Bar -->
    <div style="background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; padding: 12px 28px; display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: #64748b;">
      <div><strong>Detected at:</strong> {now_str}</div>
      {f'<div>&bull;</div><div><strong>Duration:</strong> {format_duration(elapsed_seconds)}</div>' if elapsed_seconds is not None else ''}
    </div>

    <!-- Main Content Area -->
    <div style="padding: 24px 28px; background-color: #f8fafc;">
      {records_content}
    </div>

    <!-- Footer -->
    <div style="background-color: #ffffff; border-top: 1px solid #e2e8f0; padding: 16px 28px; text-align: center; font-size: 12px; color: #94a3b8;">
      <p style="margin: 0;">Automated notification from <strong>Bureau Wbtv Registry Tracker</strong>.</p>
      <p style="margin: 4px 0 0 0; font-size: 11px; color: #cbd5e1;">Continuous Interpreter & Translator Monitoring System</p>
    </div>

  </div>
</body>
</html>
"""

    return subject, plain_text_body, html_body


if __name__ == "__main__":
    import sys

    print("\nTesting email notification from notification.py...")
    sender = os.getenv("SENDER_EMAIL") or os.getenv("EMAIL_SENDER")
    recipient = os.getenv("RECIPIENT_EMAIL") or os.getenv("EMAIL_RECEIVER")
    password = os.getenv("EMAIL_PASSWORD")

    print(f"Sender:    {sender or '<Not set>'}")
    print(f"Recipient: {recipient or '<Not set>'}")
    print(f"Password:  {'*' * 8 if password else '<Not set>'}")

    if not sender or not recipient or not password:
        print("\nError: Incomplete email configuration in .env file.")
        print("Please check SENDER_EMAIL, EMAIL_PASSWORD, and RECIPIENT_EMAIL.")
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = "[Bureau Wbtv] Test Email Alert"
    body = (
        "Hello!\n\n"
        "This is a test notification from the Bureau Wbtv Registry Tracker.\n"
        "Your email alert configuration is functioning properly.\n\n"
        f"Timestamp: {now}"
    )
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
      <h2 style="color: #0f172a; margin-top: 0;">&check; Bureau Wbtv Email Alert Connected</h2>
      <p style="color: #334155;">Your SMTP email configuration is functioning properly.</p>
      <p style="color: #64748b; font-size: 12px;">Sent at: {now}</p>
    </div>
    """

    success = send_email_notification(subject=subject, body=body, html_body=html)
    if success:
        print("Success! Test email was successfully sent.")
    else:
        print("Failed to send test email. Check log and credentials.")
        sys.exit(1)