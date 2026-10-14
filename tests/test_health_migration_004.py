"""Миграция 004 (таблица ожидающих удалений): цепочка 001-004, повторное применение, ограничения, обрыв процесса.
Работает только с временными базами.
"""

import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from fitness import db, health_import
from fitness.errors import MigrationPendingError
from tests import health_helpers as h
from tests import health_samples as hs
from tests.test_health_migration_003 import ALL_TABLES, normalize_sql, schema_objects, table_rows

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
NEW_TABLE = "health_pending_deletions"


def at_version_3(tmp, path, populate=True):
    """База ровно версии 3 с данными Health Connect (получена миграциями 001-003)."""
    h.make_v2_database(path, tmp, health_rows=populate)
    pinned = h.migrations_dir(tmp, f"v3-only-{path.stem}", with_002=True, with_003=True)
    with mock.patch.object(db, "MIGRATIONS_DIR", pinned):
        assert db.upgrade_database(path)["to_version"] == 3


class Migration004Tests(h.IsolatedTestCase):
    """Последняя миграция в папке — 004: так тесты проверяют именно её. Цепочка до 005 — test_photo_trash.py."""

    def setUp(self):
        super().setUp()
        pinned = h.migrations_dir(self.tmp, "pinned-to-004", with_002=True, with_003=True, with_004=True)
        patcher = mock.patch.object(db, "MIGRATIONS_DIR", pinned)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_v3_with_data_gets_the_table_and_loses_nothing(self):
        path = self.tmp / "data" / "v3.db"
        at_version_3(self.tmp, path)
        conn = db.connect(path, create=False)
        before = {t: table_rows(conn, t) for t in ALL_TABLES}
        objects = schema_objects(conn)
        conn.close()
        report = db.upgrade_database(path)
        self.assertEqual((report["from_version"], report["to_version"], report["applied"]), (3, 4, [4]))
        self.assertEqual((report["integrity"], report["foreign_key_violations"]), ("ok", 0))
        self.assertIn("before-migration-004", report["backup"])
        conn = db.connect(path, create=False)
        try:
            self.assertEqual(db.schema_version(conn), 4)
            after = {t: table_rows(conn, t) for t in ALL_TABLES}
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            added = set(schema_objects(conn)) - set(objects)
        finally:
            conn.close()
        self.assertEqual(versions, [1, 2, 3, 4])
        self.assertEqual({t: rows for t, rows in after.items() if t != "schema_migrations"},
                         {t: rows for t, rows in before.items() if t != "schema_migrations"})
        self.assertEqual({name for _, name in added}, {NEW_TABLE, "uq_pending_deletions_source", "uq_pending_deletions_phone",
                                                       "idx_pending_deletions_batch"})

    def test_every_older_version_reaches_the_same_schema(self):
        h.make_v1_database(self.tmp / "data" / "a.db", self.tmp)
        h.make_v2_database(self.tmp / "data" / "b.db", self.tmp)
        at_version_3(self.tmp, self.tmp / "data" / "c.db")
        db.open_database(self.tmp / "data" / "d.db").close()                            # новая база
        for name in "abc":
            db.upgrade_database(self.tmp / "data" / f"{name}.db")
        objects = {}
        for name in "abcd":
            conn = db.connect(self.tmp / "data" / f"{name}.db", create=False)
            try:
                self.assertEqual(db.schema_version(conn), 4, name)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [], name)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok", name)
                objects[name] = {k: normalize_sql(v) for k, v in schema_objects(conn).items()}
            finally:
                conn.close()
        for name in "abc":
            self.assertEqual(objects[name], objects["d"], name)

    def test_rerun_changes_nothing(self):
        path = self.tmp / "data" / "v3.db"
        at_version_3(self.tmp, path)
        db.upgrade_database(path)
        state = h.file_state(path)
        report = db.upgrade_database(path)
        self.assertEqual((report["applied"], report["backup"]), ([], None))
        conn = db.connect(path, create=False)
        try:
            self.assertEqual(db.migrate(conn), [])
        finally:
            conn.close()
        self.assertEqual(h.file_state(path), state)

    def test_constraints_of_the_new_table(self):
        conn = db.open_database(self.tmp / "data" / "f.db")
        batch = conn.execute(
            "INSERT INTO import_batches (file_name, sha256, format_version, phone_id, sequence, exported_at, status)"
            " VALUES ('f.json', ?, 1, 'p', 1, '2026-10-10T10:00:00Z', 'ok')", ("a" * 64,)).lastrowid
        insert = ("INSERT INTO health_pending_deletions (source_key, phone_id, hc_id, batch_id, exported_at) VALUES (?, ?, ?, ?, ?)")
        stamp = "2026-10-10T10:00:00Z"
        conn.execute(insert, ("k", None, "x", batch, stamp))
        conn.execute(insert, (None, "p", "x", batch, stamp))
        bad = [("обе области пусты", (None, None, "x", batch, stamp)), ("обе заполнены", ("k2", "p2", "x", batch, stamp)),
               ("дубль по источнику", ("k", None, "x", batch, stamp)), ("дубль по телефону", (None, "p", "x", batch, stamp)),
               ("пустой hc_id", ("k", None, "", batch, stamp)), ("нет такого батча", ("k", None, "y", 999, stamp)),
               ("время с долей секунды", ("k", None, "z", batch, "2026-10-10T10:00:00.000000000Z")), ("время без Z", ("k", None, "z", batch, "2026-10-10T10:00:00"))]
        try:
            for label, values in bad:
                with self.subTest(label):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(insert, values)
            with self.assertRaises(sqlite3.IntegrityError):                      # батч с ожидающим удалением не удалить
                conn.execute("DELETE FROM import_batches WHERE id = ?", (batch,))
        finally:
            conn.close()

    def test_an_interrupted_004_leaves_version_3_and_the_migration_then_completes(self):
        path = self.tmp / "data" / "v3.db"
        at_version_3(self.tmp, path)
        conn = db.connect(path, create=False)
        before = {t: table_rows(conn, t) for t in ALL_TABLES}
        fingerprint = h.fingerprint(conn)
        conn.close()
        script = self.tmp / "interrupt.py"
        script.write_text(CHILD.format(clone=str(h.PROJECT_DIR), path=str(path),
                                       migration=str(h.REAL_MIGRATIONS / "004_health_pending_deletions.sql"),
                                       marker="CREATE UNIQUE INDEX uq_pending_deletions_phone"), encoding="utf-8")
        proc = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 3, proc.stderr.decode("utf-8", "replace"))
        conn = db.connect(path, create=False)
        try:
            self.assertEqual(db.schema_version(conn), 3)
            self.assertEqual(h.fingerprint(conn), fingerprint)                  # таблицы и индексы 004 не остались
            self.assertEqual({t: table_rows(conn, t) for t in ALL_TABLES}, before)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        self.assertEqual(db.upgrade_database(path)["to_version"], 4)

    def test_a_broken_004_leaves_version_3_untouched(self):
        path = self.tmp / "data" / "v3.db"
        at_version_3(self.tmp, path)
        conn = db.connect(path, create=False)
        fingerprint = h.fingerprint(conn)
        conn.close()
        broken = h.migrations_dir(self.tmp, "broken-004", with_002=True, with_003=True)
        sql = (h.REAL_MIGRATIONS / "004_health_pending_deletions.sql").read_text(encoding="utf-8")
        (broken / "004_health_pending_deletions.sql").write_text(sql + "\nCREATE TABLE health_pending_deletions (x);\n", encoding="utf-8")
        with mock.patch.object(db, "MIGRATIONS_DIR", broken):
            with self.assertRaises(Exception):
                db.upgrade_database(path)
        conn = db.connect(path, create=False)
        try:
            self.assertEqual((db.schema_version(conn), h.fingerprint(conn)), (3, fingerprint))
        finally:
            conn.close()

    def test_the_importer_refuses_a_database_without_004_and_the_guard_never_migrates(self):
        path = self.tmp / "data" / "v3.db"
        at_version_3(self.tmp, path, populate=False)
        doc = hs.write(self.tmp / "in", "a.json", hs.export(1, deleted=["X"]))
        before = h.file_state(path)
        with self.assertRaises(MigrationPendingError):
            health_import.import_file(doc, path)
        self.assertEqual(h.file_state(path), before)
        self.assertEqual(h.raw_version(path), 3)


if __name__ == "__main__":
    unittest.main()
