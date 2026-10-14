"""Подключение к SQLite, миграции схемы и резервные копии."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .errors import BackupError, DatabaseMissingError, MigrationError, MigrationPendingError

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "fitness.db"
BACKUP_DIR = DATA_DIR / "backups"
PHOTOS_DIR = DATA_DIR / "photos"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def resolve_db_path(path=None):
    """Путь к базе: явный аргумент, затем переменная FITNESS_DB_PATH, затем data/fitness.db."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get("FITNESS_DB_PATH")
    return Path(env_path) if env_path else DEFAULT_DB_PATH


def _lower(value):
    # Встроенный LOWER в SQLite не понимает кириллицу.
    return value.lower() if isinstance(value, str) else value


def connect(path=None, create=True):
    """Открыть базу. При create=False отсутствующая база — ошибка, а не новый пустой файл."""
    db_path = resolve_db_path(path)
    if not db_path.exists():
        if not create:
            raise DatabaseMissingError(f"База данных не найдена: {db_path}")
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None: транзакции управляются явно через transaction().
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.create_function("py_lower", 1, _lower, deterministic=True)
    return conn


@contextmanager
def transaction(conn):
    """Всё внутри блока сохраняется целиком или не сохраняется вовсе."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migration_files():
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        files.append((version, path))
    return files


def latest_schema_version():
    files = _migration_files()
    return files[-1][0] if files else 0


def schema_version(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn):
    """Применить новые миграции по порядку. Каждая миграция — одна транзакция."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    current = schema_version(conn)
    applied = []
    for version, path in _migration_files():
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        name = path.name.replace("'", "")
        # executescript сам завершает открытые транзакции, поэтому BEGIN/COMMIT — внутри скрипта.
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + sql
                + f"\nINSERT INTO schema_migrations (version, name) VALUES ({version}, '{name}');\n"
                + "COMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def pending_migrations(conn):
    """Миграции, которые ещё не применены к этой базе: список (версия, путь к файлу)."""
    current = schema_version(conn)
    return [(version, path) for version, path in _migration_files() if version > current]


def open_database(path=None, create=True):
    """Открыть базу для обычной работы (MCP-сервер, инструменты).

    Новая пустая база получает схему целиком. Существующая база с устаревшей схемой НЕ обновляется:
    обычные вызовы не должны неожиданно менять данные пользователя. Обновление выполняется
    только явной командой migrate_db.py (см. upgrade_database), которая делает резервную копию.
    """
    conn = connect(path, create=create)
    try:
        current = schema_version(conn)
        latest = latest_schema_version()
        if current == 0:
            migrate(conn)
        elif current < latest:
            raise MigrationPendingError(
                f"Схема базы устарела (версия {current}, нужна {latest}). Данные не изменены. "
                f"Обновление делается отдельной командой с резервной копией: "
                f"python {PROJECT_DIR.as_posix()}/migrate_db.py"
            )
    except Exception:
        conn.close()
        raise
    return conn


def _user_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _row_counts(conn):
    # Имена таблиц берутся из самой базы (sqlite_master), а не из входных данных.
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in _user_tables(conn)}


def _open_plain_readonly(db_path):
    return sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True, timeout=10)


def _verify_backup(backup_path, expected_version, expected_counts):
    check = _open_plain_readonly(backup_path)
    try:
        quick = check.execute("PRAGMA quick_check").fetchone()[0]
        version = schema_version(check)
        counts = _row_counts(check)
    finally:
        check.close()
    if quick != "ok" or version != expected_version or counts != expected_counts:
        raise MigrationError(
            f"Резервная копия {backup_path} не совпала с базой (проверка: {quick}, версия {version}, "
            f"ожидалась {expected_version}). Возможно, в базу шла запись. Миграция не начиналась, база не изменена; "
            f"повторите при остановленном туннеле."
        )


def upgrade_database(path=None, backup_dir=None, dry_run=False):
    """Явное обновление схемы существующей базы. Возвращает отчёт (словарь).

    Порядок: проверки -> резервная копия и её проверка -> миграции (каждая одной транзакцией) -> проверка базы.
    dry_run только показывает, что будет применено, и не меняет ни базу, ни файлы.
    Отсутствующая база не создаётся.
    """
    db_path = resolve_db_path(path)
    if not db_path.is_file():
        raise DatabaseMissingError(
            f"База данных не найдена: {db_path}. Новая база создаётся при первой записи, обновлять нечего."
        )
    # Сначала только чтение и без каких-либо изменений файла (connect() включает WAL, а это запись в заголовок).
    probe = _open_plain_readonly(db_path)
    try:
        current = schema_version(probe)
        pending = pending_migrations(probe)
        foreign = current == 0 and [t for t in _user_tables(probe) if t != "schema_migrations"]
    finally:
        probe.close()
    report = {
        "database_path": str(db_path), "from_version": current, "to_version": current,
        "pending": [p.name for _, p in pending], "applied": [], "backup": None, "dry_run": bool(dry_run),
    }
    if foreign:
        raise MigrationError(f"Файл {db_path} не похож на базу FITNESS Tracker (нет версии схемы, но есть таблицы).")
    if dry_run or not pending:
        return report

    conn = connect(db_path, create=False)
    try:
        target = pending[-1][0]
        counts_before = _row_counts(conn)
        target_dir = Path(backup_dir) if backup_dir is not None else db_path.parent / "backups"
        backup_path = backup(conn, target_dir, label=f"before-migration-{target:03d}")
        report["backup"] = str(backup_path)
        _verify_backup(backup_path, current, counts_before)

        try:
            report["applied"] = migrate(conn)
            report["to_version"] = schema_version(conn)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise MigrationError(
                f"Обновление схемы не удалось: {exc}. База осталась на версии {schema_version(conn)}, "
                f"резервная копия: {backup_path}"
            ) from None
        report["foreign_key_violations"] = len(violations)
        report["integrity"] = quick
        if violations or quick != "ok" or report["to_version"] != target:
            raise MigrationError(
                f"Проверка после обновления не пройдена (версия {report['to_version']}, целостность {quick}, "
                f"нарушений внешних ключей {len(violations)}). Резервная копия: {backup_path}"
            )
        return report
    finally:
        conn.close()


def backup(conn, backup_dir=None, label=None):
    """Сделать копию базы в новый файл. Существующие копии никогда не перезаписываются."""
    target_dir = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    base = f"fitness-{stamp}{suffix}"
    target = target_dir / f"{base}.db"
    counter = 1
    while target.exists():
        counter += 1
        target = target_dir / f"{base}-{counter}.db"

    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def verify_backup_file(backup_path, table, record_id, updated_at):
    """Проверить копию перед удалением записи. Открывает только файл копии и только для чтения.

    Копия годится, если integrity_check = ok, нарушений внешних ключей нет и удаляемая запись
    (таблица table, id record_id) есть в копии с тем же updated_at. Иначе — BackupError.
    Имя таблицы передаёт вызывающий код из своего словаря, а не из входных данных.
    """
    path = Path(backup_path)
    if not path.is_file():
        raise BackupError(f"Резервная копия не найдена: {path}. Удаление не выполнено.", backup=path)
    try:
        check = _open_plain_readonly(path)
        try:
            integrity = [row[0] for row in check.execute("PRAGMA integrity_check")]
            violations = check.execute("PRAGMA foreign_key_check").fetchall()
            row = check.execute(f'SELECT updated_at FROM "{table}" WHERE id = ?', (record_id,)).fetchone()
        finally:
            check.close()
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"Не удалось проверить резервную копию {path}: {exc}. Удаление не выполнено.",
                          backup=path) from None
    if integrity != ["ok"]:
        raise BackupError(f"Резервная копия {path} повреждена (integrity_check: {'; '.join(integrity[:3])}). "
                          f"Удаление не выполнено.", backup=path)
    if violations:
        raise BackupError(f"В резервной копии {path} нарушены внешние ключи ({len(violations)}). "
                          f"Удаление не выполнено.", backup=path)
    if row is None:
        raise BackupError(f"Удаляемой записи id={record_id} нет в резервной копии {path}. Удаление не выполнено.",
                          backup=path)
    if row[0] != updated_at:
        raise BackupError(f"Запись id={record_id} в резервной копии {path} отличается от текущей "
                          f"(updated_at {row[0]} вместо {updated_at}). Удаление не выполнено.", backup=path)
