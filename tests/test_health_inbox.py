"""Автоматический импорт из входящей папки, период файла и защита от потери данных при пропуске номеров,
сводки и состояние синхронизации для MCP и панели.

Все базы и папки временные, все данные синтетические. Рабочая база проекта не используется.
"""

import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

from fitness import db, health_format, health_import, health_inbox, health_status, tools
from fitness.errors import ImportFileError, ImportOrderError, TrackerError
from fitness.web import app as web_app
from tests import health_samples as hs

OLD = time.time() - 3600          # файлы «давно скопированы»


def period(doc, start, end):
    doc["period_start"], doc["period_end"] = start, end
    return doc


def day(sequence, day_utc, phone="phone-01", with_period=True, **kw):
    """Файл одного дня: шаги в 00:00–00:10 и пульс в 19:10–19:11 (UTC) дня day_utc, экспорт в 23:00 того же дня."""
    doc = hs.export(sequence,
                    [hs.step(f"step-{day_utc}", f"{day_utc}T00:00:00Z", f"{day_utc}T00:10:00Z", 100 + sequence,
                             modified=f"{day_utc}T22:00:00Z")],
                    [hs.heart(f"hr-{day_utc}", f"{day_utc}T19:10:00Z", f"{day_utc}T19:11:00Z", (60 + sequence,),
                              modified=f"{day_utc}T22:00:00Z")],
                    phone=phone, exported_at=f"{day_utc}T23:00:00Z", **kw)
    if with_period:
        period(doc, f"{day_utc}T00:00:00Z", f"{day_utc}T23:00:00Z")
    return doc


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-inbox-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "data" / "test.db"
        self.assertNotEqual(self.db_path.resolve(), db.DEFAULT_DB_PATH.resolve())
        db.open_database(self.db_path).close()
        self.dirs = health_inbox.folders(self.db_path)
        quiet = mock.patch("sys.stderr", io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)

    def put(self, name, doc, mtime=OLD):
        path = hs.write(self.dirs["inbox"], name, doc)
        os.utime(path, (mtime, mtime))
        return path

    def run_inbox(self, **kw):
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("stable_wait", 0)
        return health_inbox.process_inbox(self.db_path, **kw)

    def outcomes(self, run):
        return {i["file"]: i["outcome"] for i in run["files"]}

    def batches(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT sequence, status FROM import_batches ORDER BY id").fetchall()
        finally:
            conn.close()

    def count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


class InboxBasicsTest(InboxTestCase):
    def test_no_files(self):
        run = self.run_inbox()
        self.assertEqual(run["files"], [])
        self.assertIsNone(run["error"])
        self.assertEqual(health_inbox.exit_code(run), 0)
        self.assertTrue(self.dirs["inbox"].is_dir())
        self.assertTrue((self.dirs["state"] / health_inbox.CHECK_FILE).is_file())
        self.assertFalse((self.dirs["state"] / health_inbox.LAST_RUN_FILE).exists())   # пустой запуск не пишет отчёт
        self.assertIn("Новых файлов нет", "\n".join(health_inbox.describe_run(run)))

    def test_valid_file_imported_and_moved(self):
        self.put("a.json", hs.day_file(1))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})
        self.assertEqual(run["files"][0]["totals"]["inserted"], 7)
        self.assertFalse((self.dirs["inbox"] / "a.json").exists())
        self.assertTrue((self.dirs["processed"] / "a.json").is_file())
        self.assertEqual(self.batches(), [(1, "ok")])
        saved = json.loads((self.dirs["state"] / health_inbox.LAST_RUN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["summary"]["imported"], 1)
        log_lines = (self.dirs["state"] / health_inbox.RUNS_LOG).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log_lines), 1)

    def test_rerun_and_repeated_file_are_idempotent(self):
        self.put("a.json", hs.day_file(1))
        self.run_inbox()
        self.assertEqual(self.outcomes(self.run_inbox()), {})                 # повторный запуск: нечего делать
        self.put("a.json", hs.day_file(1))                                      # тот же файл прислали ещё раз
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "already_imported"})
        self.assertEqual(self.batches(), [(1, "ok")])
        self.assertEqual(self.count("step_intervals"), 3)
        moved = sorted(p.name for p in self.dirs["processed"].iterdir())
        self.assertEqual(len(moved), 2)                                         # копия не затёрла первый файл
        self.assertIn("a.json", moved)

    def test_overlapping_files_do_not_duplicate(self):
        first = hs.export(1, [hs.step("s-1", "2026-10-10T00:00:00Z", "2026-10-10T00:10:00Z", 10),
                              hs.step("s-2", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", 20)])
        second = hs.export(2, [hs.step("s-2", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", 20),
                               hs.step("s-3", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z", 30)])
        self.put("f1.json", first)
        self.put("f2.json", second)
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"f1.json": "imported", "f2.json": "imported"})
        self.assertEqual(self.count("step_intervals"), 3)
        self.assertEqual(run["files"][1]["totals"]["unchanged"], 1)

    def test_files_processed_in_sequence_order_regardless_of_name(self):
        self.put("z-first.json", hs.day_file(1))
        self.put("a-second.json", hs.day_file(2, steps=[hs.step("other", "2026-10-10T05:00:00Z", "2026-10-10T05:10:00Z", 5)]))
        run = self.run_inbox()
        self.assertEqual([i["file"] for i in run["files"]], ["z-first.json", "a-second.json"])
        self.assertEqual(self.batches(), [(1, "ok"), (2, "ok")])

    def test_dry_run_changes_nothing(self):
        self.put("a.json", hs.day_file(1))
        before = self.db_path.read_bytes()
        run = self.run_inbox(dry_run=True)
        self.assertEqual(self.outcomes(run), {"a.json": "dry_run"})
        self.assertTrue((self.dirs["inbox"] / "a.json").exists())
        self.assertEqual(self.batches(), [])
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertFalse((self.dirs["state"] / health_inbox.LAST_RUN_FILE).exists())

    def test_temp_and_hidden_files_ignored(self):
        for name in (".syncthing.a.json.tmp", "b.json.part", "~c.json", "d.txt", ".e.json"):
            hs.write(self.dirs["inbox"], name, b"{")
        (self.dirs["inbox"] / "sub").mkdir()
        hs.write(self.dirs["inbox"] / "sub", "f.json", hs.day_file(1))
        self.assertEqual(self.run_inbox()["files"], [])


class InboxErrorsTest(InboxTestCase):
    def test_corrupt_json_goes_to_failed_with_reason(self):
        self.put("bad.json", b'{"format": "fitness-health-export", ')
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"bad.json": "failed"})
        self.assertTrue((self.dirs["failed"] / "bad.json").is_file())
        note = (self.dirs["failed"] / "bad.json.error.txt").read_text(encoding="utf-8")
        self.assertIn("JSON", note)
        self.assertEqual(health_inbox.exit_code(run), 1)

    def test_unsupported_version_rejected(self):
        self.put("v2.json", hs.day_file(1, format_version=2))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"v2.json": "failed"})
        self.assertIn("версия формата", run["files"][0]["error"])
        self.assertEqual(self.batches(), [])                                     # заголовок неверен: в базе ничего

    def test_one_bad_file_does_not_block_others(self):
        self.put("1.json", hs.day_file(1))
        self.put("broken.json", b"not json at all")
        self.put("other-phone.json", hs.day_file(1, phone="phone-02"))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"1.json": "imported", "other-phone.json": "imported",
                                              "broken.json": "failed"})

    def test_fresh_incomplete_file_is_waited_for_not_failed(self):
        self.put("copying.json", b'{"format": "fitness-health-export", "format_version"', mtime=time.time() - 20)
        run = self.run_inbox(min_age=5)
        self.assertEqual(self.outcomes(run), {"copying.json": "busy"})
        self.assertTrue((self.dirs["inbox"] / "copying.json").exists())
        self.assertFalse(any(self.dirs["failed"].iterdir()))

    def test_recently_modified_file_is_busy(self):
        self.put("new.json", hs.day_file(1), mtime=time.time())
        run = self.run_inbox(min_age=30)
        self.assertEqual(self.outcomes(run), {"new.json": "busy"})
        self.assertEqual(self.batches(), [])

    def test_file_growing_between_checks_is_busy(self):
        path = self.put("grow.json", hs.day_file(1))

        def grow(_seconds):
            with open(path, "ab") as fh:
                fh.write(b" ")
            os.utime(path, (OLD, OLD))
        run = self.run_inbox(sleep=grow, stable_wait=1)
        self.assertEqual(self.outcomes(run), {"grow.json": "busy"})

    def test_recovery_after_crash_between_import_and_move(self):
        self.put("a.json", hs.day_file(1))
        with mock.patch.object(health_inbox, "_move", side_effect=OSError("файл занят")):
            run = self.run_inbox()                                                # перенос не удался: не падаем
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})
        self.assertIn("не перенесён", run["files"][0]["warnings"][-1])
        self.assertNotIn("moved_to", run["files"][0])
        self.assertTrue((self.dirs["inbox"] / "a.json").exists())               # файл не потерян
        self.assertEqual(self.batches(), [(1, "ok")])                             # импорт завершён
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "already_imported"})
        self.assertTrue((self.dirs["processed"] / "a.json").exists())

    def test_failed_file_that_cannot_be_moved_stays_in_inbox(self):
        self.put("bad.json", b"{")
        with mock.patch.object(health_inbox, "_move", side_effect=OSError("файл занят")):
            run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"bad.json": "failed"})
        self.assertIn("остался во входящей папке", run["files"][0]["error"])
        self.assertTrue((self.dirs["inbox"] / "bad.json").exists())

    def test_watch_survives_unexpected_error(self):
        lines = []
        calls = []

        def flaky(*args, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("папка недоступна")
            return real(*args, **kw)

        real = health_inbox.process_inbox
        self.put("a.json", hs.day_file(1))
        with mock.patch.object(health_inbox, "process_inbox", side_effect=flaky):
            runs = health_inbox.watch(self.db_path, 5, out=lines.append, sleep=lambda s: None, max_runs=2)
        self.assertEqual(runs, 2)
        self.assertTrue(any("папка недоступна" in line for line in lines))
        self.assertTrue((self.dirs["processed"] / "a.json").exists())           # вторая проверка всё сделала

    def test_database_problem_stops_run_and_keeps_files(self):
        self.put("a.json", hs.day_file(1))
        self.put("b.json", hs.day_file(1, phone="phone-02"))
        with mock.patch.object(health_import, "import_file",
                               side_effect=TrackerError("Ошибка при импорте, ничего не сохранено: database is locked")):
            run = self.run_inbox()
        self.assertEqual(run["files"], [])
        self.assertIn("locked", run["error"]["message"])
        self.assertEqual(sorted(p.name for p in self.dirs["inbox"].iterdir()), ["a.json", "b.json"])
        self.assertEqual(health_inbox.exit_code(run), 1)

    def test_missing_database_stops_run(self):
        self.put("a.json", hs.day_file(1))
        self.db_path.unlink()
        run = self.run_inbox()
        self.assertEqual(run["error"]["code"], "database_missing")
        self.assertTrue((self.dirs["inbox"] / "a.json").exists())
        self.assertFalse(self.db_path.exists())                                  # база не создана

    def test_unexpected_error_keeps_file_for_diagnosis(self):
        self.put("a.json", hs.day_file(1))
        with mock.patch.object(health_import, "import_file", side_effect=RuntimeError("сбой")):
            run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "failed"})
        self.assertTrue((self.dirs["failed"] / "a.json").exists())

    def test_retry_failed_returns_files_to_inbox(self):
        self.put("bad.json", b"{")
        self.run_inbox()
        moved = health_inbox.retry_failed(self.db_path)
        self.assertEqual(moved, ["bad.json"])
        self.assertTrue((self.dirs["inbox"] / "bad.json").exists())
        self.assertTrue((self.dirs["failed"] / "bad.json.error.txt").exists())   # заметка осталась

    def test_second_processor_is_refused(self):
        with health_inbox._Lock(self.dirs["state"] / health_inbox.LOCK_FILE):
            run = self.run_inbox()
        self.assertEqual(run["error"]["code"], "inbox_locked")
        self.assertEqual(health_inbox.exit_code(run), 3)
        self.assertIsNone(self.run_inbox()["error"])                             # после освобождения работает


class GapProtectionTest(InboxTestCase):
    """Номер растёт на телефоне при каждом созданном файле, даже если файл не дошёл до компьютера."""

    def test_gap_covered_by_period_is_accepted(self):
        self.put("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        # Файлы №2 и №3 не дошли; №4 читает с 01.10 (нахлёст), данные в базе есть до 01.10 19:11 — ничего не потеряно.
        doc = day(4, "2026-10-03")
        period(doc, "2026-10-01T00:00:00Z", "2026-10-03T23:00:00Z")
        self.put("4.json", doc)
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"4.json": "imported"})
        gap = run["files"][0]["gap"]
        self.assertEqual((gap["accepted"], gap["missing_from"], gap["missing_to"]), ("covered", 2, 3))
        self.assertTrue(any("перекрыт" in w for w in run["files"][0]["warnings"]))

    def test_uncovered_gap_is_held_and_explained(self):
        self.put("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.put("3.json", day(3, "2026-10-05"))           # период с 05.10, а данные в базе до 01.10
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"3.json": "held"})
        item = run["files"][0]
        self.assertEqual((item["order_problem"], item["missing_from"], item["missing_to"]), ("gap", 2, 2))
        self.assertIn("потеряется", item["error"])
        self.assertTrue((self.dirs["inbox"] / "3.json").exists())               # ждёт, не в failed
        self.assertEqual(health_inbox.exit_code(run), 2)
        status = health_inbox.inbox_status(self.db_path)
        self.assertEqual(status["waiting"][0]["outcome"], "held")
        # Пропущенный файл нашёлся: следующий запуск применяет оба по порядку.
        self.put("2.json", day(2, "2026-10-03"))
        run = self.run_inbox()
        self.assertEqual([(i["file"], i["outcome"]) for i in run["files"]], [("2.json", "imported"), ("3.json", "imported")])

    def test_old_file_without_period_keeps_strict_rule(self):
        self.put("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.put("3.json", day(3, "2026-10-01", with_period=False))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"3.json": "held"})
        self.assertIn("не указан период", run["files"][0]["error"])
        run = self.run_inbox(allow_gaps=True)                                     # явное согласие пользователя
        self.assertEqual(self.outcomes(run), {"3.json": "imported"})

    def test_late_older_file_waits_for_explicit_permission(self):
        self.put("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        doc = day(3, "2026-10-03")
        period(doc, "2026-09-30T00:00:00Z", "2026-10-03T23:00:00Z")
        self.put("3.json", doc)
        self.run_inbox()
        self.put("2.json", day(2, "2026-10-02"))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"2.json": "held"})
        self.assertEqual(run["files"][0]["order_problem"], "older")
        run = self.run_inbox(allow_older=True)
        self.assertEqual(self.outcomes(run), {"2.json": "imported"})

    def test_reused_number_with_other_content_fails(self):
        self.put("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.put("1b.json", day(1, "2026-10-02"))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"1b.json": "failed"})
        self.assertEqual(run["files"][0]["order_problem"], "taken")

    def test_continuity_warning_for_next_file_with_later_start(self):
        self.put("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.put("2.json", day(2, "2026-10-05"))           # следующий номер, но произвольный период позже
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"2.json": "imported"})
        self.assertTrue(any("не выгружался" in w for w in run["files"][0]["warnings"]))


class PeriodFormatTest(unittest.TestCase):
    def parse(self, doc):
        return health_format.parse_export(hs.encode(doc))

    def test_period_is_optional_and_canonical(self):
        self.assertIsNone(self.parse(hs.day_file(1)).header["period_start"])
        parsed = self.parse(period(hs.day_file(1), "2026-10-09T21:00:00Z", "2026-10-10T17:20:00.5Z"))
        self.assertEqual(parsed.header["period_start"], "2026-10-09T21:00:00.000000000Z")
        self.assertEqual(parsed.header["period_end"], "2026-10-10T17:20:00.500000000Z")

    def test_invalid_periods_rejected(self):
        cases = {
            "only start": {"period_start": "2026-10-09T21:00:00Z"},
            "end before start": {"period_start": "2026-10-10T00:00:00Z", "period_end": "2026-10-09T00:00:00Z"},
            "bad time": {"period_start": "2026-10-09 21:00", "period_end": "2026-10-10T00:00:00Z"},
            "future end": {"period_start": "2026-10-09T00:00:00Z", "period_end": "2026-10-20T00:00:00Z"},
        }
        for label, extra in cases.items():
            with self.subTest(label), self.assertRaises(ImportFileError):
                self.parse(dict(hs.day_file(1), **extra))

    def test_import_report_contains_period(self):
        tmp = Path(tempfile.mkdtemp(prefix="fitness-period-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db_path = tmp / "t.db"
        db.open_database(db_path).close()
        doc = period(hs.day_file(1), "2026-10-09T21:00:00Z", "2026-10-10T17:20:00Z")
        report = health_import.import_bytes(hs.encode(doc), "x.json", db_path)
        self.assertEqual(report["period"]["start_utc"], "2026-10-09T21:00:00.000000000Z")
        self.assertIsNone(report["gap"])

    def test_cli_gap_error_carries_details(self):
        tmp = Path(tempfile.mkdtemp(prefix="fitness-period-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db_path = tmp / "t.db"
        db.open_database(db_path).close()
        health_import.import_bytes(hs.encode(day(1, "2026-10-01")), "1.json", db_path)
        with self.assertRaises(ImportOrderError) as ctx:
            health_import.import_bytes(hs.encode(day(5, "2026-10-08")), "5.json", db_path)
        data = ctx.exception.to_dict()
        self.assertEqual((data["order_problem"], data["missing_from"], data["missing_to"]), ("gap", 2, 4))
        self.assertEqual(data["covered_until"], "2026-10-01T19:11:00.000000000Z")
        # dry_run показывает то же решение и не пишет журнал
        with self.assertRaises(ImportOrderError):
            health_import.import_bytes(hs.encode(day(5, "2026-10-08")), "5.json", db_path, dry_run=True)


class SummaryAndStatusTest(InboxTestCase):
    def load(self):
        # Шаги 21:30–21:40 UTC 9 октября при UTC+3 — это 00:30 10 октября по местному времени.
        doc = hs.export(1, [hs.step("late", "2026-10-09T21:30:00Z", "2026-10-09T21:40:00Z", 700),
                            hs.step("early", "2026-10-09T20:30:00Z", "2026-10-09T20:40:00Z", 300)],
                        [hs.heart("hr-a", "2026-10-10T06:00:00Z", "2026-10-10T06:01:00Z", (50,)),
                         hs.heart("hr-b", "2026-10-10T07:00:00Z", "2026-10-10T07:01:00Z", (90,))],
                        [hs.sleep("night", "2026-10-09T20:00:00Z", "2026-10-10T04:00:00Z", stages=[
                            {"stage": "deep", "start": "2026-10-09T20:00:00Z", "end": "2026-10-09T22:00:00Z"},
                            {"stage": "light", "start": "2026-10-09T22:00:00Z", "end": "2026-10-10T04:00:00Z"}])])
        self.put("a.json", doc)
        self.run_inbox()

    def test_local_day_boundaries(self):
        self.load()
        s = health_status.health_summary(self.db_path, "2026-10-09", "2026-10-10", compare=False)
        steps = {d["date"]: d["steps"] for d in s["days"]["steps"]}
        self.assertEqual(steps, {"2026-10-09": 300, "2026-10-10": 700})
        hr = s["summary"]["heart_rate"]
        self.assertEqual((hr["min_bpm"], hr["max_bpm"], hr["avg_bpm"], hr["samples"]), (50, 90, 70.0, 2))
        sleep = s["days"]["sleep"]
        self.assertEqual([d["date"] for d in sleep], ["2026-10-10"])            # дата пробуждения
        self.assertEqual((sleep[0]["in_bed_min"], sleep[0]["deep_min"], sleep[0]["light_min"]), (480.0, 120.0, 360.0))

    def test_days_mode_and_comparison(self):
        self.load()
        s = health_status.health_summary(self.db_path, days=1, today=date(2026, 10, 10))
        self.assertEqual((s["period"]["date_from"], s["period"]["date_to"]), ("2026-10-10", "2026-10-10"))
        self.assertEqual(s["summary"]["steps"]["total"], 700)
        self.assertEqual(s["previous_period"]["summary"]["steps"]["total"], 300)
        self.assertEqual(s["change"]["steps_avg_per_day"], 400)
        s = health_status.health_summary(self.db_path, days=3, today=date(2026, 10, 14))
        self.assertTrue(any("только до 2026-10-10" in n for n in s["notes"]))

    def test_period_validation(self):
        for kwargs in ({}, {"days": 0}, {"days": 3, "date_from": "2026-10-01"}, {"date_from": "2026-10-05", "date_to": "2026-10-01"},
                       {"date_from": "10.10.2026", "date_to": "2026-10-11"}):
            with self.subTest(kwargs), self.assertRaises(TrackerError):
                health_status.health_summary(self.db_path, **kwargs)

    def test_empty_database_summary_and_status(self):
        s = health_status.health_summary(self.db_path, days=7)
        self.assertEqual(s["summary"]["steps"]["total"], 0)
        self.assertIsNone(s["summary"]["heart_rate"]["avg_bpm"])
        status = health_status.sync_status(self.db_path)
        self.assertTrue(status["database"]["ok"])
        self.assertIsNone(status["last_successful_import"])
        self.assertIn("Ни одного успешного импорта ещё не было", status["attention"])

    def test_status_reports_queue_errors_and_freshness(self):
        self.load()
        self.put("bad.json", b"{")
        self.run_inbox()
        self.put("waiting.json", hs.day_file(5), mtime=time.time())
        status = health_status.sync_status(self.db_path)
        self.assertEqual(status["imported_files"], 1)
        self.assertEqual(status["last_successful_import"]["sequence"], 1)
        self.assertEqual(status["data"]["steps"]["last_local_date"], "2026-10-10")
        self.assertEqual(status["inbox"]["failed_files"], 1)
        self.assertEqual(status["inbox"]["waiting_files"], 1)
        self.assertTrue(any("health_failed" in n for n in status["attention"]))
        self.assertTrue(any("Необработанных" in n for n in status["attention"]))
        text = "\n".join(health_status.describe(status))
        self.assertIn("bad.json", text)

    def test_status_never_writes(self):
        self.load()

        def files():   # служебные -wal/-shm создаёт сам SQLite при чтении базы в режиме WAL, содержимое базы не меняется
            return sorted((p.relative_to(self.tmp), p.read_bytes() if p.is_file() else None)
                          for p in self.tmp.rglob("*") if not p.name.endswith(("-wal", "-shm")))
        before = files()
        health_status.sync_status(self.db_path)
        health_status.health_summary(self.db_path, days=30)
        self.assertEqual(files(), before)

    def test_missing_database_status(self):
        status = health_status.sync_status(self.tmp / "nope" / "x.db")
        self.assertFalse(status["database"]["ok"])
        self.assertFalse((self.tmp / "nope").exists())


class ToolsAndDashboardTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"FITNESS_DB_PATH": str(self.db_path)})
        env.start()
        self.addCleanup(env.stop)

    def call(self, name, args=None):
        result = tools.call_tool(name, args or {})
        return result["isError"], result["structuredContent"]

    def test_tools_registered_with_annotations(self):
        by_name = {t["name"]: t for t in tools.list_tools()}
        for name in ("get_health_summary", "get_health_sync_status"):
            self.assertTrue(by_name[name]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["import_health_inbox"]["annotations"]["readOnlyHint"])

    def test_inbox_tool_summary_and_status(self):
        self.put("a.json", hs.day_file(1))
        error, body = self.call("import_health_inbox", {"dry_run": True})
        self.assertFalse(error)
        self.assertEqual(body["run"]["summary"]["dry_run"], 1)
        error, body = self.call("import_health_inbox")
        self.assertFalse(error, body)
        self.assertEqual(body["run"]["summary"]["imported"], 1)
        error, body = self.call("get_health_summary", {"date_from": "2026-10-10", "date_to": "2026-10-10"})
        self.assertFalse(error, body)
        self.assertEqual(body["summary"]["steps"]["total"], 511 + 876 + 6)
        error, body = self.call("get_health_sync_status")
        self.assertFalse(error, body)
        self.assertEqual(body["imported_files"], 1)
        self.assertEqual(body["inbox"]["processed_files"], 1)

    def test_tool_validation_errors(self):
        error, body = self.call("get_health_summary", {"days": 3, "date_from": "2026-10-01"})
        self.assertTrue(error)
        self.assertEqual(body["error"], "validation_error")
        error, body = self.call("get_health_summary", {"sql": "x"})
        self.assertTrue(error)
        error, body = self.call("import_health_inbox", {"path": "C:/x"})
        self.assertTrue(error)

    def test_import_tool_reports_gap_details(self):
        self.call("import_health_inbox")
        self.put("1.json", day(1, "2026-10-01"))
        self.call("import_health_inbox")
        self.put("3.json", day(3, "2026-10-05"))
        error, body = self.call("import_health_inbox")
        self.assertFalse(error)
        self.assertEqual(body["run"]["files"][0]["outcome"], "held")

    def test_dashboard_sync_page(self):
        self.put("bad.json", b"{")
        self.put("1.json", hs.day_file(1))
        self.run_inbox()
        server = web_app.make_server(port=0, db_path=self.db_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        html = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/sync").read().decode("utf-8")
        self.assertIn("Требует внимания", html)
        self.assertIn("bad.json", html)
        self.assertIn("Последние данные в базе", html)
        self.assertIn('href="/sync"', html)

    def test_dashboard_sync_page_without_database(self):
        server = web_app.make_server(port=0, db_path=self.tmp / "missing" / "x.db")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        html = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/sync").read().decode("utf-8")
        self.assertIn("База недоступна", html)


if __name__ == "__main__":
    unittest.main()
