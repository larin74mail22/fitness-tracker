"""Импорт выгрузки Health Connect: три типа данных, точность времени, порядок файлов, повторы, обновления,
удаления, откат, пробный запуск, командная строка.

Все базы временные, все файлы синтетические и лежат во временной папке теста.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
import time
import unittest

from fitness import db, health_import
from fitness.errors import (DatabaseMissingError, ImportFileError, ImportOrderError, MigrationError,
                            MigrationPendingError, TrackerError)
from tests import health_helpers as h
from tests import health_samples as hs

IMPORT_CLI = h.PROJECT_DIR / "import_health.py"
DATA_TABLES = ["data_sources", "step_intervals", "heart_rate_records", "heart_rate_samples", "sleep_sessions",
               "sleep_stages"]


class ImportTestCase(h.IsolatedTestCase):
    def setUp(self):
        super().setUp()
        db.open_database(self.db_path).close()                   # пустая база последней версии (v3)
        self.incoming = self.tmp / "incoming"
        self.counter = 0

    def put(self, doc, name=None):
        self.counter += 1
        return hs.write(self.incoming, name or f"export-{self.counter:03d}.json", doc)

    def run_import(self, doc, name=None, **kwargs):
        return health_import.import_file(self.put(doc, name), self.db_path, **kwargs)

    def rows(self, sql, *params, path=None):
        conn = db.connect(path or self.db_path, create=False)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def dump(self, tables=None, path=None):
        conn = db.connect(path or self.db_path, create=False)
        try:
            names = tables or [t for t in db._user_tables(conn) if t != "schema_migrations"]
            return {t: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{t}"')), key=repr) for t in names}
        finally:
            conn.close()

    def dump_content(self, tables, path=None):
        """Содержимое таблиц без привязки к батчу и служебных меток обновления: повторный файл с теми же данными
        подтверждает запись (batch_id переходит на более поздний батч), но содержимое не меняется."""
        conn = db.connect(path or self.db_path, create=False)
        try:
            out = {}
            for t in tables:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")') if r[1] not in ("batch_id", "updated_at")]
                out[t] = sorted((tuple(r) for r in conn.execute(f'SELECT {", ".join(cols)} FROM "{t}"')), key=repr)
            return out
        finally:
            conn.close()

    def count(self, table):
        return self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def assertNothingImported(self):
        for table in DATA_TABLES:
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM import_batches WHERE status = 'ok'")[0]["n"], 0)


class FirstImportTests(ImportTestCase):
    def test_example_day_is_imported_with_all_three_types(self):
        report = self.run_import(hs.day_file())
        self.assertEqual(report["status"], "imported")
        self.assertEqual(report["batch_id"], 1)
        self.assertEqual({k: v["inserted"] for k, v in report["counts"].items()}, {"steps": 3, "heart_rate": 3, "sleep": 1})
        self.assertTrue(all(v["updated"] == v["unchanged"] == v["deleted"] == v["rejected"] == 0
                            for v in report["counts"].values()))

        step = self.rows("SELECT * FROM step_intervals ORDER BY start_utc")[0]
        self.assertEqual((step["start_utc"], step["end_utc"], step["count"]),
                         ("2026-10-10T00:00:00.000000000Z", "2026-10-10T00:10:00.000000000Z", 511))
        self.assertEqual((step["local_date"], step["revision"], step["batch_id"], step["deleted_at"], step["hc_id"]),
                         ("2026-10-10", 1, 1, None, "step-1"))
        self.assertEqual(step["hc_last_modified"], "2026-10-10T17:00:00.000000000Z")
        self.assertEqual((step["start_offset_min"], step["end_offset_min"]), (180, 180))

        self.assertEqual(self.rows("SELECT steps, intervals FROM v_daily_steps"), [{"steps": 1393, "intervals": 3}])
        hr = self.rows("SELECT * FROM v_daily_heart_rate")[0]
        self.assertEqual((hr["min_bpm"], hr["max_bpm"], hr["samples"]), (66, 73, 3))
        sleep = self.rows("SELECT * FROM v_daily_sleep")[0]
        self.assertEqual((sleep["session_min"], sleep["light_min"], sleep["deep_min"], sleep["awake_min"]), (495, 105, 20, 10))
        self.assertEqual(self.count("sleep_stages"), 8)

        source = self.rows("SELECT * FROM data_sources")
        self.assertEqual(len(source), 1)
        self.assertEqual((source[0]["source_key"], source[0]["platform"], source[0]["package_name"]),
                         ("health_connect|app.example.zepp|||", "health_connect", "app.example.zepp"))
        batch = self.rows("SELECT * FROM import_batches")[0]
        self.assertEqual((batch["status"], batch["phone_id"], batch["sequence"], batch["format_version"], batch["error"]),
                         ("ok", "phone-01", 1, 1, None))
        self.assertEqual((batch["sha256"], batch["exported_at"]), (report["sha256"], "2026-10-10T17:20:00Z"))
        counts = {r["record_type"]: r["inserted"] for r in self.rows("SELECT * FROM import_batch_counts")}
        self.assertEqual(counts, {"steps": 3, "heart_rate": 3, "sleep": 1})
        status = {r["record_type"]: r for r in self.rows("SELECT * FROM v_health_sync_status")}
        self.assertEqual(status["steps"]["last_record_end_utc"], "2026-10-10T16:40:00.000000000Z")
        self.assertEqual(status["sleep"]["last_record_end_utc"], "2026-10-10T15:20:00.000000000Z")
        self.assertIsNotNone(status["steps"]["last_ok_import_at"])

    def test_fractional_seconds_are_preserved(self):
        steps = [hs.step("a", "2026-10-10T00:00:00.123456789Z", "2026-10-10T00:10:00.5Z", modified="2026-10-10T17:00:00.000000007Z"),
                 hs.step("b", "2026-10-10T00:10:00.5Z", "2026-10-10T00:10:00.500000001Z", count=3),     # окно в одну наносекунду
                 hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00.000Z", count=4)]
        samples = [{"time": "2026-10-10T19:10:00.000000001Z", "bpm": 60}, {"time": "2026-10-10T19:10:00.000000002Z", "bpm": 61},
                   {"time": "2026-10-10T19:10:00.1Z", "bpm": 62}]
        stages = [{"stage": "deep", "start": "2026-10-10T07:05:00.25Z", "end": "2026-10-10T07:05:00.250000001Z"}]
        self.run_import(hs.export(steps=steps, heart_rate=[hs.heart(samples=samples)], sleep_sessions=[hs.sleep(stages=stages)]))
        got = {r["hc_id"]: (r["start_utc"], r["end_utc"]) for r in self.rows("SELECT * FROM step_intervals")}
        self.assertEqual(got["a"], ("2026-10-10T00:00:00.123456789Z", "2026-10-10T00:10:00.500000000Z"))
        self.assertEqual(got["b"], ("2026-10-10T00:10:00.500000000Z", "2026-10-10T00:10:00.500000001Z"))
        self.assertEqual(got["c"], ("2026-10-10T00:20:00.000000000Z", "2026-10-10T00:30:00.000000000Z"))
        self.assertEqual(self.rows("SELECT hc_last_modified FROM step_intervals WHERE hc_id = 'a'")[0]["hc_last_modified"],
                         "2026-10-10T17:00:00.000000007Z")
        self.assertEqual([r["time_utc"] for r in self.rows("SELECT time_utc FROM heart_rate_samples ORDER BY time_utc")],
                         ["2026-10-10T19:10:00.000000001Z", "2026-10-10T19:10:00.000000002Z", "2026-10-10T19:10:00.100000000Z"])
        stage = self.rows("SELECT * FROM sleep_stages")[0]
        self.assertEqual((stage["start_utc"], stage["end_utc"]), ("2026-10-10T07:05:00.250000000Z", "2026-10-10T07:05:00.250000001Z"))

    def test_same_instant_spelled_differently_is_not_a_change(self):
        self.run_import(hs.export(1, steps=[hs.step("a", "2026-10-10T00:00:00Z", "2026-10-10T00:10:00Z")]))
        report = self.run_import(hs.export(2, steps=[hs.step("a", "2026-10-10T00:00:00.000Z", "2026-10-10T00:10:00.000000000Z")]))
        self.assertEqual(report["counts"]["steps"]["unchanged"], 1)
        self.assertEqual(self.rows("SELECT revision FROM step_intervals")[0]["revision"], 1)

    def test_offsets_and_local_date(self):
        steps = [hs.step("night", "2026-10-10T22:00:00Z", "2026-10-10T22:10:00Z", offset=180),
                 hs.step("nofset", "2026-10-10T22:10:00Z", "2026-10-10T22:20:00Z", offset=None)]
        self.run_import(hs.export(steps=steps, fallback=0))
        rows = {r["hc_id"]: r for r in self.rows("SELECT * FROM step_intervals")}
        self.assertEqual((rows["night"]["local_date"], rows["night"]["start_offset_min"]), ("2026-10-11", 180))
        self.assertEqual((rows["nofset"]["local_date"], rows["nofset"]["start_offset_min"]), ("2026-10-10", None))

    def test_only_used_sources_are_created_and_reused(self):
        sources = [hs.source("s1", "app.one"), hs.source("s2", "app.two"), hs.source("s3", "app.unused")]
        self.run_import(hs.export(1, steps=[hs.step("a", src="s1"), hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", src="s2")],
                                  sources=sources))
        self.run_import(hs.export(2, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z", src="s1")],
                                  sources=[hs.source("s1", "app.one")]))
        self.assertEqual(sorted(r["package_name"] for r in self.rows("SELECT * FROM data_sources")), ["app.one", "app.two"])
        self.assertEqual(self.rows("SELECT COUNT(DISTINCT source_id) AS n FROM step_intervals WHERE hc_id IN ('a', 'c')")[0]["n"], 1)

    def test_old_categories_are_untouched(self):
        conn = db.connect(self.db_path, create=False)
        h.add_old_data(conn)
        conn.close()
        old = ["measurement_sessions", "body_measurements", "workouts", "workout_exercises", "exercise_sets", "meals",
               "meal_items", "alcohol_entries", "notes", "note_fields", "exercises", "measurement_types", "photos"]
        before = self.dump(old)
        self.run_import(hs.day_file())
        self.assertEqual(self.dump(old), before)


class SequenceTests(ImportTestCase):
    def fresh_db(self, name):
        path = self.tmp / "data" / name
        db.open_database(path).close()
        return path

    def test_first_file_may_have_any_positive_sequence(self):
        for seq in (1, 2, 42, 9_007_199_254_740_991):
            with self.subTest(sequence=seq):
                path = self.fresh_db(f"first-{seq}.db")
                report = health_import.import_file(self.put(hs.day_file(sequence=seq)), path)
                self.assertEqual((report["status"], report["sequence"]), ("imported", seq))

    def test_gap_check_does_not_block_the_initial_import_but_works_afterwards(self):
        path = self.fresh_db("start42.db")
        imp = lambda doc, **kw: health_import.import_file(self.put(doc), path, **kw)
        self.assertEqual(imp(hs.export(42, steps=[hs.step("a")]))["status"], "imported")      # начальный импорт с №42
        self.assertEqual(imp(hs.export(43, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]))["status"], "imported")
        with self.assertRaises(ImportOrderError) as ctx:
            imp(hs.export(45, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]))
        self.assertIn("44", str(ctx.exception))
        self.assertIn("--allow-gaps", str(ctx.exception))
        with self.assertRaises(ImportOrderError):
            imp(hs.export(1, steps=[hs.step("d", "2026-10-10T00:30:00Z", "2026-10-10T00:40:00Z")]))      # старше принятых
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM step_intervals", path=path)[0]["n"], 2)

    def test_refused_file_is_journaled_and_does_not_disturb_the_order(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        before = self.dump(DATA_TABLES)
        with self.assertRaises(ImportOrderError):
            self.run_import(hs.export(3, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]))
        self.assertEqual(self.dump(DATA_TABLES), before)
        failed = self.rows("SELECT * FROM import_batches WHERE status = 'failed'")
        self.assertEqual((len(failed), failed[0]["sequence"]), (1, 3))
        self.assertIn("Пропущены файлы", failed[0]["error"])
        report = self.run_import(hs.export(2, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]))
        self.assertEqual(report["status"], "imported")                      # ожидаемый №2 принимается после неудачной попытки №3

    def test_allow_gaps(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        report = self.run_import(hs.export(5, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]), allow_gaps=True)
        self.assertEqual(report["status"], "imported")
        self.assertTrue(any("пропущены" in w for w in report["warnings"]))
        with self.assertRaises(ImportOrderError):
            self.run_import(hs.export(4, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]), allow_gaps=True)

    def test_same_sequence_with_different_content_is_refused_but_same_file_is_a_repeat(self):
        first = hs.export(1, steps=[hs.step("a")])
        self.run_import(first)
        with self.assertRaises(ImportOrderError):
            self.run_import(hs.export(1, steps=[hs.step("a", count=999)]))
        self.assertEqual(self.run_import(first, name="copy-of-first.json")["status"], "already_imported")

    def test_phones_have_independent_sequences(self):
        self.run_import(hs.export(1, phone="phone-A", steps=[hs.step("a")]))
        self.run_import(hs.export(1, phone="phone-B", steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]))
        self.run_import(hs.export(2, phone="phone-A", steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]))
        self.assertEqual(self.count("step_intervals"), 3)


class RepeatAndUpdateTests(ImportTestCase):
    def test_same_file_twice_changes_nothing(self):
        path = self.put(hs.day_file())
        first = health_import.import_file(path, self.db_path)
        before = self.dump()
        second = health_import.import_file(path, self.db_path)
        copy = health_import.import_file(hs.write(self.incoming, "same-bytes-other-name.json", path.read_bytes()), self.db_path)
        self.assertEqual((second["status"], copy["status"]), ("already_imported", "already_imported"))
        self.assertEqual(second["batch_id"], first["batch_id"])
        self.assertEqual(self.dump(), before)                                     # побайтово то же, включая updated_at и журнал
        self.assertEqual(self.count("import_batches"), 1)

    def test_reexport_with_unchanged_records_changes_no_data(self):
        self.run_import(hs.day_file(sequence=1))
        before = self.dump_content(DATA_TABLES)
        report = self.run_import(hs.day_file(sequence=2))
        self.assertEqual({k: (v["inserted"], v["updated"], v["unchanged"]) for k, v in report["counts"].items()},
                         {"steps": (0, 0, 3), "heart_rate": (0, 0, 3), "sleep": (0, 0, 1)})
        self.assertEqual(self.dump_content(DATA_TABLES), before)
        self.assertEqual({r["batch_id"] for r in self.rows("SELECT batch_id FROM step_intervals")}, {report["batch_id"]})   # подтверждены вторым файлом
        self.assertEqual({r["revision"] for r in self.rows("SELECT revision FROM step_intervals")}, {1})
        self.assertEqual(self.count("import_batches"), 2)

    def test_newer_versions_update_records_and_children(self):
        self.run_import(hs.day_file(sequence=1))
        ids = {t: [r["id"] for r in self.rows(f"SELECT id FROM {t} ORDER BY id")] for t in
               ("step_intervals", "heart_rate_records", "sleep_sessions")}
        new_modified = "2026-10-10T18:00:00Z"
        new_stages = [dict(hs.SCREEN_STAGES[0], stage="deep"), dict(hs.SCREEN_STAGES[1], stage="rem")]
        doc = hs.export(2, steps=[hs.step("step-1", count=600, modified=new_modified)],
                        heart_rate=[hs.heart("hr-1", bpms=(70, 71), modified=new_modified)],
                        sleep_sessions=[hs.sleep("sleep-1", stages=new_stages, title="новое название", modified=new_modified)])
        report = self.run_import(doc)
        self.assertEqual({k: (v["inserted"], v["updated"], v["unchanged"]) for k, v in report["counts"].items()},
                         {"steps": (0, 1, 0), "heart_rate": (0, 1, 0), "sleep": (0, 1, 0)})
        step = self.rows("SELECT * FROM step_intervals WHERE hc_id = 'step-1'")[0]
        self.assertEqual((step["count"], step["revision"], step["batch_id"], step["hc_last_modified"], step["id"]),
                         (600, 2, 2, "2026-10-10T18:00:00.000000000Z", ids["step_intervals"][0]))
        self.assertEqual(self.rows("SELECT bpm FROM heart_rate_samples WHERE record_id = ? ORDER BY time_utc",
                                   ids["heart_rate_records"][0]), [{"bpm": 70}, {"bpm": 71}])          # старый отсчёт заменён
        session = self.rows("SELECT * FROM sleep_sessions")[0]
        self.assertEqual((session["title"], session["revision"], session["id"]), ("новое название", 2, ids["sleep_sessions"][0]))
        self.assertEqual([r["stage"] for r in self.rows("SELECT stage FROM sleep_stages ORDER BY start_utc")], ["deep", "rem"])
        # остальные записи не тронуты
        self.assertEqual({r["revision"] for r in self.rows("SELECT revision FROM step_intervals WHERE hc_id != 'step-1'")}, {1})
        self.assertEqual(self.count("heart_rate_records"), 3)

    def test_older_version_is_not_applied(self):
        self.run_import(hs.day_file(sequence=1))
        before = self.dump(DATA_TABLES)
        report = self.run_import(hs.export(2, steps=[hs.step("step-1", count=1, modified="2026-10-10T16:00:00Z")]))
        self.assertEqual(report["counts"]["steps"]["unchanged"], 1)
        self.assertTrue(any("старше" in w for w in report["warnings"]))
        self.assertEqual(self.dump(DATA_TABLES), before)

    def test_equal_modified_time_with_different_content_is_a_conflict_and_is_not_applied(self):
        # Раньше такая запись молча заменяла прежнюю (дефект Д3); теперь это конфликт: данные остаются, запись в отчёте.
        self.run_import(hs.day_file(sequence=1))
        before = self.dump(DATA_TABLES)
        report = self.run_import(hs.export(2, steps=[hs.step("step-1", count=777)]))
        self.assertEqual((report["counts"]["steps"]["updated"], report["counts"]["steps"]["rejected"]), (0, 1))
        self.assertEqual(report["not_applied"]["conflict"], 1)
        self.assertEqual(self.dump(DATA_TABLES), before)

    def test_update_writes_new_counts_for_the_batch(self):
        self.run_import(hs.day_file(sequence=1))
        self.run_import(hs.export(2, steps=[hs.step("step-1", count=600, modified="2026-10-10T18:00:00Z")]))
        counts = {(r["batch_id"], r["record_type"]): (r["inserted"], r["updated"]) for r in self.rows("SELECT * FROM import_batch_counts")}
        self.assertEqual(counts[(1, "steps")], (3, 0))
        self.assertEqual(counts[(2, "steps")], (0, 1))
        self.assertEqual(len(counts), 6)


class DeletionTests(ImportTestCase):
    def test_delete_by_id_in_every_type(self):
        self.run_import(hs.day_file(sequence=1))
        report = self.run_import(hs.export(2, deleted=["step-2", "hr-3", "sleep-1", "never-seen"]))
        self.assertEqual({k: v["deleted"] for k, v in report["counts"].items()}, {"steps": 1, "heart_rate": 1, "sleep": 1})
        self.assertEqual((report["unmatched_deletions"], report["unmatched_ids"]), (1, ["never-seen"]))
        for table, hc in (("step_intervals", "step-2"), ("heart_rate_records", "hr-3"), ("sleep_sessions", "sleep-1")):
            row = self.rows(f"SELECT deleted_at, batch_id FROM {table} WHERE hc_id = ?", hc)[0]
            self.assertRegex(row["deleted_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertEqual(row["batch_id"], 2)
        self.assertEqual((self.count("step_intervals"), self.count("heart_rate_records"), self.count("sleep_sessions")), (3, 3, 1))
        self.assertEqual(self.rows("SELECT steps FROM v_daily_steps")[0]["steps"], 511 + 6)          # удалённое скрыто представлениями
        self.assertEqual(self.rows("SELECT samples FROM v_daily_heart_rate")[0]["samples"], 2)
        self.assertEqual(self.rows("SELECT * FROM v_daily_sleep"), [])
        self.assertEqual(self.rows("SELECT deleted FROM import_batch_counts WHERE batch_id = 2 AND record_type = 'sleep'"),
                         [{"deleted": 1}])

    def test_deleting_twice_is_harmless_and_not_unmatched(self):
        self.run_import(hs.day_file(sequence=1))
        self.run_import(hs.export(2, deleted=["step-2"]))
        before = self.dump(DATA_TABLES)
        report = self.run_import(hs.export(3, deleted=["step-2"]))
        self.assertEqual((report["counts"]["steps"]["deleted"], report["unmatched_deletions"]), (0, 0))
        self.assertEqual(self.dump(DATA_TABLES), before)

    def test_absence_from_a_later_file_is_not_a_deletion(self):
        self.run_import(hs.day_file(sequence=1))
        before = self.dump_content(DATA_TABLES)
        self.run_import(hs.export(2, steps=[hs.step("step-1")]))                       # в файле только одна запись
        self.run_import(hs.export(3))                                                  # пустой файл
        self.assertEqual(self.dump_content(DATA_TABLES), before)
        for table in ("step_intervals", "heart_rate_records", "sleep_sessions"):
            self.assertEqual(self.rows(f"SELECT COUNT(*) AS n FROM {table} WHERE deleted_at IS NOT NULL")[0]["n"], 0)

    def test_deleted_record_that_reappears_is_restored(self):
        self.run_import(hs.day_file(sequence=1))
        self.run_import(hs.export(2, deleted=["step-1"]))
        self.assertEqual(self.rows("SELECT steps FROM v_daily_steps")[0]["steps"], 876 + 6)
        report = self.run_import(hs.export(3, steps=[hs.step("step-1")]))                # тот же состав, что был
        self.assertEqual(report["counts"]["steps"]["updated"], 1)
        row = self.rows("SELECT deleted_at, revision, batch_id FROM step_intervals WHERE hc_id = 'step-1'")[0]
        self.assertEqual((row["deleted_at"], row["revision"], row["batch_id"]), (None, 2, 3))
        self.assertEqual(self.rows("SELECT steps FROM v_daily_steps")[0]["steps"], 1393)

    def test_deleted_record_returning_with_older_content_is_not_restored(self):
        # Раньше устаревшая версия воскрешала удалённую запись и понижала hc_last_modified (дефект Д2).
        self.run_import(hs.day_file(sequence=1))
        self.run_import(hs.export(2, deleted=["step-1"]))
        report = self.run_import(hs.export(3, steps=[hs.step("step-1", count=5, modified="2026-10-10T01:00:00Z")]))
        self.assertEqual((report["counts"]["steps"]["updated"], report["not_applied"]["replay_of_deleted"]), (0, 1))
        row = self.rows("SELECT count, deleted_at, hc_last_modified FROM step_intervals WHERE hc_id = 'step-1'")[0]
        self.assertEqual((row["count"], row["hc_last_modified"]), (511, "2026-10-10T17:00:00.000000000Z"))
        self.assertIsNotNone(row["deleted_at"])

    def test_deleted_record_returning_with_newer_content_is_updated(self):
        self.run_import(hs.day_file(sequence=1))
        self.run_import(hs.export(2, deleted=["step-1"]))
        report = self.run_import(hs.export(3, steps=[hs.step("step-1", count=5, modified="2026-10-10T18:30:00Z")]))
        self.assertEqual(report["counts"]["steps"]["updated"], 1)
        self.assertEqual(self.rows("SELECT count, deleted_at FROM step_intervals WHERE hc_id = 'step-1'"),
                         [{"count": 5, "deleted_at": None}])


class RollbackTests(ImportTestCase):
    def test_one_bad_record_rejects_the_whole_file_and_is_journaled(self):
        doc = hs.day_file(sequence=1)
        doc["heart_rate"][2]["samples"][0]["bpm"] = 999
        doc["steps"][1]["count"] = 0
        with self.assertRaises(ImportFileError) as ctx:
            self.run_import(doc)
        self.assertEqual(ctx.exception.total, 2)
        self.assertTrue(ctx.exception.journal_batch_id)
        self.assertNothingImported()
        self.assertEqual(self.count("data_sources"), 0)
        failed = self.rows("SELECT * FROM import_batches")
        self.assertEqual([(r["status"], r["sequence"], r["phone_id"]) for r in failed], [("failed", 1, "phone-01")])
        self.assertIn("heart_rate[2].samples[0].bpm", failed[0]["error"])
        counts = {r["record_type"]: r["rejected"] for r in self.rows("SELECT * FROM import_batch_counts")}
        self.assertEqual(counts, {"steps": 1, "heart_rate": 1, "sleep": 0})
        # исправленный файл с тем же номером принимается: неудача не занимает номер
        doc["heart_rate"][2]["samples"][0]["bpm"] = 73
        doc["steps"][1]["count"] = 876
        self.assertEqual(self.run_import(doc)["status"], "imported")
        self.assertEqual(self.count("step_intervals"), 3)

    def test_sqlite_failure_in_the_middle_rolls_everything_back(self):
        conn = db.connect(self.db_path, create=False)
        conn.execute("CREATE TRIGGER trg_fail BEFORE INSERT ON sleep_stages BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        conn.close()
        path = self.put(hs.day_file(sequence=1))
        with self.assertRaises(TrackerError) as ctx:
            health_import.import_file(path, self.db_path)
        self.assertNotIsInstance(ctx.exception, ImportFileError)
        self.assertIn("ничего не сохранено", str(ctx.exception))
        self.assertNothingImported()
        self.assertEqual(self.count("data_sources"), 0)                                   # источник тоже откатился
        failed = self.rows("SELECT * FROM import_batches")
        self.assertEqual([r["status"] for r in failed], ["failed"])
        self.assertIn("simulated failure", failed[0]["error"])
        conn = db.connect(self.db_path, create=False)
        conn.execute("DROP TRIGGER trg_fail")
        conn.close()
        self.assertEqual(health_import.import_file(path, self.db_path)["status"], "imported")     # те же байты теперь проходят
        self.assertEqual(self.count("sleep_stages"), 8)

    def test_failure_after_updates_rolls_back_the_updates_too(self):
        self.run_import(hs.day_file(sequence=1))
        conn = db.connect(self.db_path, create=False)
        conn.execute("CREATE TRIGGER trg_fail BEFORE INSERT ON heart_rate_samples WHEN NEW.bpm = 77 "
                     "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        conn.close()
        before = self.dump(DATA_TABLES)
        doc = hs.export(2, steps=[hs.step("step-1", count=600, modified="2026-10-10T18:00:00Z")], deleted=["step-3"],
                        heart_rate=[hs.heart("hr-new", "2026-10-10T20:00:00Z", "2026-10-10T20:01:00Z", (77,))])
        with self.assertRaises(TrackerError):
            self.run_import(doc)
        self.assertEqual(self.dump(DATA_TABLES), before)                                 # ни обновления, ни удаления, ни вставки
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM import_batches WHERE status = 'ok'")[0]["n"], 1)

    def test_unreadable_files_do_not_touch_the_database(self):
        before = self.dump()
        cases = {"not json": b"{not json", "empty": b"", "binary": b"\xff\xfe\x00\x01", "array": b"[]"}
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(ImportFileError) as ctx:
                health_import.import_file(hs.write(self.incoming, f"{label}.json", data), self.db_path)
            self.assertIsNone(getattr(ctx.exception, "journal_batch_id", None))       # заголовка нет, журнал записать нельзя
        with self.assertRaises(ImportFileError):
            health_import.import_file(self.incoming / "missing.json", self.db_path)
        self.assertEqual(self.dump(), before)

    def test_bad_header_is_not_journaled(self):
        doc = hs.day_file()
        doc["sequence"] = 0
        with self.assertRaises(ImportFileError):
            self.run_import(doc)
        self.assertEqual(self.count("import_batches"), 0)


class DryRunTests(ImportTestCase):
    def test_dry_run_reports_real_counts_and_changes_nothing(self):
        self.run_import(hs.day_file(sequence=1))
        doc = hs.export(2, steps=[hs.step("step-1", count=600, modified="2026-10-10T18:00:00Z"),
                                  hs.step("step-9", "2026-10-10T10:00:00Z", "2026-10-10T10:10:00Z", count=9)],
                        deleted=["hr-1", "ghost"])
        state, snapshot = h.file_state(self.db_path), self.dump()
        dry = self.run_import(doc, dry_run=True)
        self.assertEqual((dry["status"], dry["dry_run"], dry["batch_id"]), ("dry_run", True, None))
        self.assertEqual((h.file_state(self.db_path)[0], self.dump()), (state[0], snapshot))
        real = self.run_import(doc)
        self.assertEqual(real["status"], "imported")
        self.assertEqual(real["counts"], dry["counts"])
        self.assertEqual((dry["counts"]["steps"]["inserted"], dry["counts"]["steps"]["updated"],
                          dry["counts"]["heart_rate"]["deleted"], dry["unmatched_deletions"]), (1, 1, 1, 1))

    def test_dry_run_of_invalid_or_refused_file_writes_no_journal(self):
        bad = hs.day_file(sequence=1)
        bad["steps"][0]["count"] = 0
        with self.assertRaises(ImportFileError):
            self.run_import(bad, dry_run=True)
        self.assertEqual(self.count("import_batches"), 0)
        self.run_import(hs.day_file(sequence=1))
        with self.assertRaises(ImportOrderError):
            self.run_import(hs.day_file(sequence=5), dry_run=True)
        self.assertEqual(self.count("import_batches"), 1)

    def test_dry_run_of_already_imported_file(self):
        doc = hs.day_file()
        self.run_import(doc)
        self.assertEqual(self.run_import(doc, name="again.json", dry_run=True)["status"], "already_imported")


class DatabasePreconditionTests(ImportTestCase):
    def test_missing_database_is_not_created(self):
        missing = self.tmp / "nowhere" / "none.db"
        with self.assertRaises(DatabaseMissingError):
            health_import.import_file(self.put(hs.day_file()), missing)
        self.assertFalse(missing.parent.exists())

    def test_outdated_schema_is_refused_without_changes(self):
        old = self.tmp / "data" / "v2.db"
        h.make_v2_database(old, self.tmp)
        before = h.file_state(old)
        with self.assertRaises(MigrationPendingError) as ctx:
            health_import.import_file(self.put(hs.day_file()), old)
        self.assertIn("migrate_db.py", str(ctx.exception))
        self.assertEqual(h.file_state(old), before)

    def test_newer_schema_is_refused(self):
        conn = db.connect(self.db_path, create=False)
        conn.execute("INSERT INTO schema_migrations (version, name) VALUES (99, '099_future.sql')")
        conn.close()
        before = self.dump()
        with self.assertRaises(MigrationError):
            self.run_import(hs.day_file())
        self.assertEqual(self.dump(), before)

    def test_corrupt_database_is_reported_as_a_clear_error(self):
        broken = self.tmp / "data" / "broken.db"
        broken.write_bytes(b"this is not a database" * 200)
        with self.assertRaises(TrackerError) as ctx:
            health_import.import_file(self.put(hs.day_file()), broken)
        self.assertIn("базу", str(ctx.exception))

    def test_working_database_path_is_never_used_by_these_tests(self):
        self.assertNotEqual(self.db_path.resolve(), db.DEFAULT_DB_PATH.resolve())
        self.assertFalse("FITNESS_DB_PATH" in os.environ, "FITNESS_DB_PATH задан")  # не печатать окружение


class BulkTests(ImportTestCase):
    def month(self, sequence):
        steps, hearts, sleeps = [], [], []
        for day in range(1, 31):
            date = f"2026-09-{day:02d}"
            for slot in range(144):
                a = f"{slot * 10 // 60:02d}:{slot * 10 % 60:02d}"
                b = f"{(slot * 10 + 10) // 60 % 24:02d}:{(slot * 10 + 10) % 60:02d}"
                end_date = date if (slot * 10 + 10) < 1440 else f"2026-10-{1:02d}" if day == 30 else f"2026-09-{day + 1:02d}"
                steps.append(hs.step(f"s-{day}-{slot}", f"{date}T{a}:00Z", f"{end_date}T{b}:00Z", count=1 + slot))
            for minute in range(0, 1440, 5):
                t = f"{date}T{minute // 60:02d}:{minute % 60:02d}:00Z"
                e = f"{date}T{minute // 60:02d}:{minute % 60:02d}:59Z"
                hearts.append(hs.heart(f"h-{day}-{minute}", t, e, samples=[{"time": t, "bpm": 50 + minute % 40}]))
            sleeps.append(hs.sleep(f"z-{day}", f"{date}T01:00:00Z", f"{date}T09:00:00Z",
                                   stages=[{"stage": "light" if i % 2 else "deep", "start": f"{date}T0{1 + i}:00:00Z",
                                            "end": f"{date}T0{1 + i}:59:00Z"} for i in range(8)]))
        return hs.export(sequence, steps, hearts, sleeps)

    def test_thirty_days_import_in_reasonable_time_and_repeat_cleanly(self):
        doc = self.month(1)
        path = self.put(doc)
        start = time.monotonic()
        report = health_import.import_file(path, self.db_path)
        first = time.monotonic() - start
        self.assertEqual({k: v["inserted"] for k, v in report["counts"].items()},
                         {"steps": 4320, "heart_rate": 8640, "sleep": 30})
        self.assertEqual((self.count("step_intervals"), self.count("heart_rate_samples"), self.count("sleep_stages")),
                         (4320, 8640, 240))
        self.assertEqual(health_import.import_file(path, self.db_path)["status"], "already_imported")
        again = self.put(dict(doc, sequence=2))
        start = time.monotonic()
        report = health_import.import_file(again, self.db_path)
        second = time.monotonic() - start
        self.assertEqual({k: v["unchanged"] for k, v in report["counts"].items()},
                         {"steps": 4320, "heart_rate": 8640, "sleep": 30})
        self.assertLess(first, 60, f"первый импорт {first:.1f} с")
        self.assertLess(second, 60, f"повторный импорт {second:.1f} с")


class CommandLineTests(ImportTestCase):
    def cli(self, *args, db_path=None):
        env = {k: v for k, v in os.environ.items() if k not in ("FITNESS_DB_PATH", "PYTHONUTF8")}
        env["PYTHONIOENCODING"] = "utf-8"
        command = [sys.executable, str(IMPORT_CLI), *args]
        if db_path is not False:
            command += ["--db", str(db_path or self.db_path)]
        proc = subprocess.run(command, capture_output=True, cwd=str(self.tmp), env=env, timeout=120)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_import_repeat_and_input_files_are_left_alone(self):
        path = self.put(hs.day_file())
        before_digest, before_listing = self.digest(path), sorted(p.name for p in self.incoming.iterdir())
        code, out, err = self.cli(str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("импортирован", out)
        self.assertIn("добавлено 3", out)
        self.assertEqual(self.count("step_intervals"), 3)
        code, out, err = self.cli(str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("уже был импортирован", out)
        self.assertEqual(self.count("import_batches"), 1)
        self.assertEqual((self.digest(path), sorted(p.name for p in self.incoming.iterdir())), (before_digest, before_listing))

    def test_directory_is_imported_in_sequence_order_not_name_order(self):
        for seq, name in ((3, "a.json"), (1, "c.json"), (2, "b.json")):
            hs.write(self.incoming, name, hs.export(seq, steps=[hs.step(f"s{seq}", f"2026-10-10T0{seq}:00:00Z", f"2026-10-10T0{seq}:10:00Z")]))
        code, out, err = self.cli(str(self.incoming))
        self.assertEqual(code, 0, err)
        order = [(r["id"], r["sequence"]) for r in self.rows("SELECT id, sequence FROM import_batches ORDER BY id")]
        self.assertEqual(order, [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(sorted(p.name for p in self.incoming.iterdir()), ["a.json", "b.json", "c.json"])

    def test_directory_stops_at_the_first_bad_file(self):
        hs.write(self.incoming, "1.json", hs.export(1, steps=[hs.step("a")]))
        bad = hs.export(2, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", count=0)])
        hs.write(self.incoming, "2.json", bad)
        hs.write(self.incoming, "3.json", hs.export(3, steps=[hs.step("c", "2026-10-10T00:20:00Z", "2026-10-10T00:30:00Z")]))
        code, out, err = self.cli(str(self.incoming))
        self.assertEqual(code, 1)
        self.assertIn("2.json", err)
        self.assertIn("steps[0].count", err)
        self.assertEqual(self.rows("SELECT hc_id FROM step_intervals"), [{"hc_id": "a"}])
        self.assertEqual([r["status"] for r in self.rows("SELECT status FROM import_batches ORDER BY id")], ["ok", "failed"])

    def test_gap_is_refused_and_allow_gaps_accepts_it(self):
        self.cli(str(self.put(hs.export(1, steps=[hs.step("a")]))))
        later = self.put(hs.export(3, steps=[hs.step("b", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z")]))
        code, out, err = self.cli(str(later))
        self.assertEqual(code, 1)
        self.assertIn("Пропущены файлы", err)
        code, out, err = self.cli(str(later), "--allow-gaps")
        self.assertEqual(code, 0, err)
        self.assertIn("пропущены файлы", out)

    def test_dry_run_leaves_everything_unchanged(self):
        before = self.dump()
        code, out, err = self.cli(str(self.put(hs.day_file())), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("пробный запуск", out)
        self.assertEqual(self.dump(), before)

    def test_errors_exit_with_code_1_and_explain(self):
        code, out, err = self.cli(str(self.incoming / "missing.json"))
        self.assertEqual((code, "не найден" in err), (1, True))
        code, out, err = self.cli(str(self.put(hs.day_file())), db_path=self.tmp / "nowhere" / "none.db")
        self.assertEqual((code, "не найдена" in err), (1, True))
        self.assertFalse((self.tmp / "nowhere").exists())
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, err = self.cli(str(empty))
        self.assertEqual((code, "ни одного файла" in err), (1, True))
        code, out, err = self.cli(str(hs.write(self.incoming, "junk.json", b"{oops")))
        self.assertEqual((code, "не JSON" in err), (1, True))

    def test_outdated_schema_message(self):
        old = self.tmp / "data" / "old.db"
        h.make_v2_database(old, self.tmp)
        code, out, err = self.cli(str(self.put(hs.day_file())), db_path=old)
        self.assertEqual(code, 1)
        self.assertIn("migrate_db.py", err)


if __name__ == "__main__":
    unittest.main()
