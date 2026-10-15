"""Подключение к базе строго на чтение.

Три независимых уровня защиты:
1. файл открывается в режиме SQLite mode=ro (запись невозможна на уровне драйвера);
2. PRAGMA query_only = ON;
3. authorizer разрешает только чтение (SELECT, чтение столбцов, функции) — любая другая команда отклоняется.
Миграции не запускаются, отсутствующая база не создаётся.
"""

import sqlite3
from contextlib import contextmanager

from .. import db
from ..i18n import t

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class DatabaseUnavailable(Exception):
    """База отсутствует, повреждена или её схема не совпадает с ожидаемой.

    str(exc) — всегда по-русски, как до перевода интерфейса: это же сообщение попадает в ответы MCP-инструментов
    (fitness.health_status). Панель показывает localized() — на выбранном языке интерфейса.
    """

    def __init__(self, key, **params):
        self.key, self.params = key, params
        super().__init__(t(key, lang="ru", **params))

    def localized(self):
        return t(self.key, **self.params)


def _authorizer(action, arg1, arg2, db_name, trigger):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def connect_readonly(path=None):
    db_path = db.resolve_db_path(path)
    if not db_path.is_file():
        raise DatabaseUnavailable("db_unavailable.missing", path=db_path)
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise DatabaseUnavailable("db_unavailable.open_failed", path=db_path, error=exc) from None
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        version = db.schema_version(conn)
        expected = db.latest_schema_version()
        if version != expected:
            raise DatabaseUnavailable("db_unavailable.schema", version=version, expected=expected)
        conn.set_authorizer(_authorizer)
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseUnavailable("db_unavailable.read_failed", path=db_path, error=exc) from None
    except DatabaseUnavailable:
        conn.close()
        raise
    return conn


@contextmanager
def readonly_connection(path=None):
    conn = connect_readonly(path)
    try:
        yield conn
    finally:
        conn.close()
