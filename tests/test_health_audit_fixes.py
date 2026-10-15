"""Регрессионные тесты по результатам независимого аудита импортёра (дефекты Д1–Д7 и дополнительные риски).

Каждый класс соответствует пункту аудита. Базы временные, файлы синтетические.
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import import_health
from fitness import db, health_format, health_import
from fitness.errors import ImportFileError, ImportOrderError, TrackerError
from tests import health_helpers as h
from tests import health_samples as hs
from tests import test_health_import as ti

DATA_TABLES = ti.DATA_TABLES
FIVE_SAMPLES = [{"time": f"2026-10-10T19:10:{10 * i:02d}Z", "bpm": 60 + i} for i in range(5)]


class FixTestCase(ti.ImportTestCase):
    def assertIntegrity(self, path=None):
        conn = db.connect(path or self.db_path, create=False)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def put_ascii(self, doc, name=None):
        """Файл с экранированными символами (\\ud800, \\u0000): корректный JSON, который json.dumps(ensure_ascii=False) не даст."""
        self.counter += 1
        return hs.write(self.incoming, name or f"ascii-{self.counter:03d}.json", json.dumps(doc).encode("ascii"))

    def dir_state(self, path=None):
        """Состояние базы и связанных файлов: имя, размер и SHA-256 (для -shm только размер: это служебный
        индекс SQLite, его байты меняются при любом чтении)."""
        path = Path(path or self.db_path)
        state = {}
        for f in sorted(path.parent.glob(path.name + "*")):
            digest = None if f.name.endswith("-shm") else hashlib.sha256(f.read_bytes()).hexdigest()
            state[f.name] = (f.stat().st_size, digest)
        return state

    def journal_mode(self, path=None):
        conn = h.raw_readonly(path or self.db_path)
        try:
            return conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()


# ======================= Д1: удаления в области телефона и источника =======================

class DeletionScopeTests(FixTestCase):
    def two_phones_with_same_id(self):
        self.run_import(hs.export(1, phone="phone-A", steps=[hs.step("X", count=100, src="s1")], sources=[hs.source("s1", "app.one")]))
        self.run_import(hs.export(1, phone="phone-B", steps=[hs.step("X", count=200, src="s2")], sources=[hs.source("s2", "app.two")]))
        self.assertEqual(self.count("step_intervals"), 2)

    def live(self):
        return {r["pkg"]: r["live"] for r in self.rows(
            "SELECT s.package_name AS pkg, t.deleted_at IS NULL AS live FROM step_intervals t "
            "JOIN data_sources s ON s.id = t.source_id")}

    def test_one_phone_cannot_delete_the_record_of_another_with_the_same_id(self):
        self.two_phones_with_same_id()
        report = self.run_import(hs.export(2, phone="phone-A", deleted=["X"], sources=[hs.source("s1", "app.one")]))
        self.assertEqual(report["counts"]["steps"]["deleted"], 1)
        self.assertEqual(self.live(), {"app.one": 0, "app.two": 1})                 # удалена только запись телефона A
        report = self.run_import(hs.export(2, phone="phone-B", deleted=["X"], sources=[hs.source("s2", "app.two")]))
        self.assertEqual(report["counts"]["steps"]["deleted"], 1)
        self.assertEqual(self.live(), {"app.one": 0, "app.two": 0})
        self.assertIntegrity()

    def test_a_stranger_phone_and_source_deletes_nothing_and_is_reported(self):
        self.run_import(hs.export(1, phone="phone-A", steps=[hs.step("X", src="s1")], sources=[hs.source("s1", "app.one")]))
        before = self.dump(DATA_TABLES)
        report = self.run_import(hs.export(1, phone="phone-C", deleted=["X"], sources=[hs.source("s3", "app.three")]))
        self.assertEqual(report["counts"]["steps"]["deleted"], 0)
        self.assertEqual((report["out_of_scope_deletions"], report["out_of_scope_ids"], report["not_applied"]["deletion_out_of_scope"]),
                         (1, ["X"], 1))
        self.assertTrue(any("другому телефону" in w for w in report["warnings"]))
        self.assertEqual(self.dump(DATA_TABLES), before)

    def test_deletion_without_declared_sources_works_only_for_the_phone_that_exported_the_record(self):
        self.run_import(hs.export(1, phone="phone-A", steps=[hs.step("X", src="s1")], sources=[hs.source("s1", "app.one")]))
        other = self.run_import(hs.export(1, phone="phone-B", deleted=["X"], sources=[]))
        self.assertEqual((other["counts"]["steps"]["deleted"], other["out_of_scope_deletions"]), (0, 1))
        own = self.run_import(hs.export(2, phone="phone-A", deleted=["X"], sources=[]))
        self.assertEqual(own["counts"]["steps"]["deleted"], 1)

    def test_a_reinstalled_app_with_the_same_source_can_still_delete(self):
        self.run_import(hs.export(1, phone="phone-A", steps=[hs.step("X", src="s1")], sources=[hs.source("s1", "app.one")]))
        report = self.run_import(hs.export(1, phone="phone-A-new-install", deleted=["X"], sources=[hs.source("s1", "app.one")]))
        self.assertEqual(report["counts"]["steps"]["deleted"], 1)

    def test_an_id_that_matches_several_records_of_the_same_phone_is_ambiguous_and_rejects_the_file(self):
        self.run_import(hs.export(1, steps=[hs.step("Z", src="s1")], sources=[hs.source("s1", "app.one")]))
        self.run_import(hs.export(2, steps=[hs.step("Z", src="s2")], sources=[hs.source("s2", "app.two")]))
        before = self.dump(DATA_TABLES)
        with self.assertRaises(ImportFileError) as ctx:
            self.run_import(hs.export(3, deleted=["Z"], sources=[hs.source("s1", "app.one"), hs.source("s2", "app.two")]))
        self.assertIn("неоднозначно", str(ctx.exception))
        self.assertEqual(self.dump(DATA_TABLES), before)                                # ни одна запись не удалена
        self.assertEqual(self.rows("SELECT status FROM import_batches ORDER BY id")[-1]["status"], "failed")

    def test_a_declared_source_picks_one_of_two_records_with_the_same_id_of_the_same_phone(self):
        self.run_import(hs.export(1, steps=[hs.step("Z", src="s1", count=1)], sources=[hs.source("s1", "app.one")]))
        self.run_import(hs.export(2, steps=[hs.step("Z", src="s2", count=2)], sources=[hs.source("s2", "app.two")]))
        report = self.run_import(hs.export(3, deleted=["Z"], sources=[hs.source("s1", "app.one")]))
        self.assertEqual(report["counts"]["steps"]["deleted"], 1)
        self.assertEqual(self.live(), {"app.one": 0, "app.two": 1})
        # другой источник, не подходящий ни к одной записи: остаётся неоднозначность по телефону
        with self.assertRaises(ImportFileError):
            self.run_import(hs.export(4, deleted=["Z"], sources=[hs.source("s3", "app.three")]))

    def test_the_same_id_in_other_phones_and_sources_is_not_ambiguous(self):
        self.two_phones_with_same_id()
        report = self.run_import(hs.export(2, phone="phone-A", deleted=["X"], sources=[hs.source("s1", "app.one")]))
        self.assertEqual(report["counts"]["steps"]["deleted"], 1)                      # у B такой же id, но он не в области A

    def test_a_deletion_from_an_older_file_does_not_remove_a_record_updated_by_a_newer_export(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=100)]))
        self.run_import(hs.export(3, steps=[hs.step("X", count=150, modified="2026-10-10T18:00:00Z")]), allow_gaps=True)
        report = self.run_import(hs.export(2, deleted=["X"]), allow_older=True)         # файл №2 пропущен и найден позже
        self.assertEqual((report["counts"]["steps"]["deleted"], report["not_applied"]["deletion_stale"]), (0, 1))
        self.assertEqual(self.rows("SELECT count, deleted_at FROM step_intervals")[0], {"count": 150, "deleted_at": None})


# ======================= Д2: удалённые записи и устаревший экспорт =======================

class ResurrectionTests(FixTestCase):
    LATE = "2026-10-10T18:00:00Z"            # экспорт заведомо позже удаления

    def deleted_x(self, phone="phone-A"):
        self.run_import(hs.export(1, phone=phone, steps=[hs.step("X", count=100, modified="2026-10-10T17:00:00Z")]))
        self.run_import(hs.export(2, phone=phone, deleted=["X"]))

    def row(self):
        return self.rows("SELECT count, deleted_at, revision, hc_last_modified FROM step_intervals")[0]

    def test_new_phone_with_an_older_version_cannot_resurrect_or_lower_hc_last_modified(self):
        self.deleted_x()
        report = self.run_import(hs.export(1, phone="phone-B", exported_at=self.LATE,
                                           steps=[hs.step("X", count=90, modified="2026-10-10T10:00:00Z")]))
        self.assertEqual((report["status"], report["counts"]["steps"]["updated"], report["not_applied"]["replay_of_deleted"]),
                         ("imported", 0, 1))
        row = self.row()
        self.assertEqual((row["count"], row["revision"], row["hc_last_modified"]), (100, 1, "2026-10-10T17:00:00.000000000Z"))
        self.assertIsNotNone(row["deleted_at"])

    def test_new_phone_replaying_identical_data_exported_before_the_deletion_is_ignored(self):
        self.deleted_x()
        report = self.run_import(hs.export(1, phone="phone-B",           # экспорт №1 = 17:20, удаление принято в экспорте 17:21
                                           steps=[hs.step("X", count=100, modified="2026-10-10T17:00:00Z")]))
        self.assertEqual(report["not_applied"]["replay_of_deleted"], 1)
        self.assertIsNotNone(self.row()["deleted_at"])

    def test_the_first_file_of_a_new_phone_keeps_its_arbitrary_sequence_but_gains_no_power_over_deleted_records(self):
        self.deleted_x()
        report = self.run_import(hs.export(777, phone="phone-B", exported_at=self.LATE,
                                           steps=[hs.step("X", count=90, modified="2026-10-10T10:00:00Z"),
                                                  hs.step("new", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", count=7)]))
        self.assertEqual((report["status"], report["sequence"]), ("imported", 777))       # порядок файлов не ослаблен
        self.assertEqual((report["counts"]["steps"]["inserted"], report["not_applied"]["replay_of_deleted"]), (1, 1))
        self.assertIsNotNone(self.rows("SELECT deleted_at FROM step_intervals WHERE hc_id = 'X'")[0]["deleted_at"])

    def test_a_record_is_restored_only_by_a_file_exported_after_the_deletion_and_not_older(self):
        self.deleted_x()
        report = self.run_import(hs.export(1, phone="phone-B", exported_at=self.LATE,
                                           steps=[hs.step("X", count=100, modified="2026-10-10T17:00:00Z")]))
        self.assertEqual(report["counts"]["steps"]["updated"], 1)
        row = self.row()
        self.assertEqual((row["deleted_at"], row["revision"], row["hc_last_modified"]), (None, 2, "2026-10-10T17:00:00.000000000Z"))

    def test_an_exact_replay_at_the_same_export_second_as_the_deletion_is_ignored(self):
        self.deleted_x()
        report = self.run_import(hs.export(1, phone="phone-B", exported_at=hs.exported_at_for(2),
                                           steps=[hs.step("X", count=100, modified="2026-10-10T17:00:00Z")]))
        self.assertEqual(report["not_applied"]["replay_of_deleted"], 1)

    def test_hc_last_modified_never_decreases_on_any_path(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=100, modified="2026-10-10T17:00:00Z")]))
        seen = [self.row()["hc_last_modified"]]
        steps = [
            (hs.export(2, steps=[hs.step("X", count=90, modified="2026-10-10T10:00:00Z")]), {}),                    # старая версия
            (hs.export(3, deleted=["X"]), {}),
            (hs.export(1, phone="B", exported_at=self.LATE, steps=[hs.step("X", count=80, modified="2026-10-10T09:00:00Z")]), {}),
            (hs.export(4, steps=[hs.step("X", count=120, modified="2026-10-10T19:00:00Z")]), {}),                    # настоящее обновление
        ]
        for doc, kw in steps:
            self.run_import(doc, **kw)
            seen.append(self.row()["hc_last_modified"])
        self.assertEqual(seen, sorted(seen))

    def test_a_stale_version_of_a_live_record_is_still_ignored(self):
        self.run_import(hs.export(1, phone="phone-A", steps=[hs.step("X", count=100, modified="2026-10-10T17:00:00Z")]))
        report = self.run_import(hs.export(1, phone="phone-B", exported_at=self.LATE,
                                           steps=[hs.step("X", count=90, modified="2026-10-10T10:00:00Z")]))
        self.assertEqual((report["counts"]["steps"]["updated"], report["not_applied"]["stale"]), (0, 1))

    def test_all_three_types_are_protected(self):
        docs = hs.day_file(sequence=1)
        self.run_import(docs)
        self.run_import(hs.export(2, deleted=["step-1", "hr-1", "sleep-1"]))
        old = hs.export(1, phone="phone-B", exported_at=self.LATE,
                        steps=[hs.step("step-1", count=1, modified="2026-10-10T01:00:00Z")],
                        heart_rate=[hs.heart("hr-1", bpms=(55,), modified="2026-10-10T01:00:00Z")],
                        sleep_sessions=[hs.sleep("sleep-1", stages=[], modified="2026-10-10T01:00:00Z")])
        report = self.run_import(old)
        self.assertEqual(report["not_applied"]["replay_of_deleted"], 3)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM v_daily_sleep")[0]["n"], 0)
        self.assertEqual(self.count("sleep_stages"), 8)                                     # стадии не тронуты


# ======================= Д3: равное время изменения =======================

class EqualTimeTests(FixTestCase):
    T = "2026-10-10T19:12:00Z"

    def test_a_truncated_heart_rate_record_does_not_wipe_five_samples(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, modified=self.T)]))
        before = self.dump(DATA_TABLES)
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("hr", samples=[], modified=self.T)]))
        self.assertEqual((report["counts"]["heart_rate"]["updated"], report["counts"]["heart_rate"]["rejected"],
                          report["not_applied"]["conflict"]), (0, 1, 1))
        self.assertEqual(self.count("heart_rate_samples"), 5)
        self.assertEqual(self.dump(DATA_TABLES), before)
        self.assertTrue(any("конфликт" in w for w in report["warnings"]))

    def test_a_truncated_sleep_session_does_not_wipe_eight_stages(self):
        self.run_import(hs.export(1, sleep_sessions=[hs.sleep("sl", modified=self.T)]))
        self.assertEqual(self.count("sleep_stages"), 8)
        report = self.run_import(hs.export(2, sleep_sessions=[hs.sleep("sl", stages=[], modified=self.T)]))
        self.assertEqual((report["counts"]["sleep"]["rejected"], self.count("sleep_stages")), (1, 8))
        self.assertEqual(self.rows("SELECT revision FROM sleep_sessions")[0]["revision"], 1)

    def test_changed_values_at_equal_time_are_a_conflict(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, modified=self.T)],
                                  steps=[hs.step("s", count=5, modified=self.T)]))
        other = [dict(s, bpm=s["bpm"] + 1) for s in FIVE_SAMPLES]
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("hr", samples=other, modified=self.T)],
                                           steps=[hs.step("s", count=6, modified=self.T)]))
        self.assertEqual(report["not_applied"]["conflict"], 2)
        self.assertEqual(self.rows("SELECT MIN(bpm) AS m FROM heart_rate_samples")[0]["m"], 60)
        self.assertEqual(self.rows("SELECT count FROM step_intervals")[0]["count"], 5)

    def test_a_record_that_only_gains_data_at_equal_time_is_accepted(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES[:3], modified=self.T)]))
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, modified=self.T)]))
        self.assertEqual((report["counts"]["heart_rate"]["updated"], report["not_applied"]["conflict"]), (1, 0))
        self.assertEqual((self.count("heart_rate_samples"), self.rows("SELECT revision FROM heart_rate_records")[0]["revision"]), (5, 2))

    def test_gaining_data_while_changing_another_field_is_still_a_conflict(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES[:3], modified=self.T)]))
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, end="2026-10-10T19:11:30Z",
                                                                   modified=self.T)]))
        self.assertEqual(report["not_applied"]["conflict"], 1)
        self.assertEqual(self.count("heart_rate_samples"), 3)

    def test_an_empty_list_is_not_an_error_by_itself(self):
        # новая запись без отсчётов и без стадий допустима, повтор без изменений не конфликт
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=[], modified=self.T)],
                                  sleep_sessions=[hs.sleep("sl", stages=[], modified=self.T)]))
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("hr", samples=[], modified=self.T)],
                                           sleep_sessions=[hs.sleep("sl", stages=[], modified=self.T)]))
        self.assertEqual((report["counts"]["heart_rate"]["unchanged"], report["not_applied"]["conflict"]), (1, 0))

    def test_a_newer_version_with_fewer_samples_is_applied_with_a_visible_warning(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, modified=self.T)]))
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("hr", samples=[], modified="2026-10-10T19:30:00Z")]))
        self.assertEqual((report["counts"]["heart_rate"]["updated"], self.count("heart_rate_samples")), (1, 0))
        self.assertTrue(any("стало меньше данных (5 → 0)" in w for w in report["warnings"]))

    def test_conflicts_are_counted_in_the_journal_and_the_file_stays_idempotent(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, modified=self.T)]))
        doc = hs.export(2, heart_rate=[hs.heart("hr", samples=[], modified=self.T)])
        path = self.put(doc)
        health_import.import_file(path, self.db_path)
        counts = {(r["batch_id"], r["record_type"]): r["rejected"] for r in self.rows("SELECT * FROM import_batch_counts")}
        self.assertEqual(counts[(2, "heart_rate")], 1)
        self.assertEqual(health_import.import_file(path, self.db_path)["status"], "already_imported")


# ======================= Д4 и Д5: недопустимые символы =======================

class InvalidCharacterTests(FixTestCase):
    FIELDS = {
        "hc_id": lambda v: hs.export(1, steps=[hs.step(v)]),
        "client_record_id": lambda v: hs.export(1, steps=[hs.step("a", client_record_id=v)]),
        "package": lambda v: hs.export(1, steps=[hs.step("a")], sources=[hs.source("s1", v)]),
        "device model": lambda v: hs.export(1, steps=[hs.step("a")], sources=[hs.source("s1", "p", model=v)]),
        "source id": lambda v: hs.export(1, steps=[hs.step("a", src=v)], sources=[hs.source(v, "p")]),
        "sleep title": lambda v: hs.export(1, sleep_sessions=[hs.sleep(title=v)]),
        "sleep notes": lambda v: hs.export(1, sleep_sessions=[hs.sleep(notes=v)]),
        "deleted id": lambda v: hs.export(1, deleted=[v]),
        "phone_id": lambda v: hs.export(1, steps=[hs.step("a")], phone=v),
    }
    SURROGATE = "\ud800"
    NUL = "\u0000"

    def check(self, value, word):
        for field, build in self.FIELDS.items():
            for label, text in {"в начале": value + "abc", "внутри": "ab" + value + "cd", "в конце": "abc" + value, "одна": value}.items():
                with self.subTest(field=field, position=label):
                    before = self.dump(DATA_TABLES)
                    with self.assertRaises(ImportFileError) as ctx:
                        health_import.import_file(self.put_ascii(build(text)), self.db_path)
                    message = str(ctx.exception)
                    self.assertIn(word, message)
                    self.assertNotIn("CHECK", message)                      # ошибка валидации, а не ограничение базы
                    self.assertNotIn("Ошибка базы данных", message)
                    self.assertEqual(self.dump(DATA_TABLES), before)
                    self.assertEqual(self.count("import_batches") and self.rows(
                        "SELECT COUNT(*) AS n FROM import_batches WHERE status = 'ok'")[0]["n"], 0)

    def test_unpaired_surrogates_are_rejected_during_validation(self):
        self.check(self.SURROGATE, "суррогат")

    def test_nul_characters_are_rejected_during_validation(self):
        self.check(self.NUL, "NUL")

    def test_the_error_names_the_field_and_points_at_the_record(self):
        doc = hs.day_file()
        doc["steps"][2]["hc_id"] = "x" + self.NUL
        with self.assertRaises(ImportFileError) as ctx:
            health_import.import_file(self.put_ascii(doc), self.db_path)
        self.assertEqual(ctx.exception.problems[0][0], "steps[2].hc_id")

    def test_field_names_with_surrogates_do_not_break_the_message(self):
        doc = hs.day_file()
        doc["steps"][0]["bad" + self.SURROGATE] = 1
        with self.assertRaises(ImportFileError) as ctx:
            health_import.import_file(self.put_ascii(doc), self.db_path)
        text = str(ctx.exception)
        self.assertIn("неизвестные поля", text)
        text.encode("utf-8")                                                  # сообщение само по себе кодируется в UTF-8

    def test_normal_unicode_text_and_other_control_characters_still_work(self):
        report = self.run_import(hs.export(1, sleep_sessions=[hs.sleep(title="Сон 😴 после смены — ёж", notes="строка1\nстрока2\tтабуляция",
                                                                       stages=[])]))
        self.assertEqual(report["status"], "imported")
        self.assertEqual(self.rows("SELECT title FROM sleep_sessions")[0]["title"], "Сон 😴 после смены — ёж")

    def test_the_command_line_shows_a_message_not_a_traceback(self):
        env = {k: v for k, v in os.environ.items() if k != "FITNESS_DB_PATH"}
        env["PYTHONIOENCODING"] = "utf-8"
        for label, value in (("суррогат", self.SURROGATE), ("NUL", self.NUL)):
            with self.subTest(label):
                path = self.put_ascii(hs.export(1, steps=[hs.step("a" + value)]))
                proc = subprocess.run([sys.executable, str(ti.IMPORT_CLI), "--db", str(self.db_path), str(path)],
                                      capture_output=True, env=env, timeout=120)
                err = proc.stderr.decode("utf-8", "replace")
                self.assertEqual(proc.returncode, 1)
                self.assertNotIn("Traceback", err)
                self.assertIn("steps[0].hc_id", err)


# ======================= Д6: --dry-run =======================

class DryRunStateTests(FixTestCase):
    def test_dry_run_leaves_a_non_wal_database_and_its_directory_untouched(self):
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0], "delete")
        conn.close()
        for name in ("-wal", "-shm"):                                                  # убрать служебные файлы WAL
            Path(str(self.db_path) + name).unlink(missing_ok=True)
        listing = sorted(p.name for p in self.db_path.parent.iterdir())
        state, mode = self.dir_state(), self.journal_mode()
        self.assertEqual(mode, "delete")
        report = self.run_import(hs.day_file(), dry_run=True)
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(self.dir_state(), state)
        self.assertEqual(self.journal_mode(), "delete")                                # режим журнала не переключён на WAL
        self.assertEqual(sorted(p.name for p in self.db_path.parent.iterdir()), listing)     # новых файлов нет
        real = self.run_import(hs.day_file())
        self.assertEqual(real["counts"], report["counts"])

    def test_dry_run_does_not_create_wal_sidecar_files_next_to_a_quiet_database(self):
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.db_path) + suffix).exists())
        self.assertEqual(self.journal_mode(), "wal")
        listing = sorted(p.name for p in self.db_path.parent.iterdir())
        state = self.dir_state()
        self.run_import(hs.day_file(), dry_run=True)
        self.assertEqual(sorted(p.name for p in self.db_path.parent.iterdir()), listing)      # ни -wal, ни -shm не появилось
        self.assertEqual(self.dir_state(), state)
        self.assertEqual(self.journal_mode(), "wal")

    def test_dry_run_leaves_wal_files_untouched_and_sees_uncheckpointed_data(self):
        writer = db.connect(self.db_path, create=False)                                # держит WAL несброшенным
        try:
            health_import.import_file(self.put(hs.day_file(sequence=1)), self.db_path)
            wal = Path(str(self.db_path) + "-wal")
            self.assertGreater(wal.stat().st_size, 0)
            state = self.dir_state()
            self.assertIn(self.db_path.name + "-shm", state)
            report = self.run_import(hs.day_file(sequence=2), dry_run=True)            # те же записи, новый файл
            self.assertEqual({k: v["unchanged"] for k, v in report["counts"].items()}, {"steps": 3, "heart_rate": 3, "sleep": 1})
            self.assertEqual(self.dir_state(), state)                                  # основной файл и -wal побайтово те же
        finally:
            writer.close()

    def test_dry_run_does_not_wait_for_or_take_the_write_lock(self):
        holder = db.connect(self.db_path, create=False)
        try:
            holder.execute("BEGIN IMMEDIATE")
            report = self.run_import(hs.day_file(), dry_run=True)                      # реальный импорт ждал бы блокировку
            self.assertEqual(report["status"], "dry_run")
            holder.execute("ROLLBACK")
        finally:
            holder.close()

    def test_every_failing_dry_run_leaves_the_state_unchanged_too(self):
        self.run_import(hs.day_file(sequence=1))
        state = self.dir_state()
        bad = hs.day_file(sequence=2)
        bad["steps"][0]["count"] = 0
        for label, doc, error in (("invalid file", bad, ImportFileError), ("gap", hs.day_file(sequence=9), ImportOrderError)):
            with self.subTest(label), self.assertRaises(error):
                self.run_import(doc, dry_run=True)
        self.assertEqual(self.dir_state(), state)
        self.assertEqual(self.run_import(hs.day_file(sequence=1), name="again.json", dry_run=True)["status"], "already_imported")
        self.assertEqual(self.dir_state(), state)

    def test_dry_run_reports_the_same_counts_as_the_real_import_for_every_rule(self):
        self.run_import(hs.day_file(sequence=1))
        doc = hs.export(2, steps=[hs.step("step-1", count=600, modified="2026-10-10T18:00:00Z"),
                                  hs.step("step-2", count=1, modified="2026-10-10T01:00:00Z"),
                                  hs.step("step-9", "2026-10-10T10:00:00Z", "2026-10-10T10:10:00Z", count=9)],
                        deleted=["hr-1", "ghost"])
        dry = self.run_import(doc, dry_run=True)
        real = self.run_import(doc)
        self.assertEqual((dry["counts"], dry["not_applied"], dry["unmatched_deletions"]),
                         (real["counts"], real["not_applied"], real["unmatched_deletions"]))


# ======================= Д7: строгое время =======================

class StrictTimestampTests(unittest.TestCase):
    def test_surrounding_whitespace_and_line_breaks_are_not_accepted(self):
        base = "2026-10-10T07:02:00Z"
        for bad in (base + "\n", base + "\r\n", base + "\r", "\n" + base, " " + base, base + " ", "\t" + base, base + "\t",
                    " " + base, base + " ", base + "\u0000", "  " + base + "  "):
            with self.subTest(value=repr(bad)):
                problems = health_format.Problems()
                self.assertIsNone(health_format.parse_timestamp(bad, "t", problems))
                self.assertEqual(problems.total, 1)
                self.assertIn("пробелы и переводы строк недопустимы", problems.items[0][1])
        self.assertIsNotNone(health_format.parse_timestamp(base, "t", health_format.Problems()))

    def test_every_time_field_of_a_file_is_strict(self):
        bad = "2026-10-10T07:02:00Z" + "\n"
        docs = {
            "exported_at": hs.export(1, exported_at=bad),
            "start": hs.export(1, steps=[hs.step(start=bad)]),
            "end": hs.export(1, steps=[hs.step(end=bad)]),
            "last_modified": hs.export(1, steps=[hs.step(modified=bad)]),
            "sample time": hs.export(1, heart_rate=[hs.heart(samples=[{"time": bad, "bpm": 60}])]),
            "stage start": hs.export(1, sleep_sessions=[hs.sleep(stages=[{"stage": "light", "start": bad, "end": "2026-10-10T07:30:00Z"}])]),
        }
        for label, doc in docs.items():
            with self.subTest(label), self.assertRaises(ImportFileError):
                health_format.parse_export(hs.encode(doc))


# ======================= Время из будущего =======================

class FutureTimeTests(FixTestCase):
    EXPORTED = "2026-10-10T17:20:00Z"

    def rejected(self, **kwargs):
        with self.assertRaises(ImportFileError) as ctx:
            health_format.parse_export(hs.encode(hs.export(1, exported_at=self.EXPORTED, **kwargs)))
        return str(ctx.exception)

    def test_a_year_2100_last_modified_is_rejected_and_cannot_block_later_updates(self):
        self.assertIn("позже времени экспорта", self.rejected(steps=[hs.step("F", modified="2100-01-01T00:00:00Z")]))
        with self.assertRaises(ImportFileError):                                          # то же через импорт: попадает в журнал
            self.run_import(hs.export(1, steps=[hs.step("F", count=10, modified="2100-01-01T00:00:00Z")]))
        self.assertNothingImported()
        report = self.run_import(hs.export(1, steps=[hs.step("F", count=10)]))             # номер не потрачен
        self.assertEqual(report["status"], "imported")
        report = self.run_import(hs.export(2, steps=[hs.step("F", count=11, modified="2026-10-10T18:00:00Z")]))
        self.assertEqual(report["counts"]["steps"]["updated"], 1)                          # законное обновление проходит

    def test_a_future_end_cannot_pollute_the_last_record_time(self):
        self.assertIn("позже времени экспорта", self.rejected(steps=[hs.step("F", start="2100-01-01T00:00:00Z", end="2100-01-01T00:10:00Z")]))
        self.run_import(hs.day_file())
        last = self.rows("SELECT last_record_end_utc FROM v_health_sync_status WHERE record_type = 'steps'")[0]
        self.assertTrue(last["last_record_end_utc"].startswith("2026-10-10"))

    def test_the_boundary_is_24_hours_after_the_export(self):
        health_format.parse_export(hs.encode(hs.export(1, exported_at=self.EXPORTED,
                                                       steps=[hs.step("a", start="2026-10-11T17:10:00Z", end="2026-10-11T17:20:00Z")])))
        self.rejected(steps=[hs.step("a", start="2026-10-11T17:10:00Z", end="2026-10-11T17:20:01Z")])

    def test_future_sample_and_stage_times_are_rejected_too(self):
        self.rejected(heart_rate=[hs.heart(samples=[{"time": "2026-10-13T00:00:00Z", "bpm": 60}])])
        self.rejected(sleep_sessions=[hs.sleep(stages=[{"stage": "deep", "start": "2026-10-10T07:02:00Z", "end": "2026-10-13T00:00:00Z"}])])

    def test_past_times_and_the_example_day_are_unaffected(self):
        health_format.parse_export(hs.encode(hs.day_file()))


# ======================= Пропущенные файлы =======================

class SkippedFileTests(FixTestCase):
    def test_the_refusal_tells_how_to_import_a_skipped_file_later(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        with self.assertRaises(ImportOrderError) as gap:
            self.run_import(hs.export(3, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]))
        self.assertIn("--allow-gaps", str(gap.exception))
        self.assertIn("--allow-older", str(gap.exception))
        self.run_import(hs.export(3, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]), allow_gaps=True)
        with self.assertRaises(ImportOrderError) as old:
            self.run_import(hs.export(2, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]))
        self.assertIn("--allow-older", str(old.exception))

    def test_allow_gaps_warns_and_names_the_skipped_numbers(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        report = self.run_import(hs.export(4, steps=[hs.step("d", "2026-10-10T00:30:00Z", "2026-10-10T00:40:00Z")]), allow_gaps=True)
        warning = " ".join(report["warnings"])
        self.assertIn("№2…№3", warning)
        self.assertIn("--allow-older", warning)

    def test_a_skipped_file_is_imported_later_without_undoing_newer_data(self):
        self.run_import(hs.export(1, steps=[hs.step("a", count=10)]))
        self.run_import(hs.export(3, steps=[hs.step("a", count=30, modified="2026-10-10T18:00:00Z"),
                                            hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z", count=3)]), allow_gaps=True)
        skipped = hs.export(2, steps=[hs.step("a", count=20, modified="2026-10-10T17:30:00Z"),
                                      hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", count=2)])
        report = self.run_import(skipped, allow_older=True)
        self.assertEqual((report["status"], report["counts"]["steps"]["inserted"], report["not_applied"]["stale"]), ("imported", 1, 1))
        self.assertEqual({r["hc_id"]: r["count"] for r in self.rows("SELECT hc_id, count FROM step_intervals")},
                         {"a": 30, "b": 2, "c": 3})                                        # «a» не откатилась на 20
        self.assertIsNotNone(report["warnings"])
        self.assertIntegrity()

    def test_idempotency_and_sequence_uniqueness_are_not_weakened_by_allow_older(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        skipped = hs.export(2, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")])
        self.run_import(hs.export(3, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]), allow_gaps=True)
        self.assertEqual(self.run_import(skipped, name="skipped.json", allow_older=True)["status"], "imported")
        self.assertEqual(self.run_import(skipped, name="skipped-copy.json", allow_older=True)["status"], "already_imported")
        with self.assertRaises(ImportOrderError) as ctx:                                   # номер №2 уже принят: другое содержимое нельзя
            self.run_import(hs.export(2, steps=[hs.step("z", "2026-10-10T00:50:00Z", "2026-10-10T01:00:00Z")]), allow_older=True)
        self.assertIn("уже принят", str(ctx.exception))
        with self.assertRaises(ImportOrderError):                                          # и по-прежнему нельзя без ключа
            self.run_import(hs.export(1, phone="phone-01", exported_at="2026-10-10T20:00:00Z", steps=[hs.step("q")]))
        self.assertEqual(self.rows("SELECT MAX(sequence) AS m FROM import_batches WHERE status = 'ok'")[0]["m"], 3)

    def test_the_normal_order_continues_after_a_skipped_file_was_added(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        self.run_import(hs.export(3, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]), allow_gaps=True)
        self.run_import(hs.export(2, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]), allow_older=True)
        self.assertEqual(self.run_import(hs.export(4, steps=[hs.step("d", "2026-10-10T00:30:00Z", "2026-10-10T00:40:00Z")]))["status"], "imported")

    def test_dry_run_understands_allow_older(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        self.run_import(hs.export(3, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]), allow_gaps=True)
        state = self.dir_state()
        doc = hs.export(2, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")])
        with self.assertRaises(ImportOrderError):
            self.run_import(doc, dry_run=True)
        self.assertEqual(self.run_import(doc, dry_run=True, allow_older=True)["status"], "dry_run")
        self.assertEqual(self.dir_state(), state)


# ======================= Журнал неудач и ошибки чтения файлов =======================

class FailureJournalAndReadErrorTests(FixTestCase):
    def test_repeating_the_same_failure_does_not_grow_the_journal(self):
        bad = hs.day_file()
        bad["steps"][0]["count"] = 0
        path = self.put(bad)
        ids = set()
        for _ in range(6):
            with self.assertRaises(ImportFileError) as ctx:
                health_import.import_file(path, self.db_path)
            ids.add(ctx.exception.journal_batch_id)
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM import_batches")[0]["n"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM import_batch_counts")[0]["n"], 3)

    def test_a_different_failure_of_the_same_file_is_still_recorded(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        later = self.put(hs.export(5, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]))
        with self.assertRaises(ImportOrderError):
            health_import.import_file(later, self.db_path)                                  # пропуск
        health_import.import_file(later, self.db_path, allow_gaps=True)                     # теперь проходит
        statuses = [r["status"] for r in self.rows("SELECT status FROM import_batches ORDER BY id")]
        self.assertEqual(statuses, ["ok", "failed", "ok"])

    def test_a_file_that_cannot_be_read_gives_a_clear_error(self):
        path = self.put(hs.day_file())
        with mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError(13, "Отказано в доступе")):
            with self.assertRaises(ImportFileError) as ctx:
                health_import.import_file(path, self.db_path)
        self.assertIn("не удалось прочитать файл", str(ctx.exception))
        self.assertIn("Отказано в доступе", str(ctx.exception))
        self.assertNothingImported()

    def test_the_command_line_survives_unreadable_files_and_unexpected_errors(self):
        path = self.put(hs.day_file())
        err = io.StringIO()
        with mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError(13, "Отказано в доступе")), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = import_health.main(["--db", str(self.db_path), str(self.incoming)])        # папка: и сортировка, и импорт
        self.assertEqual(code, 1)
        self.assertIn("не удалось прочитать файл", err.getvalue())
        err = io.StringIO()
        with mock.patch.object(health_import, "import_file", side_effect=RuntimeError("boom")), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = import_health.main(["--db", str(self.db_path), str(path)])
        self.assertEqual(code, 2)
        self.assertIn("НЕОЖИДАННАЯ ОШИБКА", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_the_command_line_prints_conflicts_and_not_applied_counts(self):
        self.run_import(hs.export(1, heart_rate=[hs.heart("hr", samples=FIVE_SAMPLES, modified="2026-10-10T19:12:00Z")]))
        path = self.put(hs.export(2, heart_rate=[hs.heart("hr", samples=[], modified="2026-10-10T19:12:00Z")]))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = import_health.main(["--db", str(self.db_path), str(path)])
        self.assertEqual(code, 0)
        self.assertIn("КОНФЛИКТОВ", out.getvalue())
        self.assertIn("ВНИМАНИЕ", out.getvalue())


# ======================= Параллельные запуски, внешние ключи =======================

class ConcurrencyTests(FixTestCase):
    def run_threads(self, targets):
        outcomes = []

        def worker(fn):
            try:
                outcomes.append(("ok", fn()))
            except TrackerError as exc:
                outcomes.append(("error", type(exc).__name__))

        threads = [threading.Thread(target=worker, args=(fn,)) for fn in targets]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return outcomes

    def test_the_same_file_in_parallel_is_imported_exactly_once(self):
        path = self.put(hs.day_file())
        outcomes = self.run_threads([lambda: health_import.import_file(path, self.db_path)["status"]] * 6)
        self.assertEqual(sorted(o[1] for o in outcomes), ["already_imported"] * 5 + ["imported"])
        self.assertEqual((self.count("step_intervals"), self.count("import_batches")), (3, 1))
        self.assertIntegrity()

    def test_consecutive_files_in_parallel_never_leave_inconsistent_data(self):
        files = [self.put(hs.export(n, steps=[hs.step(f"s{n}", f"2026-10-10T0{n}:00:00Z", f"2026-10-10T0{n}:10:00Z")])) for n in (1, 2, 3, 4)]
        outcomes = self.run_threads([lambda p=p: health_import.import_file(p, self.db_path)["status"] for p in files])
        accepted = self.rows("SELECT sequence FROM import_batches WHERE status = 'ok' ORDER BY sequence")
        sequences = [r["sequence"] for r in accepted]
        self.assertEqual(sequences, list(range(sequences[0], sequences[0] + len(sequences))))        # без дыр
        self.assertEqual(self.count("step_intervals"), len(sequences))
        self.assertEqual(len([o for o in outcomes if o[0] == "ok"]), len(sequences))
        self.assertIntegrity()

    def test_foreign_keys_and_integrity_after_a_mixed_history(self):
        self.run_import(hs.day_file(sequence=1))
        self.run_import(hs.export(2, deleted=["step-1", "hr-2"], steps=[hs.step("step-2", count=900, modified="2026-10-10T18:00:00Z")]))
        self.run_import(hs.export(1, phone="phone-B", exported_at="2026-10-10T18:00:00Z",
                                  steps=[hs.step("step-1", count=1, modified="2026-10-10T01:00:00Z")]))
        self.run_import(hs.export(3, heart_rate=[hs.heart("hr-3", samples=[], modified="2026-10-10T19:30:00Z")]))
        self.assertIntegrity()
        conn = db.connect(self.db_path, create=False)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            orphans = conn.execute("SELECT COUNT(*) FROM heart_rate_samples WHERE record_id NOT IN (SELECT id FROM heart_rate_records)").fetchone()[0]
            self.assertEqual(orphans, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
