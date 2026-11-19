# appointment-slot-conflict-auditor-cli-lite

Zero-dependency Python 3.9+ CLI to audit appointment schedules for overlapping slots, buffer violations, off-hours bookings, break conflicts, and duplicate client bookings, as well as generate available free time slots for any target date and service duration.

---

## Features

- **Zero external dependencies**: Pure Python 3.9+ standard library (`argparse`, `csv`, `datetime`, `json`, `pathlib`). No virtual environment or third-party packages required.
- **Flexible JSON & CSV input**: Accepts structured JSON schedules (with embedded working hours, breaks, and buffer settings), flat JSON booking arrays, or CSV exports (`--schedule` / positional path or `-` for `stdin`).
- **Comprehensive schedule audit (`--audit`)**:
  - `OVERLAP` (`ERROR`): Detects exact, partial, and nested overlapping appointments.
  - `BUFFER_VIOLATION` (`WARN`): Flags consecutive bookings separated by fewer minutes than `--buffer` / `buffer_minutes`.
  - `OFF_HOURS` (`ERROR`): Flags appointments outside weekday working hours or on closed days.
  - `BREAK_CONFLICT` (`ERROR`): Flags bookings overlapping configured break windows (e.g. lunch breaks).
  - `DUPLICATE_CLIENT` (`WARN`): Flags multiple active bookings for the same client on the same calendar date (case- and whitespace-normalized).
  - Ignores `cancelled` / `rejected` bookings by default (toggle via `--include-cancelled`), with optional multi-staff isolation (`--by-staff`, `--staff`).
- **Free time-slot generator (`--free-slots`)**: Computes available appointment intervals for `--date YYYY-MM-DD`, respecting weekday working windows (including split shifts), scheduled breaks, existing bookings, symmetric buffer margins, `--duration`, `--step`, and `--min-start`.
- **Automation-friendly outputs**: Renders aligned human-readable tables (`--format text`), structured `--format json`, or RFC-4180 `--format csv`, plus CI exit codes via `--fail-on {none,error,warn}`.

---

## Quick Start

### 1. Audit a schedule for conflicts and policy violations

```bash
python slot_auditor.py sample_schedule.json --audit
```

Example output:

```text
Schedule Audit Summary | Bookings: 10 (active: 9, cancelled: 1) | Buffer: 15m | Errors: 4 | Warnings: 2 | Status: FAIL

Severity | Code             | Date       | Window      | Bookings       | Details
---------+------------------+------------+-------------+----------------+------------------------------------------------------------------------------------------------
WARN     | DUPLICATE_CLIENT | 2026-04-13 | 09:00-10:00 | BK-101, BK-105 | Client 'Alice Carter' has 2 bookings on 2026-04-13: BK-101 (09:00-10:00), BK-105 (15:00-16:00).
ERROR    | OVERLAP          | 2026-04-13 | 09:45-10:00 | BK-101, BK-102 | Bookings BK-101 (09:00-10:00) and BK-102 (09:45-10:45) overlap by 15m.
WARN     | BUFFER_VIOLATION | 2026-04-13 | 10:45-10:50 | BK-102, BK-103 | Gap between BK-102 (ends 10:45) and BK-103 (starts 10:50) is 5m, below required 15m buffer.
ERROR    | BREAK_CONFLICT   | 2026-04-13 | 13:15-14:00 | BK-104         | Booking BK-104 (13:15-14:00) overlaps scheduled break (13:00-14:00).
ERROR    | OFF_HOURS        | 2026-04-13 | 18:15-19:00 | BK-107         | Booking BK-107 (18:15-19:00) falls outside Mon working hours (09:00-18:00).
ERROR    | OFF_HOURS        | 2026-04-19 | 11:00-12:00 | BK-110         | Booking BK-110 (11:00-12:00) is scheduled on a closed day (Sun).
```

### 2. Generate free slots for a target date & service duration

```bash
python slot_auditor.py sample_schedule.json --free-slots --date 2026-04-14 --duration 60
```

```text
Free Slots | Date: 2026-04-14 (Tue) | Work: 09:00-18:00 | Breaks: 13:00-14:00 | Duration: 60m | Step: 60m | Buffer: 15m | Available: 3

# | Date       | Start | End   | Duration
--+------------+-------+-------+---------
1 | 2026-04-14 | 12:00 | 13:00 | 60m
2 | 2026-04-14 | 16:00 | 17:00 | 60m
3 | 2026-04-14 | 17:00 | 18:00 | 60m
```

### 3. Custom step size, JSON/CSV exports, and CSV schedule auditing

```bash
# 45-minute slots on a 30-minute grid
python slot_auditor.py sample_schedule.json --free-slots --date 2026-04-14 --duration 45 --step 30

# Audit and generate free slots together in JSON
python slot_auditor.py sample_schedule.json --audit --free-slots --date 2026-04-14 --format json

# Export audit findings to CSV and fail CI on errors
python slot_auditor.py sample_schedule.json --audit --format csv --fail-on error

# Audit a CSV schedule with CLI working-hours and buffer rules
python slot_auditor.py bookings.csv --audit --work-hours "mon-fri=09:00-18:00,sat=10:00-14:00,sun=closed" --buffer 15 --break "13:00-14:00"
```

---

## Input Formats

### JSON Schedule (`sample_schedule.json`)

```json
{
  "timezone": "UTC",
  "buffer_minutes": 15,
  "working_hours": {
    "mon-thu": "09:00-18:00",
    "fri": "09:00-17:00",
    "sat": "10:00-14:00",
    "sun": null
  },
  "breaks": {
    "default": ["13:00-14:00"]
  },
  "bookings": [
    {
      "id": "BK-101",
      "date": "2026-04-13",
      "start": "09:00",
      "end": "10:00",
      "client": "Alice Carter",
      "service": "Initial Consultation",
      "staff": "Dr. Lee",
      "status": "confirmed"
    }
  ]
}
```

- Top-level JSON arrays of booking objects are also supported.
- Bookings accept `date` + `start` + `end`, `date` + `start` + `duration_minutes`, `date` + `slot` (`"09:00-10:00"`), or ISO-8601 `start` / `end` timestamps (`"2026-04-13T09:00:00"`).

### CSV Schedule

Supports standard comma- or semicolon-delimited CSV with flexible column aliases:

```csv
id,date,start,end,client,service,staff,status
BK-101,2026-04-13,09:00,10:00,Alice Carter,Consultation,Dr. Lee,confirmed
```

---

## CLI Options & Exit Codes

| Flag | Default | Description |
| :--- | :--- | :--- |
| `schedule` / `-s`, `--schedule` | — | Path to JSON or CSV booking file (`-` for stdin) |
| `--audit` | `true` (if no mode set) | Audit schedule for conflicts, buffer gaps, off-hours, breaks, and duplicate clients |
| `--free-slots` | `false` | Generate available time slots for `--date` and `--duration` |
| `--date` | `—` | Target `YYYY-MM-DD` for `--free-slots` (or filter date for `--audit`) |
| `--duration` | `60` | Service duration in minutes for `--free-slots` |
| `--step` | `duration` | Candidate slot grid step in minutes |
| `--min-start` | `—` | Earliest allowed slot start time (`HH:MM`) |
| `--rules` | `—` | Optional JSON rules file (`working_hours`, `breaks`, `buffer_minutes`) |
| `--work-hours` | `mon-fri=09:00-18:00` | Override working hours (`HH:MM-HH:MM` or `mon-fri=...,sat=...,sun=closed`) |
| `--day-hours` | `[]` | Repeatable weekday override (`DAY=HH:MM-HH:MM` or `DAY=closed`) |
| `--buffer` | `0` (or file rule) | Turnaround buffer minutes between bookings |
| `--break` | `[]` | Repeatable break window (`HH:MM-HH:MM`, `day=HH:MM-HH:MM`, or `none`) |
| `--staff` | `—` | Filter bookings and free slots by staff/resource name |
| `--by-staff` | `false` | Check overlaps and buffer gaps separately per staff/resource |
| `--include-cancelled` | `false` | Include cancelled/rejected bookings in audit and slot blocking |
| `--format` | `text` | Output format: `text`, `json`, or `csv` |
| `--fail-on` | `none` | Exit `1` on audit issues: `none`, `error`, or `warn` |

**Exit Codes**: `0` = success (within `--fail-on` policy), `1` = audit policy failed under `--fail-on`, `2` = invalid arguments, unreadable file, or malformed schedule data.

---

## Lite CLI vs Full Telegram Appointment Booking Bot Pro

This open-source Lite CLI handles offline schedule conflict auditing and free-slot calculation from JSON/CSV files. If you want an end-to-end **self-hosted Telegram booking system** where clients browse an interactive calendar, pick slots, receive automated reminders, and admins manage bookings in real time, compare both editions below:

| Capability | Lite CLI (Free MIT) | Full Telegram Appointment Booking, Slot Calendar & Reminder Bot Pro |
| :--- | :--- | :--- |
| Offline JSON/CSV schedule conflict & buffer audit | Yes | Built-in database conflict prevention |
| Free time-slot calculation with buffer & breaks | Yes | Yes (live dynamic calendar per service/day) |
| Interactive Telegram inline calendar & slot picker | — | Yes |
| Persistent booking & client storage | — | SQLite database with atomic slot locking |
| Automated 24h & 2h client reminders | — | Yes (scheduled Telegram notifications) |
| Admin management panel (confirm, cancel, reschedule, holidays) | — | Yes |
| Multi-service catalog (custom duration, price, buffer) | — | Yes |
| Production deployment templates & setup guide | — | Yes |

- **Get the Pro Edition on FunPay:** https://funpay.com/lots/offer?id=82603786

---

## Running Tests

```bash
py -m unittest discover -s tests -v
```

---

## License

MIT License — see [LICENSE](LICENSE).
