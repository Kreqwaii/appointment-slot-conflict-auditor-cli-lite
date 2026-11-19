#!/usr/bin/env python3
"""Appointment Slot Conflict Auditor & Free-Slot Generator CLI (Lite).

Zero-dependency Python 3.9+ standard-library utility to audit JSON/CSV
appointment schedules for overlapping bookings, buffer-gap violations,
off-hours bookings, break conflicts, and duplicate client bookings, as well
as generate available time slots for a target date and service duration.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

WEEKDAYS: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS: Dict[str, str] = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}
WEEKDAY_ALIASES: Dict[str, str] = {
    "mon": "mon",
    "monday": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "thursday": "thu",
    "fri": "fri",
    "friday": "fri",
    "sat": "sat",
    "saturday": "sat",
    "sun": "sun",
    "sunday": "sun",
}
INACTIVE_STATUSES = frozenset({"cancelled", "canceled", "rejected", "declined", "void"})
CLOSED_TOKENS = frozenset({"", "closed", "off", "none", "null", "holiday"})
SEVERITY_RANK = {"ERROR": 0, "WARN": 1, "INFO": 2}


@dataclass(frozen=True, order=True)
class TimeInterval:
    """Half-open time interval [start, end) represented as minutes since 00:00."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (0 <= self.start < self.end <= 24 * 60):
            raise ValueError(
                f"Invalid time interval '{minutes_to_hhmm(self.start)}-{minutes_to_hhmm(self.end)}': "
                "start must be strictly before end within 00:00-24:00."
            )

    @property
    def duration_minutes(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "TimeInterval") -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: "TimeInterval") -> bool:
        return self.start <= other.start and other.end <= self.end

    def format_hhmm(self) -> str:
        return f"{minutes_to_hhmm(self.start)}-{minutes_to_hhmm(self.end)}"


@dataclass(frozen=True)
class Booking:
    """Normalized appointment booking record."""

    booking_id: str
    booking_date: date
    start_min: int
    end_min: int
    client: str = ""
    service: str = ""
    staff: str = ""
    status: str = "confirmed"
    source_index: int = 0

    @property
    def interval(self) -> TimeInterval:
        return TimeInterval(self.start_min, self.end_min)

    @property
    def window(self) -> str:
        return f"{minutes_to_hhmm(self.start_min)}-{minutes_to_hhmm(self.end_min)}"

    @property
    def is_active(self) -> bool:
        return self.status.strip().lower() not in INACTIVE_STATUSES


@dataclass
class ScheduleRules:
    """Weekday working hours, breaks, and turnaround buffer configuration."""

    working_hours: Dict[str, Tuple[TimeInterval, ...]] = field(default_factory=dict)
    breaks: Dict[str, Tuple[TimeInterval, ...]] = field(default_factory=dict)
    buffer_minutes: int = 0
    timezone: str = "UTC"

    @classmethod
    def default(cls) -> "ScheduleRules":
        default_wh: Dict[str, Tuple[TimeInterval, ...]] = {
            "mon": (TimeInterval(9 * 60, 18 * 60),),
            "tue": (TimeInterval(9 * 60, 18 * 60),),
            "wed": (TimeInterval(9 * 60, 18 * 60),),
            "thu": (TimeInterval(9 * 60, 18 * 60),),
            "fri": (TimeInterval(9 * 60, 18 * 60),),
            "sat": (),
            "sun": (),
        }
        default_breaks: Dict[str, Tuple[TimeInterval, ...]] = {day: () for day in WEEKDAYS}
        return cls(working_hours=default_wh, breaks=default_breaks, buffer_minutes=0, timezone="UTC")


@dataclass(frozen=True)
class AuditIssue:
    """Single audit finding emitted during schedule verification."""

    severity: str  # ERROR | WARN
    code: str      # OVERLAP | BUFFER_VIOLATION | OFF_HOURS | BREAK_CONFLICT | DUPLICATE_CLIENT
    issue_date: date
    window: str
    booking_ids: Tuple[str, ...]
    clients: Tuple[str, ...]
    message: str
    sort_start: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "date": self.issue_date.isoformat(),
            "window": self.window,
            "booking_ids": list(self.booking_ids),
            "clients": list(self.clients),
            "message": self.message,
        }


@dataclass(frozen=True)
class AuditReport:
    """Aggregated schedule audit result."""

    total_bookings: int
    active_bookings: int
    cancelled_bookings: int
    buffer_minutes: int
    issues: Tuple[AuditIssue, ...]
    target_date: Optional[date] = None

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.issues if item.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.issues if item.severity == "WARN")

    @property
    def passed(self) -> bool:
        return len(self.issues) == 0

    def counts_by_code(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self.issues:
            counts[item.code] = counts.get(item.code, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "total_bookings": self.total_bookings,
            "active_bookings": self.active_bookings,
            "cancelled_bookings": self.cancelled_bookings,
            "buffer_minutes": self.buffer_minutes,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issue_count": len(self.issues),
            "passed": self.passed,
            "counts_by_code": self.counts_by_code(),
        }
        if self.target_date is not None:
            summary["target_date"] = self.target_date.isoformat()
        return {
            "mode": "audit",
            "summary": summary,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class FreeSlotsReport:
    """Generated free time slots for a target date."""

    target_date: date
    weekday: str
    closed: bool
    working_hours: Tuple[TimeInterval, ...]
    breaks: Tuple[TimeInterval, ...]
    duration_minutes: int
    step_minutes: int
    buffer_minutes: int
    slots: Tuple[TimeInterval, ...]
    staff: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "mode": "free_slots",
            "date": self.target_date.isoformat(),
            "weekday": self.weekday,
            "closed": self.closed,
            "working_hours": [w.format_hhmm() for w in self.working_hours],
            "breaks": [b.format_hhmm() for b in self.breaks],
            "duration_minutes": self.duration_minutes,
            "step_minutes": self.step_minutes,
            "buffer_minutes": self.buffer_minutes,
            "available_count": len(self.slots),
            "slots": [
                {
                    "date": self.target_date.isoformat(),
                    "start": minutes_to_hhmm(slot.start),
                    "end": minutes_to_hhmm(slot.end),
                    "label": slot.format_hhmm(),
                    "duration_minutes": slot.duration_minutes,
                }
                for slot in self.slots
            ],
        }
        if self.staff:
            payload["staff"] = self.staff
        return payload


def minutes_to_hhmm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def hhmm_to_minutes(value: str) -> int:
    cleaned = value.strip()
    parts = cleaned.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid time '{value}', expected HH:MM.")
    if not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid time '{value}', expected HH:MM.")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if second != 0:
        raise ValueError(f"Seconds must be 00 in '{value}', expected HH:MM.")
    if hour == 24 and minute == 0:
        return 24 * 60
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range '{value}'.")
    return hour * 60 + minute


def parse_date(value: str) -> date:
    cleaned = value.strip()
    try:
        return date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}', expected YYYY-MM-DD.") from exc


def parse_interval(spec: str) -> TimeInterval:
    cleaned = spec.strip()
    if "-" not in cleaned:
        raise ValueError(f"Invalid interval '{spec}', expected HH:MM-HH:MM.")
    start_s, end_s = cleaned.split("-", 1)
    return TimeInterval(hhmm_to_minutes(start_s), hhmm_to_minutes(end_s))


def _expand_day_selector(selector: str) -> List[str]:
    """Expand a weekday selector like 'mon', 'mon-fri', 'mon,wed,fri', or 'all'."""
    raw = selector.strip().lower()
    if raw in ("all", "daily", "*"):
        return list(WEEKDAYS)

    days: List[str] = []
    for token in raw.split(","):
        part = token.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            left_norm = WEEKDAY_ALIASES.get(left.strip())
            right_norm = WEEKDAY_ALIASES.get(right.strip())
            if not left_norm or not right_norm:
                raise ValueError(f"Invalid weekday range '{part}'.")
            idx_l = WEEKDAYS.index(left_norm)
            idx_r = WEEKDAYS.index(right_norm)
            if idx_l <= idx_r:
                span = WEEKDAYS[idx_l : idx_r + 1]
            else:
                span = WEEKDAYS[idx_l:] + WEEKDAYS[: idx_r + 1]
            for d in span:
                if d not in days:
                    days.append(d)
        else:
            norm = WEEKDAY_ALIASES.get(part)
            if not norm:
                raise ValueError(f"Invalid weekday name '{part}'.")
            if norm not in days:
                days.append(norm)

    if not days:
        raise ValueError(f"Empty weekday selector '{selector}'.")
    return days


def _parse_intervals_value(value: Any) -> Tuple[TimeInterval, ...]:
    """Parse working hours or breaks value into sorted non-empty TimeIntervals."""
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in CLOSED_TOKENS:
            return ()
        parts = [p.strip() for p in stripped.split(";") if p.strip()]
        if len(parts) == 1 and "+" in stripped:
            parts = [p.strip() for p in stripped.split("+") if p.strip()]
        intervals = sorted(parse_interval(part) for part in parts)
        return tuple(intervals)
    if isinstance(value, dict):
        if "start" not in value or "end" not in value:
            raise ValueError("Interval object must contain 'start' and 'end' keys.")
        return (TimeInterval(hhmm_to_minutes(str(value["start"])), hhmm_to_minutes(str(value["end"]))),)
    if isinstance(value, list):
        collected: List[TimeInterval] = []
        for item in value:
            collected.extend(_parse_intervals_value(item))
        collected.sort()
        return tuple(collected)
    raise ValueError(f"Unsupported interval specification: {value!r}")


def parse_rules_mapping(data: Dict[str, Any], base: Optional[ScheduleRules] = None) -> ScheduleRules:
    """Merge JSON rule fields ('working_hours', 'breaks', 'buffer_minutes', 'timezone') onto base rules."""
    rules = ScheduleRules(
        working_hours=dict(base.working_hours) if base else dict(ScheduleRules.default().working_hours),
        breaks=dict(base.breaks) if base else dict(ScheduleRules.default().breaks),
        buffer_minutes=base.buffer_minutes if base else 0,
        timezone=base.timezone if base else "UTC",
    )

    if "timezone" in data and data["timezone"] is not None:
        rules.timezone = str(data["timezone"]).strip() or rules.timezone

    if "buffer_minutes" in data and data["buffer_minutes"] is not None:
        try:
            buf = int(data["buffer_minutes"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid buffer_minutes '{data['buffer_minutes']}'.") from exc
        if buf < 0:
            raise ValueError("buffer_minutes must be >= 0.")
        rules.buffer_minutes = buf

    if "working_hours" in data and data["working_hours"] is not None:
        wh_raw = data["working_hours"]
        if isinstance(wh_raw, str):
            intervals = _parse_intervals_value(wh_raw)
            for d in WEEKDAYS:
                rules.working_hours[d] = intervals if d not in ("sat", "sun") else ()
        elif isinstance(wh_raw, dict):
            has_default = "default" in wh_raw
            default_val = _parse_intervals_value(wh_raw["default"]) if has_default else ()
            new_wh: Dict[str, Tuple[TimeInterval, ...]] = {
                day: (default_val if has_default else ()) for day in WEEKDAYS
            }
            for key, val in wh_raw.items():
                key_l = str(key).strip().lower()
                if key_l == "default":
                    continue
                intervals = _parse_intervals_value(val)
                for day in _expand_day_selector(key_l):
                    new_wh[day] = intervals
            rules.working_hours = new_wh
        else:
            raise ValueError("'working_hours' must be an object or string interval.")

    if "breaks" in data and data["breaks"] is not None:
        brk_raw = data["breaks"]
        if isinstance(brk_raw, (list, str)):
            intervals = _parse_intervals_value(brk_raw)
            rules.breaks = {day: intervals for day in WEEKDAYS}
        elif isinstance(brk_raw, dict):
            has_default = "default" in brk_raw
            default_val = _parse_intervals_value(brk_raw["default"]) if has_default else ()
            new_brks: Dict[str, Tuple[TimeInterval, ...]] = {
                day: (default_val if has_default else ()) for day in WEEKDAYS
            }
            for key, val in brk_raw.items():
                key_l = str(key).strip().lower()
                if key_l == "default":
                    continue
                intervals = _parse_intervals_value(val)
                for day in _expand_day_selector(key_l):
                    new_brks[day] = intervals
            rules.breaks = new_brks
        else:
            raise ValueError("'breaks' must be a list, string, or object.")

    return rules


def apply_work_hours_override(rules: ScheduleRules, spec: str) -> None:
    """Apply --work-hours CLI specification to rules in place."""
    cleaned = spec.strip()
    if not cleaned:
        raise ValueError("--work-hours cannot be empty.")

    if "=" not in cleaned:
        intervals = _parse_intervals_value(cleaned)
        for day in WEEKDAYS:
            rules.working_hours[day] = intervals if day not in ("sat", "sun") else ()
        return

    # Support comma-separated clauses like: mon-fri=09:00-18:00,sat=10:00-14:00,sun=closed
    # Careful not to split inside day lists unless separated by '=' clauses.
    clauses = _split_day_clauses(cleaned)
    for day_sel, val_spec in clauses:
        intervals = _parse_intervals_value(val_spec)
        for day in _expand_day_selector(day_sel):
            rules.working_hours[day] = intervals


def apply_day_hours_override(rules: ScheduleRules, spec: str) -> None:
    """Apply repeatable --day-hours SPEC (e.g. 'fri=09:00-17:00' or 'sun=closed')."""
    if "=" not in spec:
        raise ValueError(f"Invalid --day-hours '{spec}', expected DAY=HH:MM-HH:MM or DAY=closed.")
    day_sel, val_spec = spec.split("=", 1)
    intervals = _parse_intervals_value(val_spec)
    for day in _expand_day_selector(day_sel):
        rules.working_hours[day] = intervals


def apply_break_override(rules: ScheduleRules, specs: Sequence[str]) -> None:
    """Apply repeatable --break SPEC ('none' clears breaks; 'HH:MM-HH:MM' or 'day=HH:MM-HH:MM')."""
    for spec in specs:
        cleaned = spec.strip()
        if cleaned.lower() in ("none", "clear", "off"):
            rules.breaks = {day: () for day in WEEKDAYS}
            continue
        if "=" in cleaned:
            day_sel, val_spec = cleaned.split("=", 1)
            intervals = _parse_intervals_value(val_spec)
            for day in _expand_day_selector(day_sel):
                existing = list(rules.breaks.get(day, ()))
                for iv in intervals:
                    if iv not in existing:
                        existing.append(iv)
                existing.sort()
                rules.breaks[day] = tuple(existing)
        else:
            intervals = _parse_intervals_value(cleaned)
            for day in WEEKDAYS:
                existing = list(rules.breaks.get(day, ()))
                for iv in intervals:
                    if iv not in existing:
                        existing.append(iv)
                existing.sort()
                rules.breaks[day] = tuple(existing)


def _split_day_clauses(spec: str) -> List[Tuple[str, str]]:
    """Split 'mon,wed=09:00-17:00,fri=09:00-15:00' cleanly by detecting '=...' boundaries."""
    tokens = [part.strip() for part in spec.split(",") if part.strip()]
    clauses: List[Tuple[str, str]] = []
    pending_days: List[str] = []
    for token in tokens:
        if "=" in token:
            day_part, val_part = token.split("=", 1)
            pending_days.append(day_part.strip())
            clauses.append((",".join(pending_days), val_part.strip()))
            pending_days = []
        else:
            pending_days.append(token)
    if pending_days:
        raise ValueError(
            f"Invalid --work-hours clause '{','.join(pending_days)}': missing '=HH:MM-HH:MM' or '=closed'."
        )
    return clauses


def _extract_iso_date_and_minutes(value: str) -> Tuple[Optional[date], int]:
    """Parse either 'HH:MM' or 'YYYY-MM-DDTHH:MM[:SS]' / 'YYYY-MM-DD HH:MM[:SS]'."""
    cleaned = value.strip()
    if "T" in cleaned or (" " in cleaned and "-" in cleaned.split(" ", 1)[0]):
        sep = "T" if "T" in cleaned else " "
        date_part, time_part = cleaned.split(sep, 1)
        # Strip optional trailing Z or timezone offset if present
        if time_part.endswith(("Z", "z")):
            time_part = time_part[:-1]
        for tz_sep in ("+", "-"):
            if tz_sep in time_part:
                time_part = time_part.split(tz_sep, 1)[0]
        parsed_date = parse_date(date_part)
        return parsed_date, hhmm_to_minutes(time_part)
    return None, hhmm_to_minutes(cleaned)


def _first_present(row: Dict[str, Any], keys: Sequence[str], default: str = "") -> str:
    for key in keys:
        if key in row and row[key] is not None:
            val = str(row[key]).strip()
            if val != "":
                return val
    return default


def _parse_booking_record(raw: Dict[str, Any], index: int) -> Booking:
    norm_map = {str(k).strip().lower(): v for k, v in raw.items()}

    booking_id = _first_present(
        norm_map, ("id", "booking_id", "appointment_id", "ref"), f"ROW-{index + 1}"
    )
    client = _first_present(
        norm_map, ("client", "client_name", "customer", "customer_name", "patient", "name", "phone", "email")
    )
    service = _first_present(norm_map, ("service", "service_name", "type", "title"))
    staff = _first_present(norm_map, ("staff", "provider", "specialist", "doctor", "resource"))
    status = _first_present(norm_map, ("status", "state"), "confirmed")

    date_str = _first_present(norm_map, ("date", "booking_date", "appointment_date", "day"))
    explicit_date: Optional[date] = parse_date(date_str) if date_str else None

    slot_str = _first_present(norm_map, ("slot", "time_slot", "interval"))
    start_str = _first_present(norm_map, ("start", "start_time", "from"))
    end_str = _first_present(norm_map, ("end", "end_time", "to"))
    duration_str = _first_present(norm_map, ("duration_minutes", "duration", "duration_min", "minutes"))

    start_date: Optional[date] = None
    end_date: Optional[date] = None

    if slot_str and not start_str:
        iv = parse_interval(slot_str)
        start_min, end_min = iv.start, iv.end
    else:
        if not start_str:
            raise ValueError(f"Booking '{booking_id}' is missing 'start' time.")
        start_date, start_min = _extract_iso_date_and_minutes(start_str)

        if end_str:
            end_date, end_min = _extract_iso_date_and_minutes(end_str)
        elif duration_str:
            try:
                dur = int(float(duration_str))
            except ValueError as exc:
                raise ValueError(f"Booking '{booking_id}' has invalid duration '{duration_str}'.") from exc
            if dur <= 0:
                raise ValueError(f"Booking '{booking_id}' duration must be > 0.")
            end_min = start_min + dur
            end_date = start_date
            if end_min > 24 * 60:
                raise ValueError(
                    f"Booking '{booking_id}' exceeds 24:00 boundary ({minutes_to_hhmm(start_min)} + {dur}m)."
                )
        else:
            raise ValueError(f"Booking '{booking_id}' requires either 'end' time or 'duration_minutes'.")

    resolved_date = explicit_date or start_date or end_date
    if resolved_date is None:
        raise ValueError(f"Booking '{booking_id}' is missing a 'date' (YYYY-MM-DD).")
    if start_date is not None and explicit_date is not None and start_date != explicit_date:
        raise ValueError(
            f"Booking '{booking_id}' date mismatch: date={explicit_date.isoformat()} vs start={start_date.isoformat()}."
        )
    if end_date is not None and end_date != resolved_date:
        raise ValueError(
            f"Booking '{booking_id}' spans multiple calendar dates ({resolved_date.isoformat()} to {end_date.isoformat()})."
        )

    # Validate [start_min, end_min)
    TimeInterval(start_min, end_min)

    return Booking(
        booking_id=booking_id,
        booking_date=resolved_date,
        start_min=start_min,
        end_min=end_min,
        client=client,
        service=service,
        staff=staff,
        status=status,
        source_index=index,
    )


def load_schedule_text(text: str, format_hint: str = "auto") -> Tuple[List[Booking], ScheduleRules]:
    """Load bookings and optional embedded rules from JSON or CSV text."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        raise ValueError("Schedule input is empty.")

    fmt = format_hint.lower()
    if fmt == "auto":
        fmt = "json" if stripped[0] in ("{", "[") else "csv"

    rules = ScheduleRules.default()
    bookings: List[Booking] = []

    if fmt == "json":
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON schedule: {exc}") from exc

        if isinstance(payload, list):
            raw_bookings = payload
        elif isinstance(payload, dict):
            rules = parse_rules_mapping(payload, base=rules)
            raw_bookings = None
            for key in ("bookings", "appointments", "slots", "items"):
                if key in payload:
                    raw_bookings = payload[key]
                    break
            if raw_bookings is None:
                raw_bookings = []
            if not isinstance(raw_bookings, list):
                raise ValueError("Schedule JSON bookings field must be a list.")
        else:
            raise ValueError("Schedule JSON root must be an object or array of bookings.")

        for idx, item in enumerate(raw_bookings):
            if not isinstance(item, dict):
                raise ValueError(f"Booking record at index {idx} must be a JSON object.")
            bookings.append(_parse_booking_record(item, idx))
        return bookings, rules

    if fmt == "csv":
        clean_text = text.lstrip("\ufeff")
        sample = "\n".join(clean_text.splitlines()[:5])
        delimiter = ","
        if ";" in sample and "," not in sample:
            delimiter = ";"
        reader = csv.DictReader(io.StringIO(clean_text), delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("CSV schedule has no header row.")
        for idx, row in enumerate(reader):
            if row is None:
                continue
            if all((v is None or str(v).strip() == "") for v in row.values()):
                continue
            bookings.append(_parse_booking_record(row, idx))
        return bookings, rules

    raise ValueError(f"Unsupported schedule format hint '{format_hint}'.")


def load_schedule_file(path: Path) -> Tuple[List[Booking], ScheduleRules]:
    """Load schedule from a .json or .csv file path (or '-' for stdin)."""
    if str(path) == "-":
        return load_schedule_text(sys.stdin.read(), format_hint="auto")
    if not path.is_file():
        raise ValueError(f"Schedule file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    hint = "json" if suffix == ".json" else "csv" if suffix == ".csv" else "auto"
    return load_schedule_text(text, format_hint=hint)


def _normalize_client(client: str) -> str:
    return " ".join(client.strip().lower().split())


def audit_schedule(
    bookings: Iterable[Booking],
    rules: ScheduleRules,
    target_date: Optional[date] = None,
    include_cancelled: bool = False,
    by_staff: bool = False,
    staff_filter: Optional[str] = None,
) -> AuditReport:
    """Audit bookings for overlaps, buffer violations, off-hours, break conflicts, and duplicate clients."""
    all_bookings = list(bookings)
    if target_date is not None:
        all_bookings = [b for b in all_bookings if b.booking_date == target_date]
    if staff_filter:
        staff_norm = staff_filter.strip().lower()
        all_bookings = [b for b in all_bookings if b.staff.strip().lower() == staff_norm]

    active = [b for b in all_bookings if include_cancelled or b.is_active]
    cancelled_count = sum(1 for b in all_bookings if not b.is_active)

    active.sort(key=lambda b: (b.booking_date, b.start_min, b.end_min, b.booking_id))
    issues: List[AuditIssue] = []

    # 1. Per-booking checks: off-hours and break conflicts
    for b in active:
        weekday_key = WEEKDAYS[b.booking_date.weekday()]
        weekday_label = WEEKDAY_LABELS[weekday_key]
        work_windows = rules.working_hours.get(weekday_key, ())
        break_windows = rules.breaks.get(weekday_key, ())

        if not work_windows:
            issues.append(
                AuditIssue(
                    severity="ERROR",
                    code="OFF_HOURS",
                    issue_date=b.booking_date,
                    window=b.window,
                    booking_ids=(b.booking_id,),
                    clients=(b.client,) if b.client else (),
                    message=f"Booking {b.booking_id} ({b.window}) is scheduled on a closed day ({weekday_label}).",
                    sort_start=b.start_min,
                )
            )
        else:
            b_iv = b.interval
            if not any(w.contains(b_iv) for w in work_windows):
                allowed_str = ", ".join(w.format_hhmm() for w in work_windows)
                issues.append(
                    AuditIssue(
                        severity="ERROR",
                        code="OFF_HOURS",
                        issue_date=b.booking_date,
                        window=b.window,
                        booking_ids=(b.booking_id,),
                        clients=(b.client,) if b.client else (),
                        message=(
                            f"Booking {b.booking_id} ({b.window}) falls outside {weekday_label} "
                            f"working hours ({allowed_str})."
                        ),
                        sort_start=b.start_min,
                    )
                )

        b_iv = b.interval
        for brk in break_windows:
            if b_iv.overlaps(brk):
                issues.append(
                    AuditIssue(
                        severity="ERROR",
                        code="BREAK_CONFLICT",
                        issue_date=b.booking_date,
                        window=b.window,
                        booking_ids=(b.booking_id,),
                        clients=(b.client,) if b.client else (),
                        message=(
                            f"Booking {b.booking_id} ({b.window}) overlaps scheduled break "
                            f"({brk.format_hhmm()})."
                        ),
                        sort_start=b.start_min,
                    )
                )

    # 2. Pairwise checks per (date, optional staff): overlaps and buffer gaps
    groups: Dict[Tuple[date, str], List[Booking]] = {}
    for b in active:
        key = (b.booking_date, b.staff.strip().lower() if by_staff else "")
        groups.setdefault(key, []).append(b)

    for (d, _staff_key), day_bookings in groups.items():
        n = len(day_bookings)
        # Overlaps
        for i in range(n):
            a = day_bookings[i]
            for j in range(i + 1, n):
                b = day_bookings[j]
                if b.start_min >= a.end_min:
                    break
                overlap_start = b.start_min
                overlap_end = min(a.end_min, b.end_min)
                overlap_min = overlap_end - overlap_start
                clients = tuple(c for c in (a.client, b.client) if c)
                issues.append(
                    AuditIssue(
                        severity="ERROR",
                        code="OVERLAP",
                        issue_date=d,
                        window=f"{minutes_to_hhmm(overlap_start)}-{minutes_to_hhmm(overlap_end)}",
                        booking_ids=(a.booking_id, b.booking_id),
                        clients=clients,
                        message=(
                            f"Bookings {a.booking_id} ({a.window}) and {b.booking_id} ({b.window}) "
                            f"overlap by {overlap_min}m."
                        ),
                        sort_start=overlap_start,
                    )
                )

        # Buffer violations
        if rules.buffer_minutes > 0:
            for i in range(n):
                a = day_bookings[i]
                for j in range(i + 1, n):
                    b = day_bookings[j]
                    if b.start_min < a.end_min:
                        continue
                    gap = b.start_min - a.end_min
                    if gap >= rules.buffer_minutes:
                        break
                    # Avoid redundant transitive alerts when another booking sits strictly between a and b
                    has_intermediate = any(
                        k != i
                        and k != j
                        and a.end_min <= day_bookings[k].start_min
                        and day_bookings[k].end_min <= b.start_min
                        for k in range(n)
                    )
                    if has_intermediate:
                        continue
                    clients = tuple(c for c in (a.client, b.client) if c)
                    issues.append(
                        AuditIssue(
                            severity="WARN",
                            code="BUFFER_VIOLATION",
                            issue_date=d,
                            window=f"{minutes_to_hhmm(a.end_min)}-{minutes_to_hhmm(b.start_min)}",
                            booking_ids=(a.booking_id, b.booking_id),
                            clients=clients,
                            message=(
                                f"Gap between {a.booking_id} (ends {minutes_to_hhmm(a.end_min)}) and "
                                f"{b.booking_id} (starts {minutes_to_hhmm(b.start_min)}) is {gap}m, "
                                f"below required {rules.buffer_minutes}m buffer."
                            ),
                            sort_start=a.end_min,
                        )
                    )

    # 3. Duplicate client bookings on the same date
    client_groups: Dict[Tuple[date, str], List[Booking]] = {}
    for b in active:
        norm_client = _normalize_client(b.client)
        if not norm_client:
            continue
        client_groups.setdefault((b.booking_date, norm_client), []).append(b)

    for (d, _norm_client), client_bookings in client_groups.items():
        if len(client_bookings) > 1:
            display_client = client_bookings[0].client
            refs = ", ".join(f"{b.booking_id} ({b.window})" for b in client_bookings)
            issues.append(
                AuditIssue(
                    severity="WARN",
                    code="DUPLICATE_CLIENT",
                    issue_date=d,
                    window=client_bookings[0].window,
                    booking_ids=tuple(b.booking_id for b in client_bookings),
                    clients=(display_client,),
                    message=f"Client '{display_client}' has {len(client_bookings)} bookings on {d.isoformat()}: {refs}.",
                    sort_start=client_bookings[0].start_min,
                )
            )

    issues.sort(
        key=lambda item: (
            item.issue_date,
            item.sort_start,
            SEVERITY_RANK.get(item.severity, 9),
            item.code,
            item.booking_ids,
        )
    )

    return AuditReport(
        total_bookings=len(all_bookings),
        active_bookings=len(active),
        cancelled_bookings=cancelled_count,
        buffer_minutes=rules.buffer_minutes,
        issues=tuple(issues),
        target_date=target_date,
    )


def generate_free_slots(
    bookings: Iterable[Booking],
    rules: ScheduleRules,
    target_date: date,
    duration_minutes: int = 60,
    step_minutes: Optional[int] = None,
    min_start_min: Optional[int] = None,
    include_cancelled: bool = False,
    staff_filter: Optional[str] = None,
) -> FreeSlotsReport:
    """Generate available booking intervals for target_date respecting work hours, breaks, bookings, and buffers."""
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be > 0.")
    step = duration_minutes if step_minutes is None else step_minutes
    if step <= 0:
        raise ValueError("step_minutes must be > 0.")
    if rules.buffer_minutes < 0:
        raise ValueError("buffer_minutes must be >= 0.")

    weekday_key = WEEKDAYS[target_date.weekday()]
    work_windows = rules.working_hours.get(weekday_key, ())
    break_windows = rules.breaks.get(weekday_key, ())

    if not work_windows:
        return FreeSlotsReport(
            target_date=target_date,
            weekday=weekday_key,
            closed=True,
            working_hours=(),
            breaks=break_windows,
            duration_minutes=duration_minutes,
            step_minutes=step,
            buffer_minutes=rules.buffer_minutes,
            slots=(),
            staff=staff_filter,
        )

    day_bookings = [
        b
        for b in bookings
        if b.booking_date == target_date
        and (include_cancelled or b.is_active)
        and (not staff_filter or b.staff.strip().lower() == staff_filter.strip().lower())
    ]

    blocked_booking_windows: List[TimeInterval] = []
    for bk in day_bookings:
        padded_start = max(0, bk.start_min - rules.buffer_minutes)
        padded_end = min(24 * 60, bk.end_min + rules.buffer_minutes)
        blocked_booking_windows.append(TimeInterval(padded_start, padded_end))

    available: List[TimeInterval] = []
    for work_win in sorted(work_windows):
        cursor = work_win.start
        while cursor + duration_minutes <= work_win.end:
            if min_start_min is not None and cursor < min_start_min:
                cursor += step
                continue

            candidate = TimeInterval(cursor, cursor + duration_minutes)
            if any(candidate.overlaps(brk) for brk in break_windows):
                cursor += step
                continue
            if any(candidate.overlaps(blk) for blk in blocked_booking_windows):
                cursor += step
                continue

            available.append(candidate)
            cursor += step

    return FreeSlotsReport(
        target_date=target_date,
        weekday=weekday_key,
        closed=False,
        working_hours=tuple(sorted(work_windows)),
        breaks=tuple(sorted(break_windows)),
        duration_minutes=duration_minutes,
        step_minutes=step,
        buffer_minutes=rules.buffer_minutes,
        slots=tuple(available),
        staff=staff_filter,
    )


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    sep_line = "-+-".join("-" * widths[i] for i in range(len(headers)))
    body_lines = [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    return "\n".join([header_line, sep_line, *body_lines])


def format_audit_text(report: AuditReport) -> str:
    status_label = "PASS" if report.passed else "FAIL"
    date_part = f" | Date: {report.target_date.isoformat()}" if report.target_date else ""
    summary_line = (
        f"Schedule Audit Summary{date_part} | "
        f"Bookings: {report.total_bookings} (active: {report.active_bookings}, cancelled: {report.cancelled_bookings}) | "
        f"Buffer: {report.buffer_minutes}m | Errors: {report.error_count} | Warnings: {report.warning_count} | "
        f"Status: {status_label}"
    )

    if not report.issues:
        return f"{summary_line}\n\nNo schedule conflicts or policy violations detected."

    rows = [
        [
            issue.severity,
            issue.code,
            issue.issue_date.isoformat(),
            issue.window,
            ", ".join(issue.booking_ids),
            issue.message,
        ]
        for issue in report.issues
    ]
    table = _format_table(("Severity", "Code", "Date", "Window", "Bookings", "Details"), rows)
    return f"{summary_line}\n\n{table}"


def format_free_slots_text(report: FreeSlotsReport) -> str:
    weekday_label = WEEKDAY_LABELS[report.weekday]
    work_str = ", ".join(w.format_hhmm() for w in report.working_hours) if report.working_hours else "CLOSED"
    breaks_str = ", ".join(b.format_hhmm() for b in report.breaks) if report.breaks else "none"
    staff_part = f" | Staff: {report.staff}" if report.staff else ""
    header = (
        f"Free Slots | Date: {report.target_date.isoformat()} ({weekday_label}){staff_part} | "
        f"Work: {work_str} | Breaks: {breaks_str} | Duration: {report.duration_minutes}m | "
        f"Step: {report.step_minutes}m | Buffer: {report.buffer_minutes}m | Available: {len(report.slots)}"
    )

    if report.closed:
        return f"{header}\n\nNo available slots: {weekday_label} is configured as closed."
    if not report.slots:
        return f"{header}\n\nNo available {report.duration_minutes}m slots on {report.target_date.isoformat()}."

    rows = [
        [
            str(idx + 1),
            report.target_date.isoformat(),
            minutes_to_hhmm(slot.start),
            minutes_to_hhmm(slot.end),
            f"{slot.duration_minutes}m",
        ]
        for idx, slot in enumerate(report.slots)
    ]
    table = _format_table(("#", "Date", "Start", "End", "Duration"), rows)
    return f"{header}\n\n{table}"


def format_audit_csv(report: AuditReport) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=("severity", "code", "date", "window", "booking_ids", "clients", "message"),
        lineterminator="\n",
    )
    writer.writeheader()
    for issue in report.issues:
        writer.writerow(
            {
                "severity": issue.severity,
                "code": issue.code,
                "date": issue.issue_date.isoformat(),
                "window": issue.window,
                "booking_ids": ";".join(issue.booking_ids),
                "clients": ";".join(issue.clients),
                "message": issue.message,
            }
        )
    return buf.getvalue().rstrip("\n")


def format_free_slots_csv(report: FreeSlotsReport) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=("date", "weekday", "start", "end", "label", "duration_minutes"),
        lineterminator="\n",
    )
    writer.writeheader()
    for slot in report.slots:
        writer.writerow(
            {
                "date": report.target_date.isoformat(),
                "weekday": report.weekday,
                "start": minutes_to_hhmm(slot.start),
                "end": minutes_to_hhmm(slot.end),
                "label": slot.format_hhmm(),
                "duration_minutes": slot.duration_minutes,
            }
        )
    return buf.getvalue().rstrip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slot_auditor.py",
        description=(
            "Zero-dependency CLI to audit appointment schedules for overlapping slots, "
            "buffer violations, off-hours bookings, duplicate clients, and free time-slot generation."
        ),
    )
    parser.add_argument(
        "schedule",
        nargs="?",
        help="Path to JSON or CSV booking schedule file (use '-' for stdin).",
    )
    parser.add_argument(
        "-s",
        "--schedule",
        dest="schedule_opt",
        metavar="PATH",
        help="Path to JSON or CSV booking schedule file (alternative to positional argument).",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit schedule for overlaps, buffer violations, off-hours, break conflicts, and duplicate clients (default action).",
    )
    parser.add_argument(
        "--free-slots",
        action="store_true",
        help="Generate available free time slots for --date and --duration.",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Target date for --free-slots (or filter date for --audit).",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        metavar="MINUTES",
        help="Service duration in minutes for --free-slots (default: 60).",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Slot grid step in minutes for --free-slots (default: same as --duration).",
    )
    parser.add_argument(
        "--min-start",
        metavar="HH:MM",
        help="Earliest allowed slot start time for --free-slots.",
    )
    parser.add_argument(
        "--rules",
        metavar="PATH",
        help="Optional JSON rules file defining working_hours, breaks, and buffer_minutes.",
    )
    parser.add_argument(
        "--work-hours",
        metavar="SPEC",
        help="Override working hours (e.g. '09:00-18:00' or 'mon-fri=09:00-18:00,sat=10:00-14:00,sun=closed').",
    )
    parser.add_argument(
        "--day-hours",
        action="append",
        default=[],
        metavar="DAY=SPEC",
        help="Override working hours for specific weekday(s) (e.g. 'fri=09:00-17:00' or 'sun=closed'). Repeatable.",
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Override turnaround buffer minutes between appointments.",
    )
    parser.add_argument(
        "--break",
        dest="breaks",
        action="append",
        default=[],
        metavar="SPEC",
        help="Add break interval ('HH:MM-HH:MM' or 'day=HH:MM-HH:MM') or 'none' to clear breaks. Repeatable.",
    )
    parser.add_argument(
        "--staff",
        metavar="NAME",
        help="Filter bookings and free-slot calculation to a specific staff/resource name.",
    )
    parser.add_argument(
        "--by-staff",
        action="store_true",
        help="Evaluate overlaps and buffer gaps separately per staff/resource name.",
    )
    parser.add_argument(
        "--include-cancelled",
        action="store_true",
        help="Include cancelled/rejected bookings in audit and slot blocking.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="Output format: text (default), json, or csv.",
    )
    parser.add_argument(
        "--fail-on",
        choices=("none", "error", "warn"),
        default="none",
        help="Exit with code 1 when --audit finds 'error', 'warn' (or error), or 'none' (default).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    schedule_path_raw: Optional[str] = None
    if args.schedule and args.schedule_opt and args.schedule != args.schedule_opt:
        print("Error: Provide schedule via positional argument OR --schedule, not two different paths.", file=sys.stderr)
        return 2
    schedule_path_raw = args.schedule_opt or args.schedule
    if not schedule_path_raw:
        parser.print_usage(sys.stderr)
        print("Error: Schedule file path is required (use '-' for stdin).", file=sys.stderr)
        return 2

    run_audit = bool(args.audit)
    run_free = bool(args.free_slots)
    if not run_audit and not run_free:
        run_audit = True

    if run_audit and run_free and args.format == "csv":
        print("Error: --format csv supports either --audit or --free-slots, not both together.", file=sys.stderr)
        return 2

    try:
        bookings, rules = load_schedule_file(Path(schedule_path_raw) if schedule_path_raw != "-" else Path("-"))

        if args.rules:
            rules_path = Path(args.rules)
            if not rules_path.is_file():
                raise ValueError(f"Rules file not found: {rules_path}")
            try:
                rules_json = json.loads(rules_path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in rules file '{rules_path}': {exc}") from exc
            if not isinstance(rules_json, dict):
                raise ValueError("Rules file JSON root must be an object.")
            rules = parse_rules_mapping(rules_json, base=rules)

        if args.work_hours is not None:
            apply_work_hours_override(rules, args.work_hours)
        for day_spec in args.day_hours:
            apply_day_hours_override(rules, day_spec)
        if args.buffer is not None:
            if args.buffer < 0:
                raise ValueError("--buffer must be >= 0.")
            rules.buffer_minutes = args.buffer
        if args.breaks:
            apply_break_override(rules, args.breaks)

        target_date = parse_date(args.date) if args.date else None
        min_start_min = hhmm_to_minutes(args.min_start) if args.min_start else None

        audit_rep: Optional[AuditReport] = None
        free_rep: Optional[FreeSlotsReport] = None

        if run_audit:
            audit_rep = audit_schedule(
                bookings=bookings,
                rules=rules,
                target_date=target_date,
                include_cancelled=args.include_cancelled,
                by_staff=args.by_staff,
                staff_filter=args.staff,
            )

        if run_free:
            free_date = target_date
            if free_date is None:
                if not bookings:
                    raise ValueError("--date YYYY-MM-DD is required for --free-slots when schedule has no bookings.")
                free_date = min(b.booking_date for b in bookings)
            free_rep = generate_free_slots(
                bookings=bookings,
                rules=rules,
                target_date=free_date,
                duration_minutes=args.duration,
                step_minutes=args.step,
                min_start_min=min_start_min,
                include_cancelled=args.include_cancelled,
                staff_filter=args.staff,
            )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        if audit_rep is not None and free_rep is not None:
            out_payload = {
                "mode": "audit_and_free_slots",
                "audit": audit_rep.to_dict(),
                "free_slots": free_rep.to_dict(),
            }
        elif audit_rep is not None:
            out_payload = audit_rep.to_dict()
        else:
            assert free_rep is not None
            out_payload = free_rep.to_dict()
        print(json.dumps(out_payload, indent=2))
    elif args.format == "csv":
        if audit_rep is not None:
            print(format_audit_csv(audit_rep))
        else:
            assert free_rep is not None
            print(format_free_slots_csv(free_rep))
    else:
        sections: List[str] = []
        if audit_rep is not None:
            sections.append(format_audit_text(audit_rep))
        if free_rep is not None:
            sections.append(format_free_slots_text(free_rep))
        print("\n\n".join(sections))

    if audit_rep is not None:
        if args.fail_on == "error" and audit_rep.error_count > 0:
            return 1
        if args.fail_on == "warn" and (audit_rep.error_count > 0 or audit_rep.warning_count > 0):
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
