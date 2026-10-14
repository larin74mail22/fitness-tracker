import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fitness import db, records
from fitness.errors import DatabaseMissingError
from tests.helpers import TempDbTestCase

EXPECTED_TABLES = {
    "schema_migrations", "measurement_types", "measurement_sessions", "body_measurements", "photos",
    "workouts", "exercises", "workout_exercises", "exercise_sets", "meals", "meal_items",
    "alcohol_entries", "notes", "note_fields",
}


class SchemaTests(TempDbTestCase):
    def test_all_tables_created(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(EXPECTED_TABLES <= names, EXPECTED_TABLES - names)

    def test_schema_version_is_latest(self):
        self.assertEqual(db.schema_version(self.conn), db.latest_schema_version())
        self.assertGreaterEqual(db.latest_schema_version(), 1)

    def test_migrate_twice_does_nothing(self):
        self.assertEqual(db.migrate(self.conn), [])
        self.assertEqual(self.count("schema_migrations"), db.latest_schema_version())

    def test_foreign_keys_enabled(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO body_measurements (session_id, type_code, value) VALUES (999, 'waist', 80)")

    def test_wal_mode(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_measurement_types_seeded(self):
        self.assertGreaterEqual(self.count("measurement_types"), 10)

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                self.conn.execute("INSERT INTO notes (note_date, category, text) VALUES ('2025-01-01', 'x', 'y')")
                raise RuntimeError("boom")
        self.assertEqual(self.count("notes"), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_updated_at_trigger(self):
        self.conn.execute("INSERT INTO notes (note_date, category, text, created_at, updated_at)"
                          " VALUES ('2025-01-01', 'x', 'y', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z')")
        self.conn.execute("UPDATE notes SET text = 'z'")
        row = self.conn.execute("SELECT created_at, updated_at FROM notes").fetchone()
        self.assertEqual(row["created_at"], "2000-01-01T00:00:00Z")
        self.assertNotEqual(row["updated_at"], "2000-01-01T00:00:00Z")


class FailedMigrationTests(unittest.TestCase):
    def test_broken_migration_is_rolled_back(self):
        tmp = Path(tempfile.mkdtemp(prefix="fitness-test-"))
        try:
            mig_dir = tmp / "migrations"
            mig_dir.mkdir()
            (mig_dir / "001_ok.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
            (mig_dir / "002_bad.sql").write_text("CREATE TABLE b (id INTEGER); THIS IS NOT SQL;",
                                                 encoding="utf-8")
            with mock.patch.object(db, "MIGRATIONS_DIR", mig_dir):
                conn = db.connect(tmp / "m.db")
                try:
                    with self.assertRaises(sqlite3.Error):
                        db.migrate(conn)
                    self.assertEqual(db.schema_version(conn), 1)
                    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    self.assertIn("a", names)
                    self.assertNotIn("b", names)
                finally:
                    conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MissingDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connect_without_create_raises(self):
        missing = self.tmp / "nope.db"
        with self.assertRaises(DatabaseMissingError):
            db.connect(missing, create=False)
        self.assertFalse(missing.exists())

    def test_status_does_not_create_file(self):
        missing = self.tmp / "nope.db"
        status = records.get_status(missing)
        self.assertFalse(status["ok"])
        self.assertFalse(status["database_exists"])
        self.assertFalse(missing.exists())

    def test_status_of_ok_database(self):
        path = self.tmp / "ok.db"
        db.open_database(path).close()
        status = records.get_status(path)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["integrity"], "ok")
        self.assertEqual(status["record_counts"]["workout"], 0)

    def test_status_of_corrupt_file(self):
        path = self.tmp / "broken.db"
        path.write_bytes(b"this is not a sqlite database" * 100)
        status = records.get_status(path)
        self.assertFalse(status["ok"])
        self.assertIn("SQLite", status["message"])

    def test_open_creates_data_folder(self):
        path = self.tmp / "sub" / "dir" / "new.db"
        db.open_database(path).close()
        self.assertTrue(path.exists())

    def test_env_variable_overrides_default_path(self):
        path = self.tmp / "env.db"
        with mock.patch.dict("os.environ", {"FITNESS_DB_PATH": str(path)}):
            self.assertEqual(db.resolve_db_path(), path)


class BackupTests(TempDbTestCase):
    def test_backup_creates_new_files_and_never_overwrites(self):
        self.conn.execute("INSERT INTO notes (note_date, category, text) VALUES ('2025-01-01', 'тест', 'данные')")
        backup_dir = self.tmp / "backups"
        first = db.backup(self.conn, backup_dir)
        second = db.backup(self.conn, backup_dir)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())
        copy = sqlite3.connect(str(first))
        try:
            self.assertEqual(copy.execute("SELECT text FROM notes").fetchone()[0], "данные")
        finally:
            copy.close()


if __name__ == "__main__":
    unittest.main()
