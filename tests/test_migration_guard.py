"""Защита от автоматического обновления схемы и явная команда migrate_db.py.

Главный сценарий: существующая база со старой схемой НЕ должна меняться при обычных вызовах MCP.
"""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from fitness import db, tools
from fitness.errors import DatabaseMissingError, MigrationError, MigrationPendingError
from tests import health_helpers as h

SERVER = h.PROJECT_DIR / "server.py"
MIGRATE = h.PROJECT_DIR / "migrate_db.py"


class OldSchemaTestCase(h.IsolatedTestCase):
    """База ровно версии 1 с выдуманными данными, как рабочая база сейчас."""

    def setUp(self):
        super().setUp()
        h.make_v1_database(self.db_path, self.tmp)
        self.before = h.file_state(self.db_path)
        patcher = mock.patch.dict(os.environ, {"FITNESS_DB_PATH": str(self.db_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch("sys.stderr", mock.MagicMock())   # журнал инструментов не засоряет вывод тестов
        quiet.start()
        self.addCleanup(quiet.stop)

    def assertDatabaseUntouched(self):
        self.assertEqual(h.file_state(self.db_path), self.before)
        self.assertEqual(h.raw_version(self.db_path), 1)
        self.assertFalse((self.db_path.parent / "backups").exists())
        conn = h.raw_readonly(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'step_intervals'").fetchone()[0], 0)
        finally:
            conn.close()


class GuardTests(OldSchemaTestCase):
    def test_new_database_gets_full_schema(self):
        path = self.tmp / "fresh" / "new.db"
        conn = db.open_database(path)
        try:
            self.assertEqual(db.schema_version(conn), db.latest_schema_version())
            self.assertGreaterEqual(db.latest_schema_version(), 2)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertTrue({"step_intervals", "heart_rate_records", "sleep_sessions", "notes"} <= tables)
        finally:
            conn.close()

    def test_existing_old_database_is_not_migrated(self):
        with self.assertRaises(MigrationPendingError) as ctx:
            db.open_database(self.db_path)
        self.assertIn("migrate_db.py", str(ctx.exception))
        self.assertIn("1", str(ctx.exception))
        self.assertDatabaseUntouched()

    def test_old_database_still_readable_by_plain_connection(self):
        conn = db.connect(self.db_path, create=False)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
        finally:
            conn.close()

    def test_database_newer_than_code_is_accepted(self):
        path = self.tmp / "newer.db"
        conn = db.open_database(path)            # последняя версия
        conn.close()
        latest = db.latest_schema_version()
        with mock.patch.object(db, "MIGRATIONS_DIR", h.migrations_dir(self.tmp, "only-v1-code")):
            db.open_database(path).close()       # код, знающий только 001, более новую базу не ломает
        self.assertEqual(h.raw_version(path), latest)

    def test_every_mcp_tool_refuses_without_changing_database(self):
        calls = {
            "list_exercises": {},
            "get_measurements": {"date_from": "2025-01-01", "date_to": "2025-12-31"},
            "get_workouts": {"date_from": "2025-01-01", "date_to": "2025-12-31"},
            "get_nutrition_summary": {"date_from": "2025-01-01", "date_to": "2025-12-31"},
            "get_alcohol_summary": {"date_from": "2025-01-01", "date_to": "2025-12-31"},
            "search_records": {"query": "тест"},
            "get_record": {"record_type": "note", "id": 1},
            "prepare_delete_record": {"record_type": "note", "id": 1},
            "delete_record": {"record_type": "note", "id": 1, "confirmation_code": "AAAAAAAA"},
            "add_note": {"note_date": "2025-09-01", "category": "тест", "text": "новая"},
            "log_measurement": {"measured_on": "2025-09-01", "weight_kg": 80},
            "backup_database": {},
        }
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                result = tools.call_tool(name, arguments)
                self.assertTrue(result["isError"], name)
                payload = json.loads(result["content"][0]["text"])
                self.assertEqual(payload["error"], "migration_pending")
                self.assertIn("migrate_db.py", payload["message"])
        self.assertDatabaseUntouched()

    def test_status_tool_reports_mismatch_without_changing_database(self):
        result = tools.call_tool("get_tracker_status", {})
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(result["isError"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["schema_version_expected"], db.latest_schema_version())
        self.assertFalse(payload["database_ok"])
        self.assertDatabaseUntouched()

    def test_real_stdio_server_does_not_migrate_old_database(self):
        """Критический сценарий: настоящий server.py (как запускает туннель) на базе со старой схемой."""
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_exercises", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "add_note", "arguments": {"note_date": "2025-09-01", "category": "тест", "text": "новая"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_tracker_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        ]
        data = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["FITNESS_DB_PATH"] = str(self.db_path)
        proc = subprocess.run([sys.executable, str(SERVER)], input=data, capture_output=True, cwd=str(self.tmp),
                              env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        by_id = {r["id"]: r for r in (json.loads(line) for line in proc.stdout.decode("ascii").splitlines())}
        for request_id in (2, 3):
            result = by_id[request_id]["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"])["error"], "migration_pending")
        self.assertFalse(by_id[4]["result"]["isError"])           # статус отвечает и показывает несовпадение
        self.assertEqual(by_id[5]["result"], {})                  # сервер жив
        self.assertDatabaseUntouched()


class UpgradeDatabaseTests(OldSchemaTestCase):
    def old_data(self):
        conn = h.raw_readonly(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            return h.snapshot(conn)
        finally:
            conn.close()

    def test_dry_run_changes_nothing(self):
        report = db.upgrade_database(self.db_path, dry_run=True)
        self.assertEqual((report["from_version"], report["to_version"]), (1, 1))
        self.assertEqual(report["pending"], ["002_health_connect.sql", "003_health_time_precision.sql", "004_health_pending_deletions.sql",
                                             "005_photo_trash.sql"])
        self.assertIsNone(report["backup"])
        self.assertDatabaseUntouched()

    def test_upgrade_with_backup(self):
        data_before = self.old_data()
        report = db.upgrade_database(self.db_path)

        self.assertEqual(report["applied"], [2, 3, 4, 5])
        self.assertEqual((report["from_version"], report["to_version"]), (1, 5))
        self.assertEqual((report["integrity"], report["foreign_key_violations"]), ("ok", 0))
        self.assertEqual(h.raw_version(self.db_path), 5)
        self.assertEqual({k: v for k, v in self.old_data().items() if k in data_before}, data_before)

        backup = Path(report["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent, self.db_path.parent / "backups")
        self.assertIn("before-migration-005", backup.name)
        self.assertEqual(h.raw_version(backup), 1)                          # копия — это старая версия
        copy = h.raw_readonly(backup)
        try:
            copy.row_factory = sqlite3.Row
            self.assertEqual({k: v for k, v in h.snapshot(copy).items() if k in data_before}, data_before)
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            copy.close()

        db.open_database(self.db_path).close()                              # после обновления обычное открытие работает

    def test_second_run_is_noop_without_new_backup(self):
        db.upgrade_database(self.db_path)
        before = h.file_state(self.db_path)
        report = db.upgrade_database(self.db_path)
        self.assertEqual(report["pending"], [])
        self.assertIsNone(report["backup"])
        self.assertEqual(len(list((self.db_path.parent / "backups").glob("*.db"))), 1)
        self.assertEqual(h.file_state(self.db_path), before)

    def test_failed_migration_keeps_database_and_backup(self):
        data_before = self.old_data()
        broken = h.migrations_dir(self.tmp, "broken", extra_sql_for_002="\nCREATE TABLE step_intervals (x);\n")
        with mock.patch.object(db, "MIGRATIONS_DIR", broken):
            with self.assertRaises(MigrationError) as ctx:
                db.upgrade_database(self.db_path)
        self.assertIn("before-migration-002", str(ctx.exception))
        self.assertEqual(h.raw_version(self.db_path), 1)
        self.assertEqual(self.old_data(), data_before)
        backups = list((self.db_path.parent / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(h.raw_version(backups[0]), 1)
        # после неудачи обновление настоящей миграцией проходит
        self.assertEqual(db.upgrade_database(self.db_path)["to_version"], 5)

    def test_failure_after_all_changes_rolls_back(self):
        extra = ("\nCREATE TRIGGER trg_block BEFORE INSERT ON schema_migrations "
                 "BEGIN SELECT RAISE(ABORT, 'simulated'); END;\n")
        broken = h.migrations_dir(self.tmp, "broken-late", extra_sql_for_002=extra)
        with mock.patch.object(db, "MIGRATIONS_DIR", broken):
            with self.assertRaises(MigrationError):
                db.upgrade_database(self.db_path)
        conn = h.raw_readonly(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'step_%' "
                                          "OR name LIKE 'v_%'").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertEqual(h.raw_version(self.db_path), 1)

    def test_backup_that_does_not_match_stops_before_migration(self):
        original = db.backup

        def damaged_backup(conn, backup_dir=None, label=None):
            path = original(conn, backup_dir, label)
            copy = sqlite3.connect(str(path))
            copy.execute("DELETE FROM notes")
            copy.commit()
            copy.close()
            return path

        with mock.patch.object(db, "backup", damaged_backup):
            with self.assertRaises(MigrationError) as ctx:
                db.upgrade_database(self.db_path)
        self.assertIn("Миграция не начиналась", str(ctx.exception))
        self.assertEqual(h.raw_version(self.db_path), 1)

    def test_missing_database_is_not_created(self):
        missing = self.tmp / "nowhere" / "missing.db"
        with self.assertRaises(DatabaseMissingError):
            db.upgrade_database(missing)
        self.assertFalse(missing.parent.exists())

    def test_foreign_sqlite_file_is_refused(self):
        other = self.tmp / "other.db"
        raw = sqlite3.connect(other)
        raw.execute("CREATE TABLE contacts (name TEXT)")
        raw.commit()
        raw.close()
        before = h.file_state(other)
        with self.assertRaises(MigrationError):
            db.upgrade_database(other)
        self.assertEqual(h.file_state(other), before)
        self.assertFalse((other.parent / "backups").exists())

    def test_corrupt_file_is_reported(self):
        broken = self.tmp / "broken.db"
        broken.write_bytes(b"this is not a database" * 200)
        with self.assertRaises(sqlite3.DatabaseError):
            db.upgrade_database(broken)
        self.assertFalse((broken.parent / "backups").exists())


class CommandLineTests(OldSchemaTestCase):
    def run_cli(self, *args, use_env=False):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("FITNESS_DB_PATH", None)
        command = [sys.executable, str(MIGRATE), *args]
        if use_env:
            env["FITNESS_DB_PATH"] = str(self.db_path)
        proc = subprocess.run(command, capture_output=True, cwd=str(self.tmp), env=env, timeout=120)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def test_dry_run(self):
        code, out, err = self.run_cli("--db", str(self.db_path), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("002_health_connect.sql", out)
        self.assertIn("Пробный запуск", out)
        self.assertDatabaseUntouched()

    def test_upgrade_and_repeat(self):
        code, out, err = self.run_cli("--db", str(self.db_path))
        self.assertEqual(code, 0, err)
        self.assertIn("Резервная копия", out)
        self.assertEqual(h.raw_version(self.db_path), 5)
        self.assertEqual(len(list((self.db_path.parent / "backups").glob("*before-migration-005*.db"))), 1)
        code, out, err = self.run_cli("--db", str(self.db_path))
        self.assertEqual(code, 0, err)
        self.assertIn("уже последней версии", out)
        self.assertEqual(len(list((self.db_path.parent / "backups").glob("*.db"))), 1)

    def test_uses_same_path_variable_as_server(self):
        code, out, err = self.run_cli("--dry-run", use_env=True)
        self.assertEqual(code, 0, err)
        self.assertIn(str(self.db_path), out)

    def test_missing_database_fails_without_creating_it(self):
        missing = self.tmp / "none" / "x.db"
        code, out, err = self.run_cli("--db", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("не найдена", err)
        self.assertFalse(missing.parent.exists())

    def test_failure_exit_code(self):
        broken = self.tmp / "bad.db"
        broken.write_bytes(b"this is not a database" * 200)
        code, out, err = self.run_cli("--db", str(broken))
        self.assertEqual(code, 1)
        self.assertIn("не изменена", err)


if __name__ == "__main__":
    unittest.main()
