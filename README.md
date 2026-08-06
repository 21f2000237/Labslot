# LabBook — Flask Backend

A REST API backend for the Lab Equipment Scheduler (`lab-booking.html`).

---

## Quick start

```bash
# 1. Install dependencies
pip install flask
python app.py
```

The server starts at **http://localhost:5000**.

---

## API Reference

### Health
| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | — |

### Auth
| Method | Path | Body | Auth |
|--------|------|------|------|
| POST | `/api/auth/login` | `{ password }` | — |
| POST | `/api/auth/logout` | — | — |

### Equipment
| Method | Path | Auth |
|--------|------|------|
| GET | `/api/equipment` | — |

Returns the list of equipment names and available time slots.

### Slots
| Method | Path | Notes | Auth |
|--------|------|-------|------|
| GET | `/api/slots?date=YYYY-MM-DD&eq=UV` | Single day status map | — |
| GET | `/api/slots/week?week_offset=0&eq=UV` | 7-day status map | — |
| POST | `/api/slots/toggle` | Toggle slot open/closed | Owner |
| POST | `/api/slots/block-day` | Toggle whole-day block | Owner |

#### `POST /api/slots/toggle` body
```json
{ "date": "2026-05-20", "eq": "UV", "time": "10:00" }
```

#### `POST /api/slots/block-day` body
```json
{ "date": "2026-05-20", "eq": "UV" }
```

### Bookings
| Method | Path | Notes | Auth |
|--------|------|-------|------|
| GET | `/api/bookings` | List all bookings (filters: `?date=&eq=`) | Owner |
| POST | `/api/bookings` | Create booking | — |
| DELETE | `/api/bookings/<id>` | Cancel booking | Owner |

#### `POST /api/bookings` body
```json
{
  "date": "2026-05-20",
  "eq": "UV",
  "time": "10:00",
  "labName": "Nano Research Lab",
  "contactName": "Dr. Priya Sharma",
  "email": "priya@iitb.ac.in",
  "phone": "+91 98765 43210"
}
```

---

## Slot status values
| Value | Meaning |
|-------|---------|
| `available` | Can be booked |
| `booked` | Active booking exists |
| `unavailable` | Owner closed the slot |
| `past` | Date is in the past (no booking) |

---

## Database schema (SQLite)

Three tables are auto-created on first run:

- **slot_overrides** — owner-set per-slot open/closed overrides
- **blocked_days** — owner-set whole-day blocks
- **bookings** — booking records (soft-deleted with `cancelled=1`)

---

## Connecting to the frontend

Replace the `save()` and `load()` functions in `lab-booking.html` with `fetch()` calls to these API endpoints. Use the session cookie (returned by `/api/auth/login`) for authenticated owner routes.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | random | Flask session secret (set a fixed value in production) |
| `OWNER_PASSWORD` | `qsrfyji@123` | Owner login password |
| `DB_PATH` | `labbook.db` | Path to SQLite database file |
| `PORT` | `5000` | HTTP port |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode |
