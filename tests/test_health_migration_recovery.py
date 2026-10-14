"""Миграция 003 и восстановление: обрыв процесса, копии баз v1 и v2, необычные настройки соединения, посторонние объекты,
большой объём. Работает только с временными базами.
"""

import hashlib
import sqlite3
import subprocess
import sys
import time
import unittest

from fitness import db
from fitness.errors import MigrationError
from tests import health_helpers as h
from tests.test_health_migration_003 import ALL_TABLES, REBUILT, table_rows

CHILD = """
import os, sqlite3, sys
sys.path.insert(0, {clone!r})
from fitness import db
conn = db.connect({path!r}, create=False)
sql = open({migration!r}, encoding="utf-8").read()
conn.execute("BEGIN IMMEDIATE")
buffer = ""
for line in sql.splitlines(True):
    buffer += line
    if sqlite3.complete_statement(buffer):
        conn.execute(buffer)
        if {marker!r} in buffer:
            os._exit(3)          # обрыв процесса: транзакция не завершена
        buffer = ""
"""


class InterruptionAndCopyTests(h.Pinned003TestCase):
    def interrupt(self, path, migration, marker):
        script = self.tmp / "interrupt.py"
        script.write_text(CHILD.format(clone=str(h.PROJECT_DIR), path=str(path),
                                       migration=str(h.REAL_MIGRATIONS / migration), marker=marker), encoding="utf-8")
        proc = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 3, proc.stderr.decode("utf-8", "replace"))

    def test_an_interrupted_003_leaves_version_2_and_the_migration_then_completes(self):
        path = self.tmp / "data" / "v2.db"
        h.make_v2_database(path, self.tmp, health_rows=True)
        conn = db.connect(path, create=False)
        before = {t: table_rows(conn, t) for t in ALL_TABLES}
        fingerprint = h.fingerprint(conn)
        conn.close()
        self.interrupt(path, "003_health_time_precision.sql", "DROP TABLE sleep_stages")        # старые таблицы уже частично удалены
        conn = db.connect(path, create=False)
        try:
            self.assertEqual(db.schema_version(conn), 2)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(h.fingerprint(conn), fingerprint)
            self.assertEqual({t: table_rows(conn, t) for t in ALL_TABLES}, before)
            leftovers = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%\\_new' ESCAPE '\\'")]
            self.assertEqual(leftovers, [])
        finally:
            conn.close()
        report = db.upgrade_database(path)
        self.assertEqual((report["from_version"], report["to_version"]), (2, 3))

    def test_an_interruption_during_002_leaves_version_1_and_both_migrations_then_complete(self):
        path = self.tmp / "data" / "v1.db"
        h.make_v1_database(path, self.tmp)
        conn = db.connect(path, create=False)
        before = {t: table_rows(conn, t) for t in ("measurement_sessions", "workouts", "meals", "alcohol_entries", "notes")}
        conn.close()
        self.interrupt(path, "002_health_connect.sql", "CREATE TABLE heart_rate_records")
        conn = db.connect(path, create=False)
        try:
            self.assertEqual(db.schema_version(conn), 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual({t: table_rows(conn, t) for t in before}, before)
            self.assertNotIn("step_intervals", {r[0] for r in conn.execute("SELECT name FROM sqlite_master")})
        finally:
            conn.close()
        self.assertEqual(db.upgrade_database(path)["applied"], [2, 3])

    def test_the_migration_works_on_backup_copies_of_v1_and_v2_and_leaves_the_originals_alone(self):
        makers = {"v1": lambda p: h.make_v1_database(p, self.tmp), "v2": lambda p: h.make_v2_database(p, self.tmp, health_rows=True)}
        for label, maker in makers.items():
            with self.subTest(label):
                original = self.tmp / "data" / f"{label}-original.db"
                maker(original)
                digest = hashlib.sha256(original.read_bytes()).hexdigest()
                copy = self.tmp / "data" / f"{label}-copy.db"
                source = h.raw_readonly(original)
                target = sqlite3.connect(copy)
                source.backup(target)
                target.close()
                source.close()
                report = db.upgrade_database(copy)
                self.assertEqual((report["to_version"], report["integrity"], report["foreign_key_violations"]), (3, "ok", 0))
                self.assertEqual(h.raw_version(original), int(label[1]))
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), digest)
                self.assertEqual(db.upgrade_database(copy)["pending"], [])                         # повторный запуск

    def test_foreign_key_references_survive_unusual_connection_settings(self):
        for label, pragma in (("legacy_alter_table=ON", "PRAGMA legacy_alter_table = ON"), ("foreign_keys=OFF", "PRAGMA foreign_keys = OFF")):
            with self.subTest(label):
                path = self.tmp / "data" / f"unusual-{label[:6]}.db"
                h.make_v2_database(path, self.tmp, health_rows=True)
                conn = db.connect(path, create=False)
                try:
                    conn.execute(pragma)
                    db.migrate(conn)
                    conn.execute("PRAGMA foreign_keys = ON")
                    targets = {t: {r["table"] for r in conn.execute(f"PRAGMA foreign_key_list({t})")}
                               for t in ("heart_rate_samples", "sleep_stages")}
                    self.assertEqual(targets, {"heart_rate_samples": {"heart_rate_records"}, "sleep_stages": {"sleep_sessions"}})
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                finally:
                    conn.close()

    def test_a_user_view_on_a_rebuilt_table_stops_the_migration_cleanly(self):
        path = self.tmp / "data" / "userview.db"
        h.make_v2_database(path, self.tmp, health_rows=True)
        conn = db.connect(path, create=False)
        conn.execute("CREATE VIEW my_steps AS SELECT hc_id, count FROM step_intervals")
        fingerprint, data = h.fingerprint(conn), {t: table_rows(conn, t) for t in ALL_TABLES}
        conn.close()
        with self.assertRaises(MigrationError) as ctx:
            db.upgrade_database(path)
        self.assertIn("before-migration-003", str(ctx.exception))                                  # путь к копии назван
        conn = db.connect(path, create=False)
        try:
            self.assertEqual((db.schema_version(conn), h.fingerprint(conn)), (2, fingerprint))
            self.assertEqual({t: table_rows(conn, t) for t in ALL_TABLES}, data)
        finally:
            conn.close()
        self.assertEqual(len(list((path.parent / "backups").glob("*before-migration-003*.db"))), 1)

    def test_a_month_of_data_migrates_completely_and_quickly(self):
        path = self.tmp / "data" / "month.db"
        h.make_v2_database(path, self.tmp, populate=False)
        conn = db.connect(path, create=False)
        source, batch = h.add_source(conn), h.add_batch(conn)
        steps, hearts, sleeps = [], [], []
        for day in range(1, 31):
            date = f"2026-09-{day:02d}"
            for slot in range(143):
                a, b = slot * 10, slot * 10 + 10
                steps.append((source, f"s{day}-{slot}", f"{date}T{a // 60:02d}:{a % 60:02d}:00Z",
                              f"{date}T{b // 60:02d}:{b % 60:02d}:00Z", date, 1 + slot, h.TS, h.HASH, batch))
            for minute in range(0, 1440, 5):
                t = f"{date}T{minute // 60:02d}:{minute % 60:02d}:00Z"
                hearts.append((source, f"h{day}-{minute}", t, t, date, h.TS, h.HASH, batch))
            sleeps.append((source, f"z{day}", f"{date}T01:00:00Z", f"{date}T09:00:00Z", date, h.TS, h.HASH, batch))
        conn.execute("BEGIN")
        conn.executemany("INSERT INTO step_intervals (source_id, hc_id, start_utc, end_utc, local_date, count, hc_last_modified,"
                         " payload_hash, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", steps)
        conn.executemany("INSERT INTO heart_rate_records (source_id, hc_id, start_utc, end_utc, local_date, hc_last_modified,"
                         " payload_hash, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", hearts)
        conn.executemany("INSERT INTO sleep_sessions (source_id, hc_id, start_utc, end_utc, local_date, hc_last_modified,"
                         " payload_hash, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", sleeps)
        conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) "
                     "SELECT id, start_utc, 60 + id % 40 FROM heart_rate_records")
        conn.execute("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) "
                     "SELECT id, 'light', start_utc, end_utc FROM sleep_sessions")
        conn.execute("COMMIT")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in REBUILT}
        conn.close()
        started = time.monotonic()
        report = db.upgrade_database(path)
        elapsed = time.monotonic() - started
        conn = db.connect(path, create=False)
        try:
            self.assertEqual({t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in REBUILT}, counts)
            self.assertEqual(counts["step_intervals"], 4290)
            plain = conn.execute("SELECT COUNT(*) FROM heart_rate_samples WHERE time_utc NOT GLOB '*.000000000Z'").fetchone()[0]
            self.assertEqual(plain, 0)
        finally:
            conn.close()
        self.assertEqual((report["integrity"], report["foreign_key_violations"]), ("ok", 0))
        self.assertLess(elapsed, 60)


if __name__ == "__main__":
    unittest.main()
