"""
app.py — Flask backend for the Birthday Wishes Web App.

Endpoints:
  GET  /              → serve index.html
  GET  /api/contacts  → fetch all contacts from Google Sheets
  POST /api/send      → send birthday wishes to selected/all contacts
  GET  /api/status    → health check
"""

import logging
import sys
import os
from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# Add parent directory to path so we can reuse existing modules
sys.path.insert(0, str(Path(__file__).parent.parent / "birthday_agent"))

import config
import database
from sheets_reader import fetch_all_records, filter_todays_birthdays
from telegram_sender import send_birthday_message

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("app")

database.init_db()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/api/contacts", methods=["GET"])
def get_contacts():
    """Fetch all contacts from Google Sheets."""
    try:
        records, sheets_count = fetch_all_records()
        today = date.today()

        contacts = []
        for r in records:
            is_birthday_today = (r.dob.month == today.month and r.dob.day == today.day)
            contacts.append({
                "name":             r.name,
                "contact_number":   r.contact_number,
                "dob":              r.dob.isoformat(),
                "telegram_chat_id": r.telegram_chat_id,
                "sheet_title":      r.sheet_title,
                "is_birthday_today": is_birthday_today,
                "has_telegram":     bool(r.telegram_chat_id),
                "already_sent":     database.already_sent(
                                        r.name, r.contact_number,
                                        r.dob.month, r.dob.day, today.year
                                    ) if is_birthday_today else False,
            })

        return jsonify({
            "success": True,
            "contacts": contacts,
            "total": len(contacts),
            "sheets_scanned": sheets_count,
            "birthdays_today": sum(1 for c in contacts if c["is_birthday_today"]),
        })

    except Exception as e:
        logger.exception("Error fetching contacts")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/send", methods=["POST"])
def send_wishes():
    """
    Send birthday wishes.
    Body (JSON):
      { "mode": "today" }          → only today's birthdays
      { "mode": "selected",
        "contacts": [...] }        → specific list of {name, contact_number, telegram_chat_id}
      { "mode": "all" }            → everyone (regardless of birthday)
    """
    data    = request.get_json() or {}
    mode    = data.get("mode", "today")
    today   = date.today()
    results = []

    try:
        records, _ = fetch_all_records()

        if mode == "today":
            targets = filter_todays_birthdays(records)
        elif mode == "selected":
            selected_names = {c["name"] for c in data.get("contacts", [])}
            targets = [r for r in records if r.name in selected_names]
        else:  # all
            targets = records

        if not targets:
            return jsonify({
                "success": True,
                "results": [],
                "message": "No contacts matched the criteria.",
                "sent": 0,
                "failed": 0,
                "skipped": 0,
            })

        sent = failed = skipped = 0

        for person in targets:
            # Skip if no Telegram chat ID
            if not person.telegram_chat_id:
                results.append({
                    "name":    person.name,
                    "status":  "skipped",
                    "reason":  "No Telegram Chat ID",
                })
                skipped += 1
                continue

            # Skip duplicates for today's mode
            if mode == "today" and database.already_sent(
                person.name, person.contact_number,
                person.dob.month, person.dob.day, today.year
            ):
                results.append({
                    "name":   person.name,
                    "status": "skipped",
                    "reason": "Already sent today",
                })
                skipped += 1
                continue

            # Send message
            success = send_birthday_message(
                name=person.name,
                chat_id=person.telegram_chat_id
            )

            if success:
                if mode == "today":
                    database.mark_sent(
                        person.name, person.contact_number,
                        person.dob.month, person.dob.day,
                        today.year, sheet_id=person.sheet_id
                    )
                results.append({"name": person.name, "status": "sent"})
                sent += 1
            else:
                results.append({
                    "name":   person.name,
                    "status": "failed",
                    "reason": "Telegram API error",
                })
                failed += 1

        database.log_run(
            sheets_scanned=1,
            rows_processed=len(targets),
            messages_sent=sent,
            errors=failed,
            notes=f"web/{mode}"
        )

        return jsonify({
            "success": True,
            "results": results,
            "sent":    sent,
            "failed":  failed,
            "skipped": skipped,
            "message": f"Done! {sent} message(s) sent successfully.",
        })

    except Exception as e:
        logger.exception("Error sending wishes")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    logger.info("Starting Birthday Web App on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug)
