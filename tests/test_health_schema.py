"""Схема данных Health Connect (миграции 002 и 003): применение поверх данных, откат, ограничения, представления."""

import sqlite3
import unittest
from unittest import mock

from fitness import db
from tests import health_helpers as h
from tests.helpers import TempDbTestCase

NEW_TABLES = {"data_sources", "import_batches", "import_batch_counts", "step_intervals", "heart_rate_records",
              "heart_rate_samples", "sleep_sessions", "sleep_stages"}
NEW_VIEWS = {"v_daily_steps", "v_heart_rate_records", "v_daily_heart_rate", "v_daily_sleep", "v_health_sync_status"}


def names(conn, kind):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


class MigrationOverExistingDataTests(h.Pinned003TestCase):
    def setUp(self):
        super().setUp()
        h.make_v1_database(self.db_path, self.tmp)

    def open_raw(self, path=None):
        conn = db.connect(path or self.db_path, create=False)   # без защиты open_database: проверяем саму миграцию
        self.addCleanup(conn.close)
        return conn

    def test_upgrade_keeps_old_data_and_adds_new_schema(self):
        conn = self.open_raw()
        old_tables = set(db._user_tables(conn)) - {"schema_migrations"}
        before = h.snapshot(conn)
        self.assertEqual(db.schema_version(conn), 1)

        self.assertEqual(db.migrate(conn), [2, 3])

        self.assertEqual(db.schema_version(conn), 3)
        self.assertTrue(NEW_TABLES <= names(conn, "table"))
        self.assertTrue(NEW_VIEWS <= names(conn, "view"))
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(h.snapshot(conn, only=old_tables), before)           # старые данные не изменились
        self.assertGreater(sum(len(rows) for rows in before.values()), 5)       # и их действительно было что сравнивать

    def test_apply_twice_is_noop(self):
        conn = self.open_raw()
        db.migrate(conn)
        state = h.fingerprint(conn)
        self.assertEqual(db.migrate(conn), [])
        self.assertEqual(h.fingerprint(conn), state)
        self.assertEqual([r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")], [1, 2, 3])

    def test_failed_migration_leaves_no_trace(self):
        variants = {
            "table_already_exists": "\nCREATE TABLE step_intervals (x);\n",
            "abort_after_all_changes": ("\nCREATE TRIGGER trg_block BEFORE INSERT ON schema_migrations "
                                        "BEGIN SELECT RAISE(ABORT, 'simulated'); END;\n"),
        }
        for label, extra in variants.items():
            with self.subTest(variant=label):
                path = self.tmp / "data" / f"{label}.db"
                h.make_v1_database(path, self.tmp)
                conn = self.open_raw(path)
                fingerprint_before = h.fingerprint(conn)
                data_before = h.snapshot(conn)
                broken = h.migrations_dir(self.tmp, f"broken-{label}", extra_sql_for_002=extra)
                with mock.patch.object(db, "MIGRATIONS_DIR", broken):
                    with self.assertRaises(sqlite3.Error):
                        db.migrate(conn)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(db.schema_version(conn), 1)
                self.assertEqual(h.fingerprint(conn), fingerprint_before)        # ни таблиц, ни представлений
                self.assertEqual(h.snapshot(conn), data_before)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                # после неудачи база по-прежнему обновляется настоящей миграцией
                self.assertEqual(db.migrate(conn), [2, 3])
                conn.close()


class ConstraintTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.source = h.add_source(self.conn)
        self.batch = h.add_batch(self.conn)

    def assertRejected(self, func, *args, **kwargs):
        with self.assertRaises(sqlite3.IntegrityError):
            func(self.conn, *args, **kwargs)

    # ---- источники ----
    def test_sources(self):
        self.assertRejected(h.add_source)                               # тот же source_key
        self.assertRejected(h.add_source, key="")                       # пустой ключ
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO data_sources (source_key, platform, package_name) VALUES ('k', 'fitbit', 'p')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO data_sources (source_key, platform, package_name) VALUES ('k2', 'health_connect', '')")
        # устройство неизвестно (NULL) — допустимо, различие источников держится на source_key
        self.conn.execute("INSERT INTO data_sources (source_key, platform, package_name, device_model) "
                          "VALUES ('k3', 'health_connect', 'p', NULL)")

    # ---- шаги ----
    def test_steps(self):
        step = h.add_step(self.conn, self.source, self.batch)
        self.assertEqual(self.count("step_intervals"), 1)
        self.assertRejected(h.add_step, self.source, self.batch)                              # тот же (source, hc_id)
        other = h.add_source(self.conn, key="other|app")
        h.add_step(self.conn, other, self.batch)                                              # другой источник — можно
        bad = {
            "count 0": dict(hc_id="a", count=0),
            "count 1000001": dict(hc_id="b", count=1000001),
            "end == start": dict(hc_id="c", end="2026-10-10T00:00:00Z"),
            "end < start": dict(hc_id="d", end="2026-10-09T23:59:00Z"),
            "bad timestamp": dict(hc_id="e", start="2026-10-10 00:00:00"),
            "no Z": dict(hc_id="f", start="2026-10-10T00:00:00"),
            "offset 841": dict(hc_id="g", start_offset_min=841),
            "empty hc_id": dict(hc_id=""),
            "short hash": dict(hc_id="i", payload_hash="abc"),
            "bad local_date": dict(hc_id="j", local_date="10.10.2026"),
            "unknown source": dict(hc_id="k", source=9999),
            "unknown batch": dict(hc_id="l", batch=9999),
        }
        for label, kwargs in bad.items():
            with self.subTest(label):
                kwargs = dict(kwargs)
                source = kwargs.pop("source", self.source)
                batch = kwargs.pop("batch", self.batch)
                self.assertRejected(h.add_step, source, batch, **kwargs)
        h.add_step(self.conn, self.source, self.batch, hc_id="ok1", count=1)
        h.add_step(self.conn, self.source, self.batch, hc_id="ok2", count=1000000)
        h.add_step(self.conn, self.source, self.batch, hc_id="ok3", start_offset_min=None, end_offset_min=-840)
        self.assertEqual(self.conn.execute("SELECT revision FROM step_intervals WHERE id = ?", (step,)).fetchone()[0], 1)

    # ---- пульс ----
    def test_heart_rate(self):
        record = h.add_hr(self.conn, self.source, self.batch, bpms=(66, 69))
        self.assertEqual(self.count("heart_rate_samples"), 2)
        h.add_hr(self.conn, self.source, self.batch, hc_id="instant", end="2026-10-10T19:10:00Z", bpms=())  # start == end допустим
        self.assertRejected(h.add_hr, self.source, self.batch, hc_id="back", end="2026-10-10T19:00:00Z")
        for bpm in (0, 301, -5):
            with self.subTest(bpm=bpm), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, ?)",
                                  (record, h.canon("2026-10-10T19:30:00Z"), bpm))
        for bpm in (1, 300):
            self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, ?)",
                              (record, h.canon(f"2026-10-10T19:3{bpm % 7}:00Z"), bpm))
        with self.assertRaises(sqlite3.IntegrityError):                                  # тот же момент дважды
            self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, 70)",
                              (record, h.canon("2026-10-10T19:10:00Z")))
        with self.assertRaises(sqlite3.IntegrityError):                                  # запись не существует
            self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (999, ?, 70)",
                              (h.canon("2026-10-10T19:10:00Z"),))
        self.conn.execute("DELETE FROM heart_rate_records WHERE id = ?", (record,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM heart_rate_samples WHERE record_id = ?",
                                           (record,)).fetchone()[0], 0)                  # каскад

    # ---- сон ----
    def test_sleep(self):
        session = h.add_sleep(self.conn, self.source, self.batch, stages=h.SCREEN_STAGES)
        self.assertEqual(self.count("sleep_stages"), 8)
        stage_names = ("unknown", "awake", "sleeping", "out_of_bed", "light", "deep", "rem", "awake_in_bed")
        for i, stage in enumerate(stage_names):
            self.conn.execute("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                              (session, stage, h.canon(f"2026-10-10T12:{i:02d}:00Z"), h.canon(f"2026-10-10T12:{i:02d}:30Z")))
        for label, sql_args in {
            "unknown stage name": ("rapid_eye", "2026-10-10T12:00:00Z", "2026-10-10T12:01:00Z"),
            "end == start": ("light", "2026-10-10T12:00:00Z", "2026-10-10T12:00:00Z"),
            "end < start": ("light", "2026-10-10T12:05:00Z", "2026-10-10T12:00:00Z"),
            "same start twice": ("light", "2026-10-10T07:05:00Z", "2026-10-10T07:06:00Z"),
        }.items():
            with self.subTest(label), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                                  (session, sql_args[0], h.canon(sql_args[1]), h.canon(sql_args[2])))
        h.add_sleep(self.conn, self.source, self.batch, hc_id="zero-length", end="2026-10-10T07:05:00Z")
        self.assertRejected(h.add_sleep, self.source, self.batch, hc_id="negative", end="2026-10-10T06:00:00Z")
        self.assertRejected(h.add_sleep, self.source, self.batch)                           # тот же hc_id
        self.conn.execute("DELETE FROM sleep_sessions WHERE id = ?", (session,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sleep_stages WHERE session_id = ?",
                                           (session,)).fetchone()[0], 0)                      # каскад

    # ---- журнал импортов ----
    def test_import_journal(self):
        self.assertRejected(h.add_batch, sha="a" * 64, seq=2)                                  # тот же sha256, оба ok
        self.assertRejected(h.add_batch, sha="b" * 64, seq=1)                                  # тот же (phone, sequence), оба ok
        h.add_batch(self.conn, sha="b" * 64, seq=2, phone="phone-2")                           # другой телефон — можно
        h.add_batch(self.conn, sha="c" * 64, seq=3, status="failed")                           # неудача записывается
        h.add_batch(self.conn, sha="c" * 64, seq=3, status="failed")                           # и повторная тоже
        h.add_batch(self.conn, sha="c" * 64, seq=3, status="ok")                               # после неудач успешный импорт
        self.assertRejected(h.add_batch, sha="c" * 64, seq=4, status="ok")                     # но второй успешный — нет
        for label, kwargs in {
            "uppercase hash": dict(sha="A" * 64, seq=10),
            "short hash": dict(sha="a" * 63, seq=11),
            "non-hex hash": dict(sha="g" * 64, seq=12),
            "bad status": dict(sha="d" * 64, seq=13, status="maybe"),
            "bad exported_at": dict(sha="e" * 64, seq=14, exported="yesterday"),
            "sequence 0": dict(sha="f" * 64, seq=0),
        }.items():
            with self.subTest(label):
                self.assertRejected(h.add_batch, **kwargs)
        with self.assertRaises(sqlite3.IntegrityError):                                         # failed без текста ошибки
            self.conn.execute("INSERT INTO import_batches (file_name, sha256, format_version, phone_id, sequence, "
                              "exported_at, status) VALUES ('x', ?, 1, 'p', 20, '2026-10-10T17:00:00Z', 'failed')",
                              ("1" * 64,))
        with self.assertRaises(sqlite3.IntegrityError):                                         # ok с текстом ошибки
            self.conn.execute("INSERT INTO import_batches (file_name, sha256, format_version, phone_id, sequence, "
                              "exported_at, status, error) VALUES ('x', ?, 1, 'p', 21, '2026-10-10T17:00:00Z', "
                              "'ok', 'oops')", ("2" * 64,))

    def test_batch_counts_and_foreign_keys(self):
        self.conn.execute("INSERT INTO import_batch_counts (batch_id, record_type, inserted) VALUES (?, 'steps', 5)",
                          (self.batch,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO import_batch_counts (batch_id, record_type) VALUES (?, 'steps')",
                              (self.batch,))                                                   # повтор типа в батче
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO import_batch_counts (batch_id, record_type) VALUES (?, 'weight')",
                              (self.batch,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO import_batch_counts (batch_id, record_type, deleted) VALUES (?, 'sleep', -1)",
                              (self.batch,))
        h.add_step(self.conn, self.source, self.batch)
        with self.assertRaises(sqlite3.IntegrityError):                                         # батч с данными не удалить
            self.conn.execute("DELETE FROM import_batches WHERE id = ?", (self.batch,))
        with self.assertRaises(sqlite3.IntegrityError):                                         # источник с данными не удалить
            self.conn.execute("DELETE FROM data_sources WHERE id = ?", (self.source,))
        empty = h.add_batch(self.conn, sha="9" * 64, seq=9)
        self.conn.execute("INSERT INTO import_batch_counts (batch_id, record_type) VALUES (?, 'sleep')", (empty,))
        self.conn.execute("DELETE FROM import_batches WHERE id = ?", (empty,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM import_batch_counts WHERE batch_id = ?",
                                           (empty,)).fetchone()[0], 0)                          # счётчики уходят вместе с батчем

    def test_updated_at_triggers(self):
        ids = {
            "step_intervals": h.add_step(self.conn, self.source, self.batch),
            "heart_rate_records": h.add_hr(self.conn, self.source, self.batch),
            "sleep_sessions": h.add_sleep(self.conn, self.source, self.batch),
        }
        for table, row_id in ids.items():
            with self.subTest(table=table):
                self.conn.execute(f"UPDATE {table} SET created_at = '2000-01-01T00:00:00Z', "
                                  f"updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (row_id,))
                self.conn.execute(f"UPDATE {table} SET revision = 2 WHERE id = ?", (row_id,))
                row = self.conn.execute(f"SELECT created_at, updated_at FROM {table} WHERE id = ?", (row_id,)).fetchone()
                self.assertEqual(row["created_at"], "2000-01-01T00:00:00Z")
                self.assertNotEqual(row["updated_at"], "2000-01-01T00:00:00Z")


class ViewTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.source = h.add_source(self.conn)
        self.batch = h.add_batch(self.conn)

    def one(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()

    def test_daily_steps(self):
        s, b = self.source, self.batch
        h.add_step(self.conn, s, b, hc_id="1", start="2026-10-10T00:00:00Z", end="2026-10-10T00:10:00Z", count=511)
        h.add_step(self.conn, s, b, hc_id="2", start="2026-10-10T00:10:00Z", end="2026-10-10T00:20:00Z", count=876)
        h.add_step(self.conn, s, b, hc_id="3", start="2026-10-10T16:30:00Z", end="2026-10-10T16:40:00Z", count=6)
        h.add_step(self.conn, s, b, hc_id="gone", start="2026-10-10T01:00:00Z", end="2026-10-10T01:10:00Z",
                   count=417, deleted_at="2026-10-10T18:00:00Z")
        h.add_step(self.conn, s, b, hc_id="4", start="2026-10-09T22:00:00Z", end="2026-10-09T22:10:00Z", count=40,
                   local_date="2026-10-09")
        other = h.add_source(self.conn, key="other", package="com.other")
        h.add_step(self.conn, other, b, hc_id="5", start="2026-10-10T05:00:00Z", end="2026-10-10T05:10:00Z", count=100)

        rows = {(r["local_date"], r["source_id"]): r for r in self.conn.execute("SELECT * FROM v_daily_steps")}
        main = rows[("2026-10-10", s)]
        self.assertEqual((main["steps"], main["intervals"]), (511 + 876 + 6, 3))               # удалённый не учтён
        self.assertEqual(main["last_end_utc"], h.canon("2026-10-10T16:40:00Z"))
        self.assertEqual(rows[("2026-10-09", s)]["steps"], 40)
        self.assertEqual(rows[("2026-10-10", other)]["steps"], 100)                               # источники раздельно
        self.assertEqual(len(rows), 3)

        self.conn.execute("UPDATE step_intervals SET deleted_at = NULL WHERE hc_id = 'gone'")   # возврат записи
        self.assertEqual(self.one("SELECT steps FROM v_daily_steps WHERE local_date = '2026-10-10' AND source_id = ?",
                                  s)["steps"], 511 + 876 + 6 + 417)

    def test_heart_rate_views(self):
        s, b = self.source, self.batch
        h.add_hr(self.conn, s, b, hc_id="a", bpms=(66,))
        h.add_hr(self.conn, s, b, hc_id="b", start="2026-10-10T19:05:00Z", end="2026-10-10T19:06:00Z", bpms=(69,))
        h.add_hr(self.conn, s, b, hc_id="c", start="2026-10-10T19:00:00Z", end="2026-10-10T19:01:00Z", bpms=(73,))
        h.add_hr(self.conn, s, b, hc_id="d", start="2026-10-10T19:20:00Z", end="2026-10-10T19:20:00Z", bpms=())
        h.add_hr(self.conn, s, b, hc_id="gone", bpms=(250,), deleted_at="2026-10-10T20:00:00Z",
                 start="2026-10-10T18:00:00Z", end="2026-10-10T18:01:00Z")
        day = self.one("SELECT * FROM v_daily_heart_rate WHERE local_date = '2026-10-10'")
        self.assertEqual((day["min_bpm"], day["max_bpm"], day["samples"]), (66, 73, 3))
        self.assertAlmostEqual(day["avg_bpm"], 69.3)
        records = {r["hc_id"]: r for r in self.conn.execute("SELECT * FROM v_heart_rate_records")}
        self.assertEqual(records["a"]["duration_sec"], 60)
        self.assertEqual(records["d"]["duration_sec"], 0)
        self.assertEqual((records["a"]["samples"], records["d"]["samples"]), (1, 0))
        self.assertNotIn("gone", records)

    def test_sleep_views_match_synthetic_stages(self):
        session = h.add_sleep(self.conn, self.source, self.batch, stages=h.SCREEN_STAGES)
        row = self.one("SELECT * FROM v_daily_sleep")
        self.assertEqual(row["local_date"], "2026-10-10")
        self.assertEqual(row["sessions"], 1)
        self.assertEqual(row["session_min"], 495)                    # 07:05–15:20
        self.assertEqual(row["light_min"], 25 + 20 + 28 + 32)
        self.assertEqual(row["deep_min"], 20)
        self.assertEqual(row["awake_min"], 2 + 3 + 5)
        self.assertEqual(row["asleep_min"], 105 + 20)
        self.assertIsNone(row["rem_min"])                            # стадии REM не было — неизвестно, а не 0
        self.conn.execute("UPDATE sleep_sessions SET deleted_at = '2026-10-10T20:00:00Z' WHERE id = ?", (session,))
        self.assertIsNone(self.one("SELECT * FROM v_daily_sleep"))

    def test_sleep_without_stages_has_unknown_minutes(self):
        h.add_sleep(self.conn, self.source, self.batch)
        row = self.one("SELECT * FROM v_daily_sleep")
        self.assertEqual(row["session_min"], 495)
        self.assertIsNone(row["asleep_min"])
        self.assertIsNone(row["awake_min"])

    def test_sync_status(self):
        self.conn.execute("DELETE FROM import_batches")          # убираем батч из setUp: нужна совсем пустая база
        empty = {r["record_type"]: r for r in self.conn.execute("SELECT * FROM v_health_sync_status")}
        self.assertEqual(set(empty), {"steps", "heart_rate", "sleep"})
        for row in empty.values():
            self.assertEqual((row["records"], row["last_record_end_utc"], row["last_ok_import_at"]), (0, None, None))

        ok1 = h.add_batch(self.conn, sha="1" * 64, seq=1, imported="2026-10-10T17:30:00Z", exported="2026-10-10T17:20:00Z")
        ok2 = h.add_batch(self.conn, sha="2" * 64, seq=2, imported="2026-10-10T18:30:00Z", exported="2026-10-10T18:20:00Z")
        h.add_batch(self.conn, sha="3" * 64, seq=3, status="failed", imported="2026-10-10T19:30:00Z",
                    exported="2026-10-10T19:20:00Z")
        h.add_step(self.conn, self.source, ok1, hc_id="1", end="2026-10-10T16:40:00Z", start="2026-10-10T16:30:00Z")
        h.add_step(self.conn, self.source, ok2, hc_id="2", end="2026-10-10T16:50:00Z", start="2026-10-10T16:40:00Z")
        h.add_step(self.conn, self.source, ok2, hc_id="late", start="2026-10-10T20:00:00Z", end="2026-10-10T20:10:00Z",
                   deleted_at="2026-10-10T21:00:00Z")
        h.add_hr(self.conn, self.source, ok2)
        rows = {r["record_type"]: r for r in self.conn.execute("SELECT * FROM v_health_sync_status")}
        self.assertEqual(rows["steps"]["last_record_end_utc"], h.canon("2026-10-10T16:50:00Z"))           # удалённая не считается
        self.assertEqual(rows["steps"]["records"], 2)
        self.assertEqual(rows["heart_rate"]["records"], 1)
        self.assertEqual(rows["sleep"]["last_record_end_utc"], None)
        for row in rows.values():                                                                # неудачный импорт не учитывается
            self.assertEqual(row["last_ok_import_at"], "2026-10-10T18:30:00Z")
            self.assertEqual(row["last_exported_at"], "2026-10-10T18:20:00Z")


if __name__ == "__main__":
    unittest.main()
