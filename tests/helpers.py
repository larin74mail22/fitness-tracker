"""Общая основа тестов: каждая проверка получает новую пустую временную базу.

Все данные в тестах выдуманные.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from fitness import db


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-test-"))
        self.db_path = self.tmp / "test.db"
        assert self.db_path.resolve() != db.DEFAULT_DB_PATH.resolve()
        self.conn = db.open_database(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
