"""Offline unit & CLI integration tests for slot_auditor.py."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import slot_auditor as sa  # noqa: E402


class TestParsingAndRules(unittest.TestCase):
    def test_load_json_dict_with_embedded_rules(self) -> None:
        raw = json.dumps(
            {
                "timezone": "Europe/Berlin",
                "buffer_minutes": 10,
                "working_hours": {
                    "mon-fri": "08:30-17:30",
                    "sat": "10:00-14:00",
                    "sun": None,
                },
                "breaks": {"default": ["12:30-13:30"], "fri": ["12:00-12:45"]},
                "bookings": [
                    {
                        "id": "A1",
                        "date": "2026-04-13",
                        "start": "09:00",
                        "end": "10:00",
                        "client": "Nina Patel",
                    }
                ],
            }
        )
        bookings, rules = sa.load_schedule_text(raw)
        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0].booking_id, "A1")
        self.assertEqual(rules.timezone, "Europe/Berlin")
        self.assertEqual(rules.buffer_minutes, 10)
        self.assertEqual(rules.working_hours["mon"], (sa.TimeInterval(510, 1050),))
        self.assertEqual(rules.working_hours["sat"], (sa.TimeInterval(600, 840),))
        self.assertEqual(rules.working_hours["sun"], ())
        self.assertEqual(rules.breaks["mon"], (sa.TimeInterval(750, 810),))
        self.assertEqual(rules.breaks["fri"], (sa.TimeInterval(720, 765),))

    def test_load_json_top_level_list_iso_and_duration(self) -> None:
        raw = json.dumps(
            [
                {
                    "booking_id": "ISO-1",
                    "start": "2026-04-14T10:00:00",
                    "duration_minutes": 45,
                    "customer": "Liam Chen",
                },
                {
                    "id": "SLOT-2",
                    "date": "2026-04-14",
                    "slot": "11:30-12:15",
                    "client_name": "Maya Singh",
                },
            ]
        )
        bookings, rules = sa.load_schedule_text(raw)
        self.assertEqual(len(bookings), 2)
        self.assertEqual(bookings[0].booking_date, date(2026, 4, 14))
        self.assertEqual(bookings[0].window, "10:00-10:45")
        self.assertEqual(bookings[0].client, "Liam Chen")
        self.assertEqual(bookings[1].window, "11:30-12:15")
        self.assertEqual(bookings[1].client, "Maya Singh")
        self.assertEqual(rules.buffer_minutes, 0)

    def test_load_csv_schedule_with_bom_and_aliases(self) -> None:
        csv_text = (
            "\ufeffid,appointment_date,start_time,end_time,client_name,service,status\n"
            "C1,2026-04-15,09:00,09:45,Omar Ali,Consult,confirmed\n"
            "\n"
            "C2,2026-04-15,10:00,11:00,Zoe Wu,Review,cancelled\n"
        )
        bookings, _ = sa.load_schedule_text(csv_text, format_hint="csv")
        self.assertEqual(len(bookings), 2)
        self.assertEqual(bookings[0].booking_id, "C1")
        self.assertEqual(bookings[0].window, "09:00-09:45")
        self.assertTrue(bookings[0].is_active)
        self.assertFalse(bookings[1].is_active)

    def test_cli_rule_overrides_work_hours_day_hours_and_breaks(self) -> None:
        rules = sa.ScheduleRules.default()
        sa.apply_work_hours_override(rules, "mon,wed,fri=08:00-16:00,sat=10:00-13:00,sun=closed")
        sa.apply_day_hours_override(rules, "fri=08:00-14:00")
        sa.apply_break_override(rules, ["12:00-12:30", "sat=11:00-11:15"])
        self.assertEqual(rules.working_hours["mon"], (sa.TimeInterval(480, 960),))
        self.assertEqual(rules.working_hours["fri"], (sa.TimeInterval(480, 840),))
        self.assertEqual(rules.working_hours["sat"], (sa.TimeInterval(600, 780),))
        self.assertEqual(rules.working_hours["sun"], ())
        self.assertEqual(rules.breaks["mon"], (sa.TimeInterval(720, 750),))
        self.assertEqual(
            rules.breaks["sat"],
            (sa.TimeInterval(660, 675), sa.TimeInterval(720, 750)),
        )
        sa.apply_break_override(rules, ["none"])
        self.assertEqual(rules.breaks["sat"], ())

    def test_invalid_interval_and_booking_errors(self) -> None:
        with self.assertRaises(ValueError):
            sa.parse_interval("14:00-13:00")
        with self.assertRaises(ValueError):
            sa.hhmm_to_minutes("25:00")
        with self.assertRaises(ValueError):
            sa.load_schedule_text('{"bookings": [{"id": "X", "date": "2026-04-13", "start": "10:00"}]}')


class TestAuditChecks(unittest.TestCase):
    def test_overlap_detection_and_touching_boundaries(self) -> None:
        rules = sa.ScheduleRules.default()
        bookings = [
            sa.Booking("B1", date(2026, 4, 13), 9 * 60, 10 * 60, "Client A"),
            sa.Booking("B2", date(2026, 4, 13), 10 * 60, 11 * 60, "Client B"),  # touches B1 cleanly
            sa.Booking("B3", date(2026, 4, 13), 10 * 60 + 30, 11 * 60 + 30, "Client C"),  # overlaps B2
            sa.Booking("B4", date(2026, 4, 13), 10 * 60 + 40, 10 * 60 + 55, "Client D"),  # nested inside B2 & B3
        ]
        report = sa.audit_schedule(bookings, rules)
        overlaps = [i for i in report.issues if i.code == "OVERLAP"]
        self.assertEqual(len(overlaps), 3)  # (B2,B3), (B2,B4), (B3,B4)
        pairs = {i.booking_ids for i in overlaps}
        self.assertIn(("B2", "B3"), pairs)
        self.assertIn(("B2", "B4"), pairs)
        self.assertIn(("B3", "B4"), pairs)
        self.assertNotIn(("B1", "B2"), pairs)

    def test_buffer_violation_exact_vs_short_gap(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.buffer_minutes = 15
        bookings = [
            sa.Booking("B1", date(2026, 4, 13), 9 * 60, 10 * 60, "A"),
            sa.Booking("B2", date(2026, 4, 13), 10 * 60 + 10, 11 * 60, "B"),  # 10m gap -> violation
            sa.Booking("B3", date(2026, 4, 13), 11 * 60 + 15, 12 * 60, "C"),  # 15m gap -> OK
        ]
        report = sa.audit_schedule(bookings, rules)
        buf_issues = [i for i in report.issues if i.code == "BUFFER_VIOLATION"]
        self.assertEqual(len(buf_issues), 1)
        self.assertEqual(buf_issues[0].booking_ids, ("B1", "B2"))
        self.assertIn("10m", buf_issues[0].message)

    def test_buffer_violation_with_overlapping_long_booking(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.buffer_minutes = 15
        bookings = [
            sa.Booking("B1", date(2026, 4, 13), 9 * 60, 11 * 60, "A"),
            sa.Booking("B2", date(2026, 4, 13), 9 * 60 + 30, 10 * 60, "B"),  # overlaps B1
            sa.Booking("B3", date(2026, 4, 13), 11 * 60 + 5, 12 * 60, "C"),  # 5m after B1
        ]
        report = sa.audit_schedule(bookings, rules)
        codes = [i.code for i in report.issues]
        self.assertIn("OVERLAP", codes)
        buf_issues = [i for i in report.issues if i.code == "BUFFER_VIOLATION"]
        self.assertEqual(len(buf_issues), 1)
        self.assertEqual(buf_issues[0].booking_ids, ("B1", "B3"))

    def test_off_hours_early_late_and_closed_weekday(self) -> None:
        rules = sa.ScheduleRules.default()
        bookings = [
            sa.Booking("EARLY", date(2026, 4, 13), 8 * 60 + 30, 9 * 60 + 30, "A"),
            sa.Booking("LATE", date(2026, 4, 13), 17 * 60 + 30, 18 * 60 + 30, "B"),
            sa.Booking("SUN1", date(2026, 4, 19), 11 * 60, 12 * 60, "C"),
        ]
        report = sa.audit_schedule(bookings, rules)
        off = [i for i in report.issues if i.code == "OFF_HOURS"]
        self.assertEqual(len(off), 3)
        self.assertEqual({i.booking_ids[0] for i in off}, {"EARLY", "LATE", "SUN1"})

    def test_break_conflict_detection(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.breaks = {d: (sa.TimeInterval(13 * 60, 14 * 60),) for d in sa.WEEKDAYS}
        bookings = [
            sa.Booking("OK", date(2026, 4, 13), 12 * 60, 13 * 60, "A"),
            sa.Booking("BRK", date(2026, 4, 13), 12 * 60 + 45, 13 * 60 + 15, "B"),
        ]
        report = sa.audit_schedule(bookings, rules)
        brk = [i for i in report.issues if i.code == "BREAK_CONFLICT"]
        self.assertEqual(len(brk), 1)
        self.assertEqual(brk[0].booking_ids, ("BRK",))

    def test_duplicate_client_same_day_vs_different_days(self) -> None:
        rules = sa.ScheduleRules.default()
        bookings = [
            sa.Booking("B1", date(2026, 4, 13), 9 * 60, 10 * 60, "Alice   Carter"),
            sa.Booking("B2", date(2026, 4, 13), 14 * 60, 15 * 60, "alice carter"),
            sa.Booking("B3", date(2026, 4, 14), 10 * 60, 11 * 60, "Alice Carter"),
        ]
        report = sa.audit_schedule(bookings, rules)
        dups = [i for i in report.issues if i.code == "DUPLICATE_CLIENT"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].booking_ids, ("B1", "B2"))
        self.assertEqual(dups[0].issue_date, date(2026, 4, 13))

    def test_cancelled_bookings_ignored_unless_included(self) -> None:
        rules = sa.ScheduleRules.default()
        bookings = [
            sa.Booking("B1", date(2026, 4, 13), 9 * 60, 10 * 60, "A", status="confirmed"),
            sa.Booking("B2", date(2026, 4, 13), 9 * 60, 10 * 60, "A", status="cancelled"),
        ]
        rep_default = sa.audit_schedule(bookings, rules, include_cancelled=False)
        self.assertTrue(rep_default.passed)
        self.assertEqual(rep_default.cancelled_bookings, 1)

        rep_inc = sa.audit_schedule(bookings, rules, include_cancelled=True)
        self.assertFalse(rep_inc.passed)
        self.assertEqual(rep_inc.error_count, 1)
        self.assertEqual(rep_inc.warning_count, 1)

    def test_by_staff_isolation_vs_global_check(self) -> None:
        rules = sa.ScheduleRules.default()
        bookings = [
            sa.Booking("B1", date(2026, 4, 13), 10 * 60, 11 * 60, "A", staff="Dr. Lee"),
            sa.Booking("B2", date(2026, 4, 13), 10 * 60, 11 * 60, "B", staff="Dr. Patel"),
        ]
        global_rep = sa.audit_schedule(bookings, rules, by_staff=False)
        self.assertEqual(global_rep.error_count, 1)

        staff_rep = sa.audit_schedule(bookings, rules, by_staff=True)
        self.assertTrue(staff_rep.passed)


class TestFreeSlotGeneration(unittest.TestCase):
    def test_free_slots_basic_empty_day(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.working_hours["mon"] = (sa.TimeInterval(9 * 60, 12 * 60),)
        rep = sa.generate_free_slots([], rules, date(2026, 4, 13), duration_minutes=60)
        self.assertFalse(rep.closed)
        self.assertEqual([s.format_hhmm() for s in rep.slots], ["09:00-10:00", "10:00-11:00", "11:00-12:00"])

    def test_free_slots_respects_breaks_bookings_and_buffer(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.buffer_minutes = 15
        rules.breaks["tue"] = (sa.TimeInterval(13 * 60, 14 * 60),)
        bookings = [
            sa.Booking("B1", date(2026, 4, 14), 10 * 60, 11 * 60, "Grace"),
            sa.Booking("B2", date(2026, 4, 14), 14 * 60, 15 * 60, "Henry"),
        ]
        rep = sa.generate_free_slots(bookings, rules, date(2026, 4, 14), duration_minutes=60)
        self.assertEqual([s.format_hhmm() for s in rep.slots], ["12:00-13:00", "16:00-17:00", "17:00-18:00"])

    def test_free_slots_custom_step_and_min_start(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.buffer_minutes = 15
        rules.working_hours["wed"] = (sa.TimeInterval(9 * 60, 13 * 60),)
        bookings = [sa.Booking("B1", date(2026, 4, 15), 10 * 60, 11 * 60, "Client")]
        rep = sa.generate_free_slots(
            bookings,
            rules,
            date(2026, 4, 15),
            duration_minutes=45,
            step_minutes=30,
            min_start_min=9 * 60 + 30,
        )
        # 10:00-11:00 + 15m buffer blocks [09:45, 11:15).
        # Starts >= 09:30 on 30m step: 09:30 (9:30-10:15 blocked), 10:00-11:00 blocked,
        # 11:30-12:15 FREE, 12:00-12:45 FREE.
        self.assertEqual([s.format_hhmm() for s in rep.slots], ["11:30-12:15", "12:00-12:45"])

    def test_free_slots_closed_day(self) -> None:
        rules = sa.ScheduleRules.default()
        rep = sa.generate_free_slots([], rules, date(2026, 4, 19), duration_minutes=60)
        self.assertTrue(rep.closed)
        self.assertEqual(rep.slots, ())

    def test_free_slots_split_shift_hours(self) -> None:
        rules = sa.ScheduleRules.default()
        rules.working_hours["thu"] = (
            sa.TimeInterval(9 * 60, 11 * 60),
            sa.TimeInterval(15 * 60, 17 * 60),
        )
        rep = sa.generate_free_slots([], rules, date(2026, 4, 16), duration_minutes=60)
        self.assertEqual(
            [s.format_hhmm() for s in rep.slots],
            ["09:00-10:00", "10:00-11:00", "15:00-16:00", "16:00-17:00"],
        )


class TestCLIAndOutputFormats(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = sa.main(argv)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_cli_audit_formats_text_json_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched_path = Path(tmp) / "sched.csv"
            sched_path.write_text(
                "id,date,start,end,client\n"
                "1,2026-04-13,09:00,10:00,Sam\n"
                "2,2026-04-13,09:30,10:30,Sam\n",
                encoding="utf-8",
            )
            code_txt, out_txt, _ = self._run_cli([str(sched_path), "--audit", "--format", "text"])
            self.assertEqual(code_txt, 0)
            self.assertIn("OVERLAP", out_txt)
            self.assertIn("DUPLICATE_CLIENT", out_txt)

            code_json, out_json, _ = self._run_cli([str(sched_path), "--audit", "--format", "json"])
            self.assertEqual(code_json, 0)
            parsed = json.loads(out_json)
            self.assertEqual(parsed["mode"], "audit")
            self.assertEqual(parsed["summary"]["error_count"], 1)
            self.assertEqual(parsed["summary"]["warning_count"], 1)

            code_csv, out_csv, _ = self._run_cli([str(sched_path), "--audit", "--format", "csv"])
            self.assertEqual(code_csv, 0)
            rows = list(csv.DictReader(io.StringIO(out_csv)))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["code"] for r in rows}, {"OVERLAP", "DUPLICATE_CLIENT"})

    def test_cli_free_slots_formats_text_json_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched_path = Path(tmp) / "sched.json"
            sched_path.write_text(
                json.dumps(
                    {
                        "working_hours": {"mon": "09:00-12:00"},
                        "buffer_minutes": 0,
                        "bookings": [
                            {"id": "1", "date": "2026-04-13", "start": "10:00", "end": "11:00", "client": "Sam"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            code_txt, out_txt, _ = self._run_cli(
                [str(sched_path), "--free-slots", "--date", "2026-04-13", "--duration", "60", "--format", "text"]
            )
            self.assertEqual(code_txt, 0)
            self.assertIn("09:00", out_txt)
            self.assertIn("11:00", out_txt)

            code_json, out_json, _ = self._run_cli(
                [str(sched_path), "--free-slots", "--date", "2026-04-13", "--duration", "60", "--format", "json"]
            )
            self.assertEqual(code_json, 0)
            payload = json.loads(out_json)
            self.assertEqual(payload["available_count"], 2)
            self.assertEqual([s["label"] for s in payload["slots"]], ["09:00-10:00", "11:00-12:00"])

            code_csv, out_csv, _ = self._run_cli(
                [str(sched_path), "--free-slots", "--date", "2026-04-13", "--duration", "60", "--format", "csv"]
            )
            self.assertEqual(code_csv, 0)
            rows = list(csv.DictReader(io.StringIO(out_csv)))
            self.assertEqual([r["label"] for r in rows], ["09:00-10:00", "11:00-12:00"])

    def test_cli_combined_audit_and_free_slots(self) -> None:
        sample = ROOT_DIR / "sample_schedule.json"
        code, out_json, _ = self._run_cli(
            [str(sample), "--audit", "--free-slots", "--date", "2026-04-14", "--format", "json"]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out_json)
        self.assertEqual(payload["mode"], "audit_and_free_slots")
        self.assertTrue(payload["audit"]["summary"]["passed"])
        self.assertEqual(payload["free_slots"]["available_count"], 3)

        code_csv, _, err_csv = self._run_cli(
            [str(sample), "--audit", "--free-slots", "--date", "2026-04-14", "--format", "csv"]
        )
        self.assertEqual(code_csv, 2)
        self.assertIn("supports either", err_csv)

    def test_cli_fail_on_policies_and_rules_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sched_path = Path(tmp) / "sched.csv"
            sched_path.write_text(
                "id,date,start,end,client\n"
                "1,2026-04-13,09:00,10:00,Sam\n"
                "2,2026-04-13,10:05,11:00,Max\n",
                encoding="utf-8",
            )
            rules_path = Path(tmp) / "rules.json"
            rules_path.write_text(json.dumps({"buffer_minutes": 15}), encoding="utf-8")

            # Only WARN (BUFFER_VIOLATION): fail-on error -> 0, fail-on warn -> 1
            c_err, _, _ = self._run_cli(
                [str(sched_path), "--rules", str(rules_path), "--audit", "--fail-on", "error"]
            )
            self.assertEqual(c_err, 0)
            c_warn, _, _ = self._run_cli(
                [str(sched_path), "--rules", str(rules_path), "--audit", "--fail-on", "warn"]
            )
            self.assertEqual(c_warn, 1)

    def test_cli_error_exit_codes(self) -> None:
        c1, _, _ = self._run_cli(["nonexistent_file.json"])
        self.assertEqual(c1, 2)

        sample = ROOT_DIR / "sample_schedule.json"
        c2, _, _ = self._run_cli([str(sample), "--free-slots", "--date", "not-a-date"])
        self.assertEqual(c2, 2)

        c3, _, _ = self._run_cli([str(sample), "--free-slots", "--date", "2026-04-14", "--duration", "0"])
        self.assertEqual(c3, 2)

    def test_sample_schedule_end_to_end(self) -> None:
        sample = ROOT_DIR / "sample_schedule.json"
        code, out_json, err = self._run_cli([str(sample), "--audit", "--format", "json", "--fail-on", "error"])
        self.assertEqual(err, "")
        self.assertEqual(code, 1)
        payload = json.loads(out_json)
        self.assertEqual(payload["summary"]["total_bookings"], 10)
        self.assertEqual(payload["summary"]["active_bookings"], 9)
        self.assertEqual(payload["summary"]["cancelled_bookings"], 1)
        self.assertEqual(
            payload["summary"]["counts_by_code"],
            {
                "DUPLICATE_CLIENT": 1,
                "OVERLAP": 1,
                "BUFFER_VIOLATION": 1,
                "BREAK_CONFLICT": 1,
                "OFF_HOURS": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()
