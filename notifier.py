# notifier.py
"""
Multi-channel notification sender.

Channels
--------
- Telegram (HTTP Bot API, no external dep beyond `requests`)
- Email (smtplib stdlib, supports TLS)
- Dashboard (just writes to alert_log so it appears in the UI)

Credentials
-----------
Read from environment variables (preferred for Streamlit Cloud Secrets):

  TELEGRAM_BOT_TOKEN     — from @BotFather
  TELEGRAM_CHAT_ID       — your user/chat ID (use @userinfobot to find it)
  ALERT_SMTP_HOST        — e.g. smtp.gmail.com
  ALERT_SMTP_PORT        — e.g. 587
  ALERT_SMTP_USER        — sender email
  ALERT_SMTP_PASSWORD    — app password
  ALERT_SMTP_FROM        — From: header (defaults to ALERT_SMTP_USER)

Each send_* function returns (success: bool, error: str | None).
"""

from __future__ import annotations
import os
import smtplib
import ssl
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

from db import myt_iso, connect
from logger import get_logger

log = get_logger("notifier")

# --------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------- #

def _telegram_creds() -> tuple[str | None, str | None]:
    return (os.environ.get("TELEGRAM_BOT_TOKEN"),
            os.environ.get("TELEGRAM_CHAT_ID"))


def telegram_configured() -> bool:
    tok, chat = _telegram_creds()
    return bool(tok and chat)


def send_telegram(message: str, parse_mode: str = "HTML",
                  disable_preview: bool = True,
                  timeout: int = 10) -> tuple[bool, str | None]:
    """Send a Telegram message. Returns (success, error)."""
    token, chat_id = _telegram_creds()
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id, "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }, timeout=timeout)
        if r.status_code == 200 and r.json().get("ok"):
            return True, None
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.exceptions.RequestException as e:
        return False, f"network error: {e}"


# --------------------------------------------------------------------- #
# Email (SMTP)
# --------------------------------------------------------------------- #

def _smtp_creds() -> dict:
    return {
        "host": os.environ.get("ALERT_SMTP_HOST"),
        "port": int(os.environ.get("ALERT_SMTP_PORT", "587")),
        "user": os.environ.get("ALERT_SMTP_USER"),
        "password": os.environ.get("ALERT_SMTP_PASSWORD"),
        "from": os.environ.get("ALERT_SMTP_FROM") or
                os.environ.get("ALERT_SMTP_USER"),
    }


def email_configured() -> bool:
    c = _smtp_creds()
    return all([c["host"], c["user"], c["password"]])


def send_email(subject: str, body: str, recipients: list[str],
               html: bool = False, timeout: int = 15) -> tuple[bool, str | None]:
    """Send an email via SMTP/TLS. Returns (success, error)."""
    c = _smtp_creds()
    if not email_configured():
        return False, "ALERT_SMTP_* env vars not set"
    if not recipients:
        return False, "no recipients provided"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = c["from"]
    msg["To"] = ", ".join(recipients)
    mime_type = "html" if html else "plain"
    msg.attach(MIMEText(body, mime_type))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(c["host"], c["port"], timeout=timeout) as srv:
            srv.starttls(context=ctx)
            srv.login(c["user"], c["password"])
            srv.sendmail(c["from"], recipients, msg.as_string())
        return True, None
    except (smtplib.SMTPException, OSError) as e:
        return False, f"smtp error: {e}"


# --------------------------------------------------------------------- #
# Alert log persistence
# --------------------------------------------------------------------- #

def _log_alert(event_type: str, channel: str, status: str,
               trade_id: int | None, ticker: str | None,
               message: str, error: str | None = None,
               payload: dict | None = None) -> None:
    try:
        with connect() as c:
            c.execute(
                "INSERT INTO alert_log "
                "(timestamp, event_type, trade_id, ticker, channel, status, "
                " message, error, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (myt_iso(), event_type, trade_id, ticker, channel, status,
                 message[:2000], (error or "")[:500],
                 json.dumps(payload or {}, default=str)),
            )
    except Exception as e:
        log.error(f"alert_log write failed: {e}")


# --------------------------------------------------------------------- #
# Dispatch helper
# --------------------------------------------------------------------- #

def dispatch(event_type: str, message_text: str,
             message_html: str, subject: str,
             trade_id: int | None, ticker: str | None,
             channels: dict, recipients: list[str] | None = None,
             payload: dict | None = None) -> dict:
    """
    Send the same alert through every enabled channel.

    `channels` keys: 'telegram' (bool), 'email' (bool), 'dashboard' (bool, always True)
    """
    results = {}
    # Always log dashboard view
    _log_alert(event_type, "DASHBOARD", "SENT",
               trade_id, ticker, message_text, payload=payload)
    results["dashboard"] = (True, None)

    if channels.get("telegram"):
        ok, err = send_telegram(message_html)
        results["telegram"] = (ok, err)
        _log_alert(event_type, "TELEGRAM",
                   "SENT" if ok else "FAILED",
                   trade_id, ticker, message_text, err, payload)

    if channels.get("email") and recipients:
        ok, err = send_email(subject, message_html, recipients, html=True)
        results["email"] = (ok, err)
        _log_alert(event_type, "EMAIL",
                   "SENT" if ok else "FAILED",
                   trade_id, ticker, message_text, err, payload)

    return results
