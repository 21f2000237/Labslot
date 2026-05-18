
"""
LabBook — Lab Equipment Scheduler
Flask Backend  (Python 3.12 / Flask 3.x)

Endpoints
─────────────────────────────────────────────────────────────────────────────
POST   /api/auth/login            Owner login
POST   /api/auth/logout           Owner logout

GET    /api/slots?date=&eq=       Slot status map for one equipment + date
GET    /api/slots/week?week_offset=&eq=   Full week status (7 days)
POST   /api/slots/toggle          Owner — toggle single slot open/closed
POST   /api/slots/block-day       Owner — toggle whole-day block

GET    /api/bookings              Owner — all bookings (filterable)
POST   /api/bookings              Create a booking
DELETE /api/bookings/<id>         Owner — cancel a booking

GET    /api/equipment             List of equipment names
GET    /health                    Health-check
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import uuid
import json
import base64
import sqlite3
import hashlib
import secrets
import logging
import smtplib
import ssl
import urllib.request
import urllib.parse
from email.message import EmailMessage
from datetime import date, timedelta, datetime
from functools import wraps

from flask import Flask, request, jsonify, g, session, send_from_directory

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("labbook")

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE = os.environ.get("DB_PATH", "labbook.db")

# Password stored as SHA-256 hex (matching the plain-text in the frontend for
# demonstration; in production load from environment variable).
OWNER_PASSWORD_HASH = hashlib.sha256(
    os.environ.get("OWNER_PASSWORD", "qsrfyji@123").encode()
).hexdigest()

EQUIPMENT: list[str] = ["UV", "Centrifuge", "HPLC", "Particle Size", "Lyophilizer"]

SLOTS: list[str] = [
    "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30",
    "15:00", "15:30", "16:00", "16:30", "17:00", "17:30",
    "18:00", "18:30",
]

# ── Database helpers ───────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Return the per-request database connection (creates if missing)."""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS slot_overrides (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,   -- YYYY-MM-DD
            equipment   TEXT    NOT NULL,
            time        TEXT    NOT NULL,   -- HH:MM
            status      TEXT    NOT NULL,   -- 'available' | 'unavailable'
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(date, equipment, time)
        );

        CREATE TABLE IF NOT EXISTS blocked_days (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,   -- YYYY-MM-DD
            equipment   TEXT    NOT NULL,
            blocked_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(date, equipment)
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id           TEXT    PRIMARY KEY,
            equipment    TEXT    NOT NULL,
            date         TEXT    NOT NULL,   -- YYYY-MM-DD
            time         TEXT    NOT NULL,   -- HH:MM
            lab_name     TEXT    NOT NULL,
            contact_name TEXT    NOT NULL,
            email        TEXT    NOT NULL,
            phone        TEXT    NOT NULL,
            booked_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            cancelled    INTEGER NOT NULL DEFAULT 0
        );
    """)
    db.commit()
    db.close()
    logger.info("Database initialised at %s", DATABASE)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def require_owner(f):
    """Decorator — returns 401 unless the session is authenticated as owner."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_owner"):
            return jsonify({"error": "Owner authentication required."}), 401
        return f(*args, **kwargs)
    return decorated


# ── Slot logic (mirrors frontend getSlot) ─────────────────────────────────────

def _today() -> date:
    return date.today()


def _is_past(d: date) -> bool:
    return d < _today()


def _get_slot_status(db: sqlite3.Connection, date_str: str, equipment: str, time: str) -> str:
    """
    Return the effective status of a slot:
      'booked'      — an active booking exists
      'unavailable' — no override (default closed) or date is in the past
      'available'   — explicitly opened by owner
    """
    d = date.fromisoformat(date_str)

    # Past → unavailable (unless there's a booking, which we still show)
    if _is_past(d):
        row = db.execute(
            "SELECT id FROM bookings WHERE date=? AND equipment=? AND time=? AND cancelled=0",
            (date_str, equipment, time),
        ).fetchone()
        return "booked" if row else "past"

    # Active booking?
    row = db.execute(
        "SELECT id FROM bookings WHERE date=? AND equipment=? AND time=? AND cancelled=0",
        (date_str, equipment, time),
    ).fetchone()
    if row:
        return "booked"

    # Owner override?
    override = db.execute(
        "SELECT status FROM slot_overrides WHERE date=? AND equipment=? AND time=?",
        (date_str, equipment, time),
    ).fetchone()
    if override:
        return override["status"]

    return "unavailable"


# ── Validation helpers ─────────────────────────────────────────────────────────

def _valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


def _valid_equipment(s: str) -> bool:
    return s in EQUIPMENT


def _valid_time(s: str) -> bool:
    return s in SLOTS


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s or ""))


# ── CORS (manual, no external package) ───────────────────────────────────────

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.route("/api/<path:p>", methods=["OPTIONS"])
@app.route("/health", methods=["OPTIONS"])
def handle_options(p=""):  # noqa: ARG001
    return "", 204


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@app.get("/")
@app.get("/uv")
@app.get("/hplc")
@app.get("/centrifuge")
@app.get("/particle-size")
@app.get("/lyophilizer")
def root():
    return send_from_directory(os.path.dirname(__file__), "lab-booking.html")


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    hashed = hashlib.sha256(password.encode()).hexdigest()
    if secrets.compare_digest(hashed, OWNER_PASSWORD_HASH):
        session["is_owner"] = True
        return jsonify({"success": True, "message": "Logged in as owner."})
    return jsonify({"success": False, "error": "Incorrect password."}), 401


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"success": True})


# ── Equipment list ─────────────────────────────────────────────────────────────

@app.get("/api/equipment")
def get_equipment():
    return jsonify({"equipment": EQUIPMENT, "slots": SLOTS})


# ── Slots ─────────────────────────────────────────────────────────────────────

@app.get("/api/slots")
def get_slots_day():
    """
    Returns slot statuses for a single equipment on a single date.
    Query params: date (YYYY-MM-DD), eq (equipment name)
    """
    date_str = request.args.get("date")
    equipment = request.args.get("eq")

    if not _valid_date(date_str):
        return jsonify({"error": "Invalid or missing 'date' (YYYY-MM-DD)."}), 400
    if not _valid_equipment(equipment):
        return jsonify({"error": f"Invalid equipment. Choose from: {EQUIPMENT}"}), 400

    db = get_db()
    blocked = db.execute(
        "SELECT 1 FROM blocked_days WHERE date=? AND equipment=?", (date_str, equipment)
    ).fetchone()

    statuses = {
        t: _get_slot_status(db, date_str, equipment, t) for t in SLOTS
    }
    return jsonify({
        "date": date_str,
        "equipment": equipment,
        "blocked": bool(blocked),
        "slots": statuses,
    })


@app.get("/api/slots/week")
def get_slots_week():
    """
    Returns slot statuses for all 7 days of a week.
    Query params: week_offset (int, default 0), eq (equipment name)
    """
    try:
        offset = int(request.args.get("week_offset", 0))
    except ValueError:
        return jsonify({"error": "'week_offset' must be an integer."}), 400

    equipment = request.args.get("eq")
    if not _valid_equipment(equipment):
        return jsonify({"error": f"Invalid equipment. Choose from: {EQUIPMENT}"}), 400

    # Monday of the requested week
    today = _today()
    days_since_monday = today.weekday()  # Mon=0, Sun=6
    monday = today - timedelta(days=days_since_monday) + timedelta(weeks=offset)
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    db = get_db()
    result = []
    for d in week_dates:
        date_str = d.isoformat()
        blocked = db.execute(
            "SELECT 1 FROM blocked_days WHERE date=? AND equipment=?",
            (date_str, equipment),
        ).fetchone()
        statuses = {
            t: _get_slot_status(db, date_str, equipment, t) for t in SLOTS
        }
        result.append({
            "date": date_str,
            "blocked": bool(blocked),
            "slots": statuses,
        })

    return jsonify({
        "week_offset": offset,
        "equipment": equipment,
        "week_start": monday.isoformat(),
        "days": result,
    })


@app.post("/api/slots/toggle")
@require_owner
def toggle_slot():
    """
    Owner — toggle a single slot between available and unavailable.
    Body: { date, eq, time }
    """
    data = request.get_json(silent=True) or {}
    date_str = data.get("date")
    equipment = data.get("eq")
    time = data.get("time")

    if not _valid_date(date_str):
        return jsonify({"error": "Invalid or missing 'date'."}), 400
    if not _valid_equipment(equipment):
        return jsonify({"error": "Invalid equipment."}), 400
    if not _valid_time(time):
        return jsonify({"error": "Invalid time slot."}), 400

    db = get_db()
    current = _get_slot_status(db, date_str, equipment, time)
    if current == "booked":
        return jsonify({"error": "Cannot toggle a booked slot."}), 409

    new_status = "unavailable" if current == "available" else "available"

    db.execute(
        """
        INSERT INTO slot_overrides (date, equipment, time, status, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(date, equipment, time) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (date_str, equipment, time, new_status),
    )
    db.commit()

    return jsonify({"date": date_str, "equipment": equipment, "time": time, "status": new_status})


@app.post("/api/slots/block-day")
@require_owner
def toggle_block_day():
    """
    Owner — toggle a whole-day block for one equipment.
    Body: { date, eq }
    """
    data = request.get_json(silent=True) or {}
    date_str = data.get("date")
    equipment = data.get("eq")

    if not _valid_date(date_str):
        return jsonify({"error": "Invalid or missing 'date'."}), 400
    if not _valid_equipment(equipment):
        return jsonify({"error": "Invalid equipment."}), 400

    db = get_db()
    existing = db.execute(
        "SELECT id FROM blocked_days WHERE date=? AND equipment=?",
        (date_str, equipment),
    ).fetchone()

    if existing:
        db.execute(
            "DELETE FROM blocked_days WHERE date=? AND equipment=?",
            (date_str, equipment),
        )
        db.commit()
        return jsonify({"date": date_str, "equipment": equipment, "blocked": False})
    else:
        db.execute(
            "INSERT INTO blocked_days (date, equipment) VALUES (?, ?)",
            (date_str, equipment),
        )
        db.commit()
        return jsonify({"date": date_str, "equipment": equipment, "blocked": True})


# ── Bookings ───────────────────────────────────────────────────────────────────

@app.get("/api/bookings")
@require_owner
def list_bookings():
    """
    Owner — list all (non-cancelled) bookings.
    Optional query filters: date, eq
    """
    db = get_db()
    query = "SELECT * FROM bookings WHERE cancelled=0"
    params: list = []

    date_filter = request.args.get("date")
    eq_filter = request.args.get("eq")

    if date_filter:
        if not _valid_date(date_filter):
            return jsonify({"error": "Invalid 'date' filter."}), 400
        query += " AND date=?"
        params.append(date_filter)
    if eq_filter:
        if not _valid_equipment(eq_filter):
            return jsonify({"error": "Invalid 'eq' filter."}), 400
        query += " AND equipment=?"
        params.append(eq_filter)

    query += " ORDER BY date, time"
    rows = db.execute(query, params).fetchall()
    return jsonify({"bookings": [dict(r) for r in rows], "total": len(rows)})


@app.post("/api/bookings")
def create_booking():
    """
    Create a new booking.
    Body: { date, eq, time, labName, contactName, email, phone }
    """
    data = request.get_json(silent=True) or {}

    date_str = data.get("date")
    equipment = data.get("eq")
    time = data.get("time")
    lab_name = (data.get("labName") or "").strip()
    contact_name = (data.get("contactName") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()

    # Validation
    errors = {}
    if not _valid_date(date_str):
        errors["date"] = "Invalid or missing date (YYYY-MM-DD)."
    if not _valid_equipment(equipment):
        errors["eq"] = f"Invalid equipment. Choose from: {EQUIPMENT}"
    if not _valid_time(time):
        errors["time"] = "Invalid time slot."
    if not lab_name:
        errors["labName"] = "Lab name is required."
    if not contact_name:
        errors["contactName"] = "Contact name is required."
    if not _valid_email(email):
        errors["email"] = "Valid email address is required."
    if not phone:
        errors["phone"] = "Phone number is required."

    if errors:
        return jsonify({"error": "Validation failed.", "fields": errors}), 422

    # Check date is not in the past
    if _is_past(date.fromisoformat(date_str)):
        return jsonify({"error": "Cannot book a slot in the past."}), 409

    db = get_db()

    # Check day block
    blocked = db.execute(
        "SELECT 1 FROM blocked_days WHERE date=? AND equipment=?",
        (date_str, equipment),
    ).fetchone()
    if blocked:
        return jsonify({"error": "This day has been blocked by the lab owner."}), 409

    # Check slot availability
    status = _get_slot_status(db, date_str, equipment, time)
    if status == "booked":
        return jsonify({"error": "This slot is already booked."}), 409
    if status == "unavailable":
        return jsonify({"error": "This slot is not available for booking."}), 409

    booking_id = uuid.uuid4().hex[:12]
    db.execute(
        """
        INSERT INTO bookings (id, equipment, date, time, lab_name, contact_name, email, phone)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (booking_id, equipment, date_str, time, lab_name, contact_name, email, phone),
    )
    db.commit()

    booking_obj = {
        "id": booking_id,
        "equipment": equipment,
        "date": date_str,
        "time": time,
        "labName": lab_name,
        "contactName": contact_name,
        "email": email,
        "phone": phone,
    }

    logger.info("Booking created: %s %s %s %s by %s", booking_id, equipment, date_str, time, lab_name)

    # Attempt to send notifications to admin (if configured)
    email_sent = send_email_notification(booking_obj)
    whatsapp_sent = send_whatsapp_notification(booking_obj)

    return jsonify({
        "success": True,
        "booking": booking_obj,
        "notified": bool(email_sent or whatsapp_sent),
        "notifications": {
            "email": email_sent,
            "whatsapp": whatsapp_sent,
        },
    }), 201


@app.delete("/api/bookings/<booking_id>")
@require_owner
def cancel_booking(booking_id: str):
    """Owner — soft-delete a booking and free the slot."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM bookings WHERE id=? AND cancelled=0", (booking_id,)
    ).fetchone()

    if not row:
        return jsonify({"error": "Booking not found."}), 404

    db.execute(
        "UPDATE bookings SET cancelled=1 WHERE id=?", (booking_id,)
    )
    # Remove any 'booked' override so the slot reverts to available
    db.execute(
        "DELETE FROM slot_overrides WHERE date=? AND equipment=? AND time=?",
        (row["date"], row["equipment"], row["time"]),
    )
    db.commit()

    logger.info("Booking cancelled: %s", booking_id)
    return jsonify({"success": True, "cancelled_id": booking_id})


# ── Email notifications ─────────────────────────────────────────────────────

def send_email_notification(booking: dict) -> bool:
    """Send a plain-text email notification about a booking to the configured ADMIN_EMAIL.
    Environment variables:
      - ADMIN_EMAIL: recipient for admin notifications (required to actually send)
      - SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
      - SMTP_FROM (optional)
    Returns True on success, False otherwise.
    """
    admin = os.environ.get("ADMIN_EMAIL")
    if not admin:
        logger.info("ADMIN_EMAIL not set — skipping email notification.")
        return False

    smtp_host = os.environ.get("SMTP_HOST", "localhost")
    smtp_port = int(os.environ.get("SMTP_PORT", "25"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    smtp_from = os.environ.get("SMTP_FROM") or (smtp_user if smtp_user else f"no-reply@{os.environ.get('HOSTNAME','localhost')}")

    msg = EmailMessage()
    msg["Subject"] = f"New booking: {booking['equipment']} {booking['date']} {booking['time']}"
    msg["From"] = smtp_from
    msg["To"] = admin

    body = (
        f"New booking received:\n"
        f"ID: {booking['id']}\n"
        f"Equipment: {booking['equipment']}\n"
        f"Date: {booking['date']}\n"
        f"Time: {booking['time']}\n"
        f"Lab name: {booking.get('labName','')}\n"
        f"Contact: {booking.get('contactName','')}\n"
        f"Email: {booking.get('email','')}\n"
        f"Phone: {booking.get('phone','')}\n"
    )
    msg.set_content(body)

    try:
        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as s:
                if smtp_user and smtp_pass:
                    s.login(smtp_user, smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as s:
                try:
                    s.starttls(context=ssl.create_default_context())
                except Exception:
                    pass
                if smtp_user and smtp_pass:
                    s.login(smtp_user, smtp_pass)
                s.send_message(msg)
        logger.info("Email notification sent to %s", admin)
        return True
    except Exception as e:
        logger.exception("Failed to send email notification: %s", e)
        return False


def send_whatsapp_notification(booking: dict) -> bool:
    """Send a WhatsApp notification via Twilio or WhatsApp Cloud API."""
    to_number = os.environ.get("WHATSAPP_TO")
    from_number = os.environ.get("WHATSAPP_FROM")
    if not to_number or not from_number:
        logger.info("WHATSAPP_TO or WHATSAPP_FROM not set — skipping WhatsApp notification.")
        return False

    body = (
        f"New booking received:\n"
        f"ID: {booking['id']}\n"
        f"Equipment: {booking['equipment']}\n"
        f"Date: {booking['date']}\n"
        f"Time: {booking['time']}\n"
        f"Lab: {booking.get('labName','')}\n"
        f"Contact: {booking.get('contactName','')}\n"
        f"Email: {booking.get('email','')}\n"
        f"Phone: {booking.get('phone','')}\n"
    )

    twilio_sid = os.environ.get("WHATSAPP_ACCOUNT_SID")
    twilio_token = os.environ.get("WHATSAPP_AUTH_TOKEN")
    api_url = os.environ.get("WHATSAPP_API_URL")
    api_token = os.environ.get("WHATSAPP_API_TOKEN")

    if twilio_sid and twilio_token:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
        data = urllib.parse.urlencode({
            "From": f"whatsapp:{from_number}",
            "To": f"whatsapp:{to_number}",
            "Body": body,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        auth = f"{twilio_sid}:{twilio_token}"
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    logger.info("WhatsApp notification sent via Twilio to %s", to_number)
                    return True
                logger.warning("WhatsApp notification failed: %s", resp.read().decode())
        except Exception as e:
            logger.exception("Failed to send WhatsApp notification via Twilio: %s", e)
        return False

    if api_url and api_token:
        payload = json.dumps({
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": body},
        }).encode("utf-8")
        req = urllib.request.Request(api_url, data=payload)
        req.add_header("Authorization", f"Bearer {api_token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    logger.info("WhatsApp notification sent via Cloud API to %s", to_number)
                    return True
                logger.warning("WhatsApp notification failed: %s", resp.read().decode())
        except Exception as e:
            logger.exception("Failed to send WhatsApp notification via Cloud API: %s", e)
        return False

    logger.info("No WhatsApp provider configured. Set WHATSAPP_ACCOUNT_SID/WHATSAPP_AUTH_TOKEN or WHATSAPP_API_URL/WHATSAPP_API_TOKEN.")
    return False


@app.post("/api/bookings/<booking_id>/email")
@require_owner
def resend_booking_email(booking_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        return jsonify({"error": "Booking not found."}), 404

    booking = {
        "id": row["id"],
        "equipment": row["equipment"],
        "date": row["date"],
        "time": row["time"],
        "labName": row["lab_name"],
        "contactName": row["contact_name"],
        "email": row["email"],
        "phone": row["phone"],
    }

    ok = send_email_notification(booking)
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to send email."}), 500


@app.get("/admin/bookings")
@require_owner
def admin_bookings_page():
    return send_from_directory(os.path.dirname(__file__), "admin-bookings.html")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
