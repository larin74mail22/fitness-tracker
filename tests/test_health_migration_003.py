"""Миграция 003: время Health Connect с точностью до наносекунд (фиксированные 9 цифр доли секунды).

Проверяется на временных базах версий 1 и 2: сохранность данных, ограничений, индексов, триггеров и
представлений, резервная копия, целостность, внешние ключи, откат при ошибке и повторный запуск.
"""

import hashlib
import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from fitness import db
from tests import health_helpers as h

REBUILT = ["step_intervals", "heart_rate_records", "heart_rate_samples", "sleep_sessions", "sleep_stages"]
TIME_COLUMNS = {"start_utc", "end_utc", "hc_last_modified", "time_utc"}
SECONDS_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"
NANOS_GLOB = ("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
              "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z")
MIGRATE = h.PROJECT_DIR / "migrate_db.py"


def schema_objects(conn):
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}


def normalize_sql(sql):
    """Сравнение определений без учёта пробелов, комментариев и кавычек вокруг имён (SQLite добавляет их при RENAME)."""
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql).replace('"', "")).strip()


def table_rows(conn, table):
    order = "1, 2" if table == "import_batch_counts" else "1"
    return [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]


def expected_after(conn, table, rows):
    """Строки v2 -> ожидаемые строки v3: только четыре столбца времени получают суффикс '.000000000'."""
    names = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
    result = []
    for row in rows:
        row = list(row)
        for i, name in enumerate(names):
            if name in TIME_COLUMNS:
                row[i] = row[i][:19] + ".000000000Z"
        result.append(tuple(row))
    return result


ALL_TABLES = REBUILT + ["data_sources", "import_batches", "import_batch_counts", "measurement_sessions", "workouts",
                        "meals", "alcohol_entries", "notes", "note_fields", "exercises", "measurement_types"]


class MigrationFromV2Tests(h.Pinned003TestCase):
    def setUp(self):
        super().setUp()
        h.make_v2_database(self.db_path, self.tmp, health_rows=True)
        self.conn = db.connect(self.db_path, create=False)    # без защиты open_database: проверяем саму миграцию
        self.addCleanup(self.conn.close)

    def test_data_is_preserved_and_times_are_normalized(self):
        before = {t: table_rows(self.conn, t) for t in ALL_TABLES}
        self.assertTrue(all(before[t] for t in ["step_intervals", "heart_rate_samples", "sleep_stages", "import_batches"]))

        self.assertEqual(db.migrate(self.conn), [3])

        self.assertEqual(db.schema_version(self.conn), 3)
        self.assertEqual(self.conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        for table in ALL_TABLES:
            with self.subTest(table=table):
                self.assertEqual(table_rows(self.conn, table), expected_after(self.conn, table, before[table]))

    def test_child_rows_survive_the_rebuild(self):
        samples = self.conn.execute("SELECT COUNT(*) FROM heart_rate_samples").fetchone()[0]
        stages = self.conn.execute("SELECT COUNT(*) FROM sleep_stages").fetchone()[0]
        db.migrate(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM heart_rate_samples").fetchone()[0], samples)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sleep_stages").fetchone()[0], stages)
        self.assertGreater(samples + stages, 5)

    def test_every_constraint_index_trigger_and_view_is_kept(self):
        old = schema_objects(self.conn)
        db.migrate(self.conn)
        new = schema_objects(self.conn)
        self.assertEqual(set(old), set(new))                       # те же таблицы, индексы, триггеры, представления
        self.assertEqual({k[0] for k in new}, {"table", "index", "trigger", "view"})
        different = []
        for key, old_sql in old.items():
            expected = normalize_sql(old_sql)
            if key[0] == "table" and key[1] in REBUILT:
                # единственное допустимое отличие: проверка формата времени в четырёх столбцах
                expected = re.sub(r"((?:start_utc|end_utc|hc_last_modified|time_utc) GLOB ')" + re.escape(SECONDS_GLOB),
                                  lambda m: m.group(1) + NANOS_GLOB, expected)
            if expected != normalize_sql(new[key]):
                different.append(key)
        self.assertEqual(different, [])
        counts = {kind: sum(1 for k in new if k[0] == kind) for kind in ("table", "index", "trigger", "view")}
        self.assertEqual(counts["view"], 5)
        self.assertGreaterEqual(counts["trigger"], 9)

    def test_deleted_at_and_service_columns_keep_second_precision(self):
        db.migrate(self.conn)
        sql = schema_objects(self.conn)[("table", "step_intervals")]
        self.assertIn(f"deleted_at GLOB '{SECONDS_GLOB}'", normalize_sql(sql))
        self.assertNotIn(f"deleted_at GLOB '{NANOS_GLOB}'", normalize_sql(sql))

    def test_rerun_is_noop(self):
        db.migrate(self.conn)
        state = (h.fingerprint(self.conn), {t: table_rows(self.conn, t) for t in ALL_TABLES})
        self.assertEqual(db.migrate(self.conn), [])
        self.assertEqual((h.fingerprint(self.conn), {t: table_rows(self.conn, t) for t in ALL_TABLES}), state)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT version FROM schema_migrations ORDER BY version")],
                         [1, 2, 3])

    def test_foreign_keys_still_point_at_final_table_names(self):
        db.migrate(self.conn)
        targets = {t: {r["table"] for r in self.conn.execute(f"PRAGMA foreign_key_list({t})")} for t in REBUILT}
        self.assertEqual(targets["heart_rate_samples"], {"heart_rate_records"})
        self.assertEqual(targets["sleep_stages"], {"sleep_sessions"})
        self.assertEqual(targets["step_intervals"], {"data_sources", "import_batches"})
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        self.assertFalse([n for n in names if n.endswith("_new")])      # временных таблиц не осталось
        # внешние ключи действуют и каскады работают
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (9999, ?, 60)",
                              (h.canon("2026-10-10T19:30:00Z"),))
        record = self.conn.execute("SELECT id FROM heart_rate_records WHERE hc_id = 'h1'").fetchone()[0]
        self.conn.execute("DELETE FROM heart_rate_records WHERE id = ?", (record,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM heart_rate_samples WHERE record_id = ?",
                                           (record,)).fetchone()[0], 0)
        session = self.conn.execute("SELECT id FROM sleep_sessions WHERE hc_id = 'z1'").fetchone()[0]
        self.conn.execute("DELETE FROM sleep_sessions WHERE id = ?", (session,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sleep_stages WHERE session_id = ?",
                                           (session,)).fetchone()[0], 0)

    def test_triggers_and_views_work_on_migrated_data(self):
        db.migrate(self.conn)
        for table in ("step_intervals", "heart_rate_records", "sleep_sessions"):
            self.conn.execute(f"UPDATE {table} SET updated_at = '2000-01-01T00:00:00Z' WHERE id = 1")
            self.conn.execute(f"UPDATE {table} SET revision = revision + 1 WHERE id = 1")
            self.assertNotEqual(self.conn.execute(f"SELECT updated_at FROM {table} WHERE id = 1").fetchone()[0],
                                "2000-01-01T00:00:00Z", table)
        steps = self.conn.execute("SELECT * FROM v_daily_steps").fetchall()
        self.assertEqual([(r["steps"], r["intervals"]) for r in steps], [(420, 1)])          # удалённое окно скрыто
        self.assertEqual(steps[0]["last_end_utc"], "2026-10-10T00:10:00.000000000Z")
        hr = self.conn.execute("SELECT * FROM v_daily_heart_rate").fetchone()
        self.assertEqual((hr["min_bpm"], hr["max_bpm"], hr["samples"]), (61, 70, 3))
        sleep = self.conn.execute("SELECT * FROM v_daily_sleep").fetchone()
        self.assertEqual((sleep["sessions"], sleep["session_min"]), (1, 498))                 # удалённая сессия скрыта
        self.assertEqual(sleep["deep_min"], 20)
        status = {r["record_type"]: r for r in self.conn.execute("SELECT * FROM v_health_sync_status")}
        self.assertEqual(status["steps"]["last_record_end_utc"], "2026-10-10T00:10:00.000000000Z")
        self.assertEqual(status["steps"]["last_ok_import_at"], "2026-10-10T18:30:00Z")
        self.assertEqual(len(self.conn.execute("SELECT * FROM v_heart_rate_records").fetchall()), 2)


class TimeFormatTests(h.IsolatedTestCase):
    """Новая форма времени на базе версии 3."""

    def setUp(self):
        super().setUp()
        self.conn = db.open_database(self.db_path)
        self.addCleanup(self.conn.close)
        self.source = h.add_source(self.conn)
        self.batch = h.add_batch(self.conn)

    def step(self, hc, start, end, **kw):
        row = {"source_id": self.source, "hc_id": hc, "start_utc": start, "end_utc": end, "local_date": "2026-10-10",
               "count": 5, "hc_last_modified": h.canon(h.TS), "payload_hash": h.HASH, "batch_id": self.batch}
        row.update(kw)
        return h._insert(self.conn, "step_intervals", row)

    def test_only_nine_digit_fraction_is_accepted(self):
        self.step("ok", "2026-10-10T00:00:00.123456789Z", "2026-10-10T00:10:00.000000001Z")
        bad = ["2026-10-10T00:00:00Z", "2026-10-10T00:00:00.1Z", "2026-10-10T00:00:00.123Z",
               "2026-10-10T00:00:00.12345678Z", "2026-10-10T00:00:00.1234567890Z", "2026-10-10T00:00:00.000000000"]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.step(f"bad-{value}", value, "2026-10-10T01:00:00.000000000Z")
        with self.assertRaises(sqlite3.IntegrityError):
            self.step("bad-end", "2026-10-10T00:00:00.000000000Z", "2026-10-10T01:00:00Z")
        with self.assertRaises(sqlite3.IntegrityError):
            self.step("bad-modified", "2026-10-10T00:00:00.000000000Z", "2026-10-10T01:00:00.000000000Z",
                      hc_last_modified="2026-10-10T00:00:00Z")

    def test_every_rebuilt_table_enforces_the_new_format(self):
        record = h.add_hr(self.conn, self.source, self.batch)
        session = h.add_sleep(self.conn, self.source, self.batch)
        for table, sql, args in [
            ("heart_rate_samples", "INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, 60)",
             (record, "2026-10-10T19:30:00Z")),
            ("sleep_stages", "INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, 'light', ?, "
             "'2026-10-10T12:30:00.000000000Z')", (session, "2026-10-10T12:00:00Z")),
            ("sleep_sessions", "INSERT INTO sleep_sessions (source_id, hc_id, start_utc, end_utc, local_date, "
             "hc_last_modified, payload_hash, batch_id) VALUES (?, 'x', ?, '2026-10-10T12:30:00.000000000Z', '2026-10-10', "
             "'2026-10-10T12:30:00.000000000Z', ?, ?)", (self.source, "2026-10-10T12:00:00Z", h.HASH, self.batch)),
            ("heart_rate_records", "INSERT INTO heart_rate_records (source_id, hc_id, start_utc, end_utc, local_date, "
             "hc_last_modified, payload_hash, batch_id) VALUES (?, 'y', '2026-10-10T12:00:00.000000000Z', ?, '2026-10-10', "
             "'2026-10-10T12:30:00.000000000Z', ?, ?)", (self.source, "2026-10-10T12:30:00Z", h.HASH, self.batch)),
        ]:
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, args)
        # собственные метки трекера по-прежнему с точностью до секунды
        self.step("row", "2026-10-10T00:00:00.000000000Z", "2026-10-10T00:10:00.000000000Z")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE step_intervals SET deleted_at = '2026-10-10T20:00:00.5Z'")

    def test_string_order_equals_time_order(self):
        times = ["2026-10-10T00:00:00.000000000Z", "2026-10-10T00:00:00.000000001Z", "2026-10-10T00:00:00.100000000Z",
                 "2026-10-10T00:00:00.500000000Z", "2026-10-10T00:00:01.000000000Z", "2026-10-10T00:00:01.000000010Z"]
        self.assertEqual(sorted(times), times)
        self.step("a", times[0], times[1])                       # разница в одну наносекунду допустима
        self.step("b", times[2], times[3], start_offset_min=0)
        with self.assertRaises(sqlite3.IntegrityError):           # конец раньше начала на долю секунды
            self.step("c", times[3], times[2])
        with self.assertRaises(sqlite3.IntegrityError):           # равные
            self.step("d", times[3], times[3])
        last = self.conn.execute("SELECT last_end_utc FROM v_daily_steps").fetchone()[0]
        self.assertEqual(last, times[3])                           # MAX(end_utc) выбирает хронологически последнее

    def test_samples_differing_by_a_nanosecond_are_distinct(self):
        record = h.add_hr(self.conn, self.source, self.batch, bpms=())
        for t in ("2026-10-10T19:10:00.000000000Z", "2026-10-10T19:10:00.000000001Z"):
            self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, 60)", (record, t))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, 61)",
                              (record, "2026-10-10T19:10:00.000000001Z"))

    def test_views_accept_fractions(self):
        h.add_hr(self.conn, self.source, self.batch, start="2026-10-10T19:10:00.250000000Z",
                 end="2026-10-10T19:11:00.250000000Z", bpms=(61,))
        row = self.conn.execute("SELECT * FROM v_heart_rate_records").fetchone()
        self.assertEqual(row["duration_sec"], 60)


class MigrationFailureAndBackupTests(h.Pinned003TestCase):
    def setUp(self):
        super().setUp()
        h.make_v2_database(self.db_path, self.tmp, health_rows=True)

    def raw(self):
        conn = db.connect(self.db_path, create=False)
        self.addCleanup(conn.close)
        return conn

    def test_failure_leaves_version_2_untouched(self):
        real = (h.REAL_MIGRATIONS / "003_health_time_precision.sql").read_text(encoding="utf-8")
        assert "DROP TABLE step_intervals;" in real
        variants = {
            "late_failure": dict(extra_sql_for_003="\nCREATE TABLE step_intervals (x);\n"),
            "abort_on_journal_insert": dict(extra_sql_for_003=(
                "\nCREATE TRIGGER trg_block BEFORE INSERT ON schema_migrations "
                "BEGIN SELECT RAISE(ABORT, 'simulated'); END;\n")),
            "middle_failure": dict(replace=("DROP TABLE step_intervals;", "DROP TABLE no_such_table;")),
        }
        for label, spec in variants.items():
            with self.subTest(variant=label):
                path = self.tmp / "data" / f"{label}.db"
                h.make_v2_database(path, self.tmp, health_rows=True)
                conn = db.connect(path, create=False)
                try:
                    fingerprint = h.fingerprint(conn)
                    data = {t: table_rows(conn, t) for t in ALL_TABLES}
                    directory = h.migrations_dir(self.tmp, f"broken-{label}", with_002=True,
                                                 extra_sql_for_003=spec.get("extra_sql_for_003"))
                    if "replace" in spec:
                        target = directory / "003_health_time_precision.sql"
                        target.write_text(real.replace(*spec["replace"]), encoding="utf-8")
                    with mock.patch.object(db, "MIGRATIONS_DIR", directory):
                        with self.assertRaises(sqlite3.Error):
                            db.migrate(conn)
                    self.assertFalse(conn.in_transaction)
                    self.assertEqual(db.schema_version(conn), 2)
                    self.assertEqual(h.fingerprint(conn), fingerprint)           # таблицы, индексы, триггеры, представления
                    self.assertEqual({t: table_rows(conn, t) for t in ALL_TABLES}, data)
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.migrate(conn), [3])                         # настоящая миграция после неудачи проходит
                finally:
                    conn.close()

    def test_upgrade_database_makes_verified_backup_and_is_repeatable(self):
        conn = self.raw()
        before = {t: table_rows(conn, t) for t in ALL_TABLES}
        conn.close()

        report = db.upgrade_database(self.db_path)

        self.assertEqual((report["from_version"], report["to_version"], report["applied"]), (2, 3, [3]))
        self.assertEqual(report["pending"], ["003_health_time_precision.sql"])
        self.assertEqual((report["integrity"], report["foreign_key_violations"]), ("ok", 0))
        backup = Path(report["backup"])
        self.assertIn("before-migration-003", backup.name)
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()

        copy = h.raw_readonly(backup)
        try:
            self.assertEqual(h.raw_version(backup), 2)                                  # копия — версия 2, как была
            self.assertEqual(copy.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(copy.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual({t: [tuple(r) for r in copy.execute(f'SELECT * FROM "{t}" ORDER BY 1')] for t in ["step_intervals",
                             "heart_rate_samples", "sleep_stages"]},
                             {t: before[t] for t in ["step_intervals", "heart_rate_samples", "sleep_stages"]})
        finally:
            copy.close()
        self.assertEqual(h.raw_version(self.db_path), 3)

        again = db.upgrade_database(self.db_path)
        self.assertEqual((again["pending"], again["backup"]), ([], None))
        self.assertEqual(len(list((self.db_path.parent / "backups").glob("*.db"))), 1)
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), digest)       # копия не менялась

    def test_failed_upgrade_keeps_backup_and_database(self):
        conn = self.raw()
        data = {t: table_rows(conn, t) for t in ALL_TABLES}
        conn.close()
        directory = h.migrations_dir(self.tmp, "broken-upgrade", with_002=True,
                                     extra_sql_for_003="\nCREATE TABLE step_intervals (x);\n")
        with mock.patch.object(db, "MIGRATIONS_DIR", directory):
            from fitness.errors import MigrationError
            with self.assertRaises(MigrationError) as ctx:
                db.upgrade_database(self.db_path)
        self.assertIn("before-migration-003", str(ctx.exception))
        backups = list((self.db_path.parent / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(h.raw_version(backups[0]), 2)
        conn = self.raw()
        self.assertEqual(db.schema_version(conn), 2)
        self.assertEqual({t: table_rows(conn, t) for t in ALL_TABLES}, data)
        conn.close()

    def test_normal_open_refuses_version_2_and_changes_nothing(self):
        from fitness.errors import MigrationPendingError
        before = h.file_state(self.db_path)
        with self.assertRaises(MigrationPendingError) as ctx:
            db.open_database(self.db_path)
        self.assertIn("2", str(ctx.exception))
        self.assertIn("3", str(ctx.exception))
        self.assertEqual(h.file_state(self.db_path), before)

    def test_command_line_dry_run_then_upgrade(self):
        before = h.file_state(self.db_path)
        run = lambda *a: subprocess.run([sys.executable, str(MIGRATE), "--db", str(self.db_path), *a],
                                        capture_output=True, timeout=120, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        dry = run("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr.decode("utf-8", "replace"))
        out = dry.stdout.decode("utf-8", "replace")
        self.assertIn("003_health_time_precision.sql", out)
        self.assertIn("004_health_pending_deletions.sql", out)
        self.assertIn("005_photo_trash.sql", out)
        self.assertNotIn("002_health_connect.sql", out)
        self.assertEqual(h.file_state(self.db_path), before)
        self.assertFalse((self.db_path.parent / "backups").exists())
        real = run()
        self.assertEqual(real.returncode, 0, real.stderr.decode("utf-8", "replace"))
        self.assertEqual(h.raw_version(self.db_path), 5)
        self.assertEqual(len(list((self.db_path.parent / "backups").glob("*before-migration-005*.db"))), 1)


class MigrationFromV1AndFreshTests(h.Pinned003TestCase):
    def test_v1_and_v2_and_fresh_databases_end_up_identical(self):
        h.make_v1_database(self.tmp / "data" / "a.db", self.tmp)
        h.make_v2_database(self.tmp / "data" / "b.db", self.tmp)
        fresh = db.open_database(self.tmp / "data" / "c.db")
        fresh.close()
        db.upgrade_database(self.tmp / "data" / "a.db")
        db.upgrade_database(self.tmp / "data" / "b.db")
        objects = {}
        for name in "abc":
            conn = db.connect(self.tmp / "data" / f"{name}.db", create=False)
            try:
                self.assertEqual(db.schema_version(conn), 3, name)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [], name)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok", name)
                objects[name] = {k: normalize_sql(v) for k, v in schema_objects(conn).items()}
            finally:
                conn.close()
        self.assertEqual(objects["a"], objects["c"])
        self.assertEqual(objects["b"], objects["c"])

    def test_v1_with_data_upgrades_through_both_migrations(self):
        path = self.tmp / "data" / "v1.db"
        h.make_v1_database(path, self.tmp)
        conn = db.connect(path, create=False)
        old = {t: table_rows(conn, t) for t in ["measurement_sessions", "workouts", "meals", "alcohol_entries", "notes"]}
        conn.close()
        report = db.upgrade_database(path)
        self.assertEqual(report["applied"], [2, 3])
        conn = db.connect(path, create=False)
        try:
            self.assertEqual({t: table_rows(conn, t) for t in old}, old)
            self.assertEqual(db.migrate(conn), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
