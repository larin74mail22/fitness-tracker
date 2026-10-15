"""Помощники для тестов миграции 002 и защиты от автообновления схемы.

Все данные выдуманные. Базы создаются только во временных папках.
"""

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fitness import alcohol, db, measurements, notes, nutrition, workouts

REAL_MIGRATIONS = db.MIGRATIONS_DIR
PROJECT_DIR = Path(__file__).resolve().parent.parent
HASH = "0" * 64
TS = "2026-10-10T07:02:00Z"


def canon(value):
    """Время записей Health Connect хранится ровно с 9 цифрами доли секунды (схема v3)."""
    if value is None or "." in value:
        return value
    return value[:-1] + ".000000000Z"


class IsolatedTestCase(unittest.TestCase):
    """Временная папка на тест; рабочая база проекта никогда не используется."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-health-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "data" / "test.db"
        self.assertNotEqual(self.db_path.resolve(), db.DEFAULT_DB_PATH.resolve())


def migrations_dir(tmp, name, extra_sql_for_002=None, with_002=False, with_003=False, extra_sql_for_003=None, with_004=False):
    """Папка миграций для теста: настоящая 001; 002 и 003 — настоящие, сломанные (с добавкой) или отсутствуют."""
    directory = Path(tmp) / name
    directory.mkdir(parents=True)
    shutil.copy(REAL_MIGRATIONS / "001_initial.sql", directory / "001_initial.sql")
    if with_002 or extra_sql_for_002 is not None:
        sql = (REAL_MIGRATIONS / "002_health_connect.sql").read_text(encoding="utf-8")
        (directory / "002_health_connect.sql").write_text(sql + (extra_sql_for_002 or ""), encoding="utf-8")
    if with_003 or extra_sql_for_003 is not None:
        sql = (REAL_MIGRATIONS / "003_health_time_precision.sql").read_text(encoding="utf-8")
        (directory / "003_health_time_precision.sql").write_text(sql + (extra_sql_for_003 or ""), encoding="utf-8")
    if with_004:
        shutil.copy(REAL_MIGRATIONS / "004_health_pending_deletions.sql", directory / "004_health_pending_deletions.sql")
    return directory


class Pinned003TestCase(IsolatedTestCase):
    """Тесты миграций 001-003 работают с папкой миграций, где последняя миграция 003: так они проверяют именно её.
    Цепочка до 004 проверяется отдельно (tests/test_health_migration_004.py)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(db, "MIGRATIONS_DIR", migrations_dir(self.tmp, "pinned-to-003", with_002=True, with_003=True))
        patcher.start()
        self.addCleanup(patcher.stop)


def add_old_data(conn):
    """Выдуманные записи во всех старых категориях."""
    measurements.log_measurement(conn, {"measured_on": "2025-03-01", "weight_kg": 81.4,
                                        "measurements": [{"type": "waist", "value": 86.5}], "notes": "тест"})
    workouts.log_workout(conn, {"workout_date": "2025-04-02", "title": "Верх тела", "exercises": [
        {"name": "Жим лёжа", "sets": [{"weight_kg": 60, "reps": 8, "rir": 2}], "discomfort": "тест"}]})
    nutrition.log_meal(conn, {"eaten_on": "2025-06-01", "items": [
        {"name": "Овсянка", "quantity": 60, "unit": "g", "kcal": 220, "nutrition_source": "label",
         "is_estimate": False}]})
    alcohol.log_alcohol(conn, {"consumed_on": "2025-07-01", "drink": "Пиво", "volume_ml": 500, "abv_percent": 5})
    notes.add_note(conn, {"note_date": "2025-08-01", "category": "тест", "text": "заметка",
                          "fields": [{"key": "сон", "value": 7.5, "unit": "ч"}]})


def make_v1_database(path, tmp, populate=True):
    """База ровно версии 1 (только 001), как рабочая база сейчас."""
    with mock.patch.object(db, "MIGRATIONS_DIR", migrations_dir(tmp, f"v1-only-{Path(path).stem}")):
        conn = db.open_database(path)
        assert db.schema_version(conn) == 1
        if populate:
            add_old_data(conn)
        conn.close()   # закрытие последнего соединения переносит WAL в основной файл


def snapshot(conn, only=None):
    """Содержимое таблиц (кроме schema_migrations) для сравнения до и после."""
    result = {}
    for name in db._user_tables(conn):
        if name == "schema_migrations" or (only is not None and name not in only):
            continue
        result[name] = [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()]
    return result


def fingerprint(conn):
    """Все объекты схемы (таблицы, индексы, триггеры, представления) с их SQL."""
    return [tuple(r) for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")]


def file_state(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, path.stat().st_size


def raw_readonly(path):
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


def raw_version(path):
    conn = raw_readonly(path)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()


# ---------- Вставки для проверки ограничений ----------

def add_source(conn, key="health_connect|app.example.zepp||||", package="app.example.zepp"):
    return conn.execute(
        "INSERT INTO data_sources (source_key, platform, package_name) VALUES (?, 'health_connect', ?)",
        (key, package)).lastrowid


def add_batch(conn, sha="a" * 64, phone="phone-1", seq=1, status="ok", exported="2026-10-10T17:00:00Z",
              imported=None, error=None):
    error = error if error is not None else ("тестовая ошибка" if status == "failed" else None)
    columns = "file_name, sha256, format_version, phone_id, sequence, exported_at, status, error"
    values = [f"export-{seq}.json", sha, 1, phone, seq, exported, status, error]
    if imported is not None:
        columns += ", imported_at"
        values.append(imported)
    marks = ", ".join("?" for _ in values)
    return conn.execute(f"INSERT INTO import_batches ({columns}) VALUES ({marks})", values).lastrowid


def add_step(conn, source, batch, hc_id="step-1", start="2026-10-10T00:00:00Z", end="2026-10-10T00:10:00Z",
             count=420, local_date="2026-10-10", deleted_at=None, **overrides):
    row = {"source_id": source, "hc_id": hc_id, "start_utc": canon(start), "end_utc": canon(end),
           "local_date": local_date, "count": count, "hc_last_modified": canon(TS), "payload_hash": HASH,
           "batch_id": batch, "deleted_at": deleted_at}
    row.update(overrides)
    return _insert(conn, "step_intervals", row)


def add_hr(conn, source, batch, hc_id="hr-1", start="2026-10-10T19:10:00Z", end="2026-10-10T19:11:00Z",
           local_date="2026-10-10", deleted_at=None, bpms=(61,), **overrides):
    row = {"source_id": source, "hc_id": hc_id, "start_utc": canon(start), "end_utc": canon(end),
           "local_date": local_date, "hc_last_modified": canon(TS), "payload_hash": HASH, "batch_id": batch,
           "deleted_at": deleted_at}
    row.update(overrides)
    record_id = _insert(conn, "heart_rate_records", row)
    for i, bpm in enumerate(bpms):
        conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, ?)",
                     (record_id, canon(f"2026-10-10T19:1{i}:00Z"), bpm))
    return record_id


def add_sleep(conn, source, batch, hc_id="sleep-1", start="2026-10-10T07:02:00Z", end="2026-10-10T15:20:00Z",
              local_date="2026-10-10", deleted_at=None, stages=(), **overrides):
    row = {"source_id": source, "hc_id": hc_id, "start_utc": canon(start), "end_utc": canon(end),
           "local_date": local_date, "hc_last_modified": canon(TS), "payload_hash": HASH, "batch_id": batch,
           "deleted_at": deleted_at}
    row.update(overrides)
    session_id = _insert(conn, "sleep_sessions", row)
    for stage, s, e in stages:
        conn.execute("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                     (session_id, stage, canon(s), canon(e)))
    return session_id


def _insert(conn, table, row):
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    # table — только из констант этого файла.
    return conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values())).lastrowid


# Синтетические стадии сна (выдуманные, 07:02–15:20).
SCREEN_STAGES = [
    ("light", "2026-10-10T07:02:00Z", "2026-10-10T07:30:00Z"),
    ("deep", "2026-10-10T07:30:00Z", "2026-10-10T07:50:00Z"),
    ("light", "2026-10-10T07:50:00Z", "2026-10-10T08:10:00Z"),
    ("awake", "2026-10-10T08:10:00Z", "2026-10-10T08:12:00Z"),
    ("light", "2026-10-10T08:12:00Z", "2026-10-10T08:40:00Z"),
    ("awake", "2026-10-10T08:40:00Z", "2026-10-10T08:43:00Z"),
    ("light", "2026-10-10T08:43:00Z", "2026-10-10T09:15:00Z"),
    ("awake", "2026-10-10T09:15:00Z", "2026-10-10T09:20:00Z"),
]


def make_v2_database(path, tmp, populate=True, health_rows=False):
    """База ровно версии 2 (001 и 002): в ней время хранится с точностью до секунды."""
    with mock.patch.object(db, "MIGRATIONS_DIR", migrations_dir(tmp, f"v2-only-{Path(path).stem}", with_002=True)):
        conn = db.open_database(path)
        assert db.schema_version(conn) == 2
        if populate:
            add_old_data(conn)
        if health_rows:
            add_v2_health_rows(conn)
        conn.close()


def add_v2_health_rows(conn):
    """Строки во всех таблицах Health Connect в формате v2 (секунды), включая удалённые и дочерние."""
    source = add_source(conn)
    b1 = add_batch(conn, sha="1" * 64, seq=1, imported="2026-10-10T17:30:00Z")
    b2 = add_batch(conn, sha="2" * 64, seq=2, imported="2026-10-10T18:30:00Z")
    add_batch(conn, sha="3" * 64, seq=3, status="failed", imported="2026-10-10T19:30:00Z")
    conn.execute("INSERT INTO import_batch_counts (batch_id, record_type, inserted, updated) VALUES (?, 'steps', 2, 0)", (b1,))
    base = {"source_id": source, "hc_last_modified": TS, "payload_hash": HASH, "local_date": "2026-10-10"}
    _insert(conn, "step_intervals", dict(base, hc_id="s1", start_utc="2026-10-10T00:00:00Z", end_utc="2026-10-10T00:10:00Z",
                                         count=420, batch_id=b1, client_record_id="c1", client_record_version=3))
    _insert(conn, "step_intervals", dict(base, hc_id="s2", start_utc="2026-10-10T00:10:00Z", end_utc="2026-10-10T00:20:00Z",
                                         count=730, batch_id=b2, deleted_at="2026-10-10T20:00:00Z", revision=2))
    for hc, start, end, bpms in (("h1", "19:10", "19:11", (61, 64)), ("h2", "19:20", "19:20", (70,))):
        rid = _insert(conn, "heart_rate_records", dict(base, hc_id=hc, start_utc=f"2026-10-10T{start}:00Z",
                                                       end_utc=f"2026-10-10T{end}:00Z", batch_id=b1,
                                                       start_offset_min=180))
        for i, bpm in enumerate(bpms):
            conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, ?)",
                         (rid, f"2026-10-10T{start}:{i:02d}Z", bpm))
    sid = _insert(conn, "sleep_sessions", dict(base, hc_id="z1", start_utc="2026-10-10T07:02:00Z",
                                               end_utc="2026-10-10T15:20:00Z", batch_id=b2, title="сон", notes=None))
    for stage, a, z in SCREEN_STAGES:
        conn.execute("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, ?, ?, ?)", (sid, stage, a, z))
    sid2 = _insert(conn, "sleep_sessions", dict(base, hc_id="z2", start_utc="2026-10-09T22:00:00Z",
                                                end_utc="2026-10-10T05:00:00Z", batch_id=b2, deleted_at="2026-10-10T21:00:00Z"))
    conn.execute("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, 'deep', "
                 "'2026-10-09T23:00:00Z', '2026-10-09T23:30:00Z')", (sid2,))
