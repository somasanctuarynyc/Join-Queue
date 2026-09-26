"""Send the end-of-day client list (name + phone, seen or not)."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from store import format_phone

DEFAULT_TO = "somasanctuarynyc@gmail.com"
DEFAULT_SMTP_HOST = "smtp.gmail.com"


def mail_config() -> dict:
    to_addr = os.environ.get("JTQ_ADMIN_EMAIL", DEFAULT_TO).strip() or DEFAULT_TO
    host = os.environ.get("JTQ_SMTP_HOST", DEFAULT_SMTP_HOST).strip() or DEFAULT_SMTP_HOST
    user = os.environ.get("JTQ_SMTP_USER", to_addr).strip() or to_addr
    password = os.environ.get("JTQ_SMTP_PASS", "").strip()
    from_addr = os.environ.get("JTQ_FROM_EMAIL", user or to_addr).strip() or to_addr
    port = int(os.environ.get("JTQ_SMTP_PORT", "587"))
    return {
        "to": to_addr,
        "from": from_addr,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "ready": bool(to_addr and host and password),
    }


def seen_label(client: dict) -> str:
    if client.get("status") == "done" or client.get("seen"):
        return "Seen"
    if client.get("status") == "removed":
        return "Not seen (removed / no-show)"
    return "Not seen (still in queue)"


def report_text(state: dict) -> str:
    day = state.get("day", "")
    lines = [
        "SOMA Sanctuary — Join The Queue (JTQ)",
        "www.somasanctuary.nyc",
        f"Daily client list for {day}",
        "",
        "Name | Phone | Status",
        "-----|-------|--------",
    ]
    if not state.get("clients"):
        lines.append("(no clients today)")
    for client in state.get("clients", []):
        lines.append(
            f"{client.get('name', '')} | {format_phone(client.get('phone', ''))} | {seen_label(client)}"
        )
    lines.append("")
    lines.append("Payment was recorded as intent only — nothing was charged.")
    lines.append("SOMA Sanctuary · https://www.somasanctuary.nyc")
    return "\n".join(lines)


def send_day_report(state: dict) -> tuple[bool, str]:
    cfg = mail_config()
    if not cfg["password"]:
        return (
            False,
            "Reports go to somasanctuarynyc@gmail.com. Add a Gmail app password as JTQ_SMTP_PASS in .env.",
        )

    msg = EmailMessage()
    msg["Subject"] = f"SOMA Sanctuary JTQ — {state.get('day', 'today')}"
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(report_text(state))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        return True, f"Sent to {cfg['to']}"
    except Exception as exc:  # noqa: BLE001 — surface SMTP errors to admin UI
        return False, str(exc)
