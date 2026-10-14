"""Импорт файла выгрузки Health Connect в базу (схема v3).

Гарантии:
- Файл проверяется целиком до записи; любая проблема отклоняет весь файл (health_format).
- Вся запись файла — одна транзакция BEGIN IMMEDIATE: либо импортировано всё, либо ничего.
- Повторный файл (тот же SHA-256 среди успешных импортов) ничего не меняет.
- Записи сопоставляются по (источник, hc_id) и никогда не откатываются на более старую версию:
  устаревшая версия, воспроизведение удалённой записи и конфликт при равном времени изменения не применяются
  (см. _decide и docs/HEALTH_EXPORT_FORMAT.md).
- Отсутствие записи в файле не считается удалением: удаляет только список "deleted", и только записи в области
  этого файла (его источники или его телефон), см. _apply_deletions.
- --dry-run работает на копии базы в памяти: сама база, режим её журнала и связанные файлы не затрагиваются.
- Миграции не запускаются и база не создаётся: нужна существующая база последней версии схемы.
"""

import hashlib
import sqlite3
from pathlib import Path

from . import db, health_format
from .errors import (DatabaseMissingError, ImportFileError, ImportOrderError, MigrationError,
                     MigrationPendingError, TrackerError)

TABLES = {"steps": "step_intervals", "heart_rate": "heart_rate_records", "sleep": "sleep_sessions"}   # константы, не ввод
COUNTERS = ("inserted", "updated", "unchanged", "deleted", "rejected")
# Что не было применено и почему (в отчёте и в выводе команды).
NOT_APPLIED = ("stale", "replay_of_deleted", "conflict", "deletion_out_of_scope", "deletion_stale")
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
MAX_ERROR_TEXT = 4000

_COMMON_COLUMNS = (("client_record_id", "client_record_id"), ("client_record_version", "client_record_version"),
                   ("start_utc", "start"), ("end_utc", "end"), ("start_offset_min", "start_offset_min"),
                   ("end_offset_min", "end_offset_min"), ("local_date", "local_date"))
_KIND_COLUMNS = {"steps": ("count",), "heart_rate": (), "sleep": ("title", "notes")}


def _empty_counts():
    return {t: {c: 0 for c in COUNTERS} for t in health_format.RECORD_TYPES}


def _empty_notes():
    return {k: 0 for k in NOT_APPLIED}


def _warn(warnings, text):
    if len(warnings) < health_format.MAX_WARNINGS:
        warnings.append(text)


def _read(path):
    path = Path(path)
    try:
        if not path.is_file():
            raise ImportFileError([("файл", f"файл не найден: {path}")])
        size = path.stat().st_size
        if size > health_format.MAX_FILE_BYTES:
            raise ImportFileError([("файл", f"файл слишком большой ({size} байт, максимум {health_format.MAX_FILE_BYTES})")])
        return path.read_bytes()
    except ImportFileError:
        raise
    except OSError as exc:
        raise ImportFileError([("файл", f"не удалось прочитать файл {path}: {exc.strerror or exc}")]) from None


# ---------- Подключение к базе ----------

def _resolve(db_path):
    resolved = db.resolve_db_path(db_path)
    if not resolved.is_file():
        raise DatabaseMissingError(f"База данных не найдена: {resolved}. Импорт базу не создаёт.")
    return resolved


def _check_version(conn, resolved):
    current, latest = db.schema_version(conn), db.latest_schema_version()
    if current < latest:
        raise MigrationPendingError(
            f"Схема базы устарела (версия {current}, нужна {latest}). Импорт ничего не менял. "
            "Сначала обновите схему командой migrate_db.py (она делает резервную копию).")
    if current > latest:
        raise MigrationError(f"Версия схемы базы ({current}) новее, чем знает этот код ({latest}). Импорт отменён.")


def _open_ready(db_path):
    """Подключение к существующей базе последней версии схемы для записи. Не создаёт базу и не мигрирует."""
    resolved = _resolve(db_path)
    try:
        conn = db.connect(resolved, create=False)
    except sqlite3.Error as exc:
        raise TrackerError(f"Не удалось открыть базу {resolved}: {exc}") from None
    try:
        _check_version(conn, resolved)
    except sqlite3.Error as exc:
        conn.close()
        raise TrackerError(f"Не удалось прочитать базу {resolved}: {exc}") from None
    except Exception:
        conn.close()
        raise
    return conn


def _open_snapshot(db_path):
    """Копия базы в памяти для --dry-run. Исходная база открывается только на чтение (mode=ro): без PRAGMA,
    без смены режима журнала, без записи в файл базы и в -wal."""
    resolved = _resolve(db_path)
    base = resolved.resolve()
    # Если рядом нет файлов -wal, -shm и -journal, никто базу не использует и в WAL нет данных: читаем с immutable=1,
    # тогда SQLite не создаёт эти файлы и не блокирует. Иначе (база в работе, например MCP-сервером) читаем обычным
    # режимом mode=ro: он видит данные из WAL и не создаёт новых файлов (меняются только байты служебного -shm).
    in_use = any(Path(str(base) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    try:
        source = sqlite3.connect(base.as_uri() + ("?mode=ro" if in_use else "?mode=ro&immutable=1"), uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise TrackerError(f"Не удалось открыть базу {resolved} для чтения: {exc}") from None
    try:
        _check_version(source, resolved)
        snapshot = sqlite3.connect(":memory:", isolation_level=None)
        snapshot.row_factory = sqlite3.Row
        try:
            source.backup(snapshot)
            snapshot.execute("PRAGMA foreign_keys = ON")
        except Exception:
            snapshot.close()
            raise
        return snapshot
    except sqlite3.Error as exc:
        raise TrackerError(f"Не удалось прочитать базу {resolved}: {exc}") from None
    finally:
        source.close()


# ---------- Порядок файлов ----------

def covered_until(conn, phone_id):
    """Самый поздний конец записи (UTC), пришедшей в базу из успешных файлов этого телефона, или None.

    Каждый принятый файл покрывает свой период чтения целиком, а его записи кончаются не позже конца периода.
    Значит, время до этой отметки уже покрыто принятыми файлами: файл, чей период начинается не позже неё,
    не оставляет в базе непокрытого промежутка, даже если файлы между ними пропущены."""
    ends = [conn.execute(
        f"SELECT MAX(r.end_utc) FROM {table} r JOIN import_batches b ON b.id = r.batch_id"
        " WHERE b.phone_id = ? AND b.status = 'ok'", (phone_id,)).fetchone()[0] for table in TABLES.values()]
    ends = [e for e in ends if e]
    return max(ends) if ends else None


def _show_time(canonical):
    return canonical[:16].replace("T", " ") + " UTC" if canonical else "?"


def _check_order(conn, header, allow_gaps, allow_older, warnings):
    """Проверить номер файла. Возвращает сведения о пропуске номеров, если он принят (иначе None)."""
    phone, sequence = header["phone_id"], header["sequence"]
    last = conn.execute("SELECT MAX(sequence) FROM import_batches WHERE phone_id = ? AND status = 'ok'",
                        (phone,)).fetchone()[0]
    if last is None:                                # первый файл телефона принимается с любым номером
        return None
    if sequence == last + 1:
        _check_continuity(conn, header, warnings)
        return None
    if sequence <= last:
        taken = conn.execute("SELECT 1 FROM import_batches WHERE phone_id = ? AND sequence = ? AND status = 'ok'",
                             (phone, sequence)).fetchone()
        if taken:
            raise ImportOrderError(
                f"Файл №{sequence} от телефона {phone} уже принят ранее (с другим содержимым): номер второй раз не "
                "используется. Ничего не изменено.", "taken", phone_id=phone, sequence=sequence, last_sequence=last)
        if allow_older:
            _warn(warnings, f"файл №{sequence} старше последнего принятого №{last}: записи, уже обновлённые более "
                            "новыми файлами, не откатываются, устаревшие удаления не применяются")
            return None
        raise ImportOrderError(
            f"Файл №{sequence} от телефона {phone} старше уже принятого №{last}. Если он был пропущен и ещё не "
            "импортировался, импортируйте его явно ключом --allow-older (записи, которые уже есть в базе, "
            "на старые версии при этом не откатываются). Ничего не изменено.", "older",
            phone_id=phone, sequence=sequence, last_sequence=last)
    missing = list(range(last + 1, sequence))
    gap = {"missing_from": missing[0], "missing_to": missing[-1], "missing_count": len(missing)}
    numbers = f"№{missing[0]}" if len(missing) == 1 else f"№{missing[0]}…№{missing[-1]}"
    if allow_gaps:
        _warn(warnings, f"пропущены файлы {numbers} (разрешено ключом --allow-gaps); если они "
                        "найдутся, импортируйте их позже ключом --allow-older")
        return dict(gap, accepted="allow_gaps")
    covered = covered_until(conn, phone)
    period_start = header.get("period_start")
    if period_start and covered and period_start <= covered:
        _warn(warnings, f"пропущены файлы {numbers}, но этот файл читает данные с {_show_time(period_start)}, а данные "
                        f"телефона в базе уже есть до {_show_time(covered)}: период пропущенных файлов перекрыт, "
                        "ничего не потеряно (изменения старых записей, которые были только в пропущенных файлах, "
                        "придут, если найти и импортировать эти файлы)")
        return dict(gap, accepted="covered", covered_until=covered, period_start=period_start)
    if period_start and covered:
        reason = (f" Данные этого телефона в базе есть до {_show_time(covered)}, а файл начинается с "
                  f"{_show_time(period_start)}: период между ними был только в пропущенных файлах и потеряется. "
                  "Найдите пропущенные файлы или сделайте на телефоне экспорт за период, который начинается не позже "
                  f"{covered[:10]} (например, «Последние 30 дней»).")
    elif covered is None:
        reason = " В базе нет записей этого телефона, по которым можно проверить, что период не потерян."
    else:
        reason = (" В файле не указан период чтения (старая версия приложения), поэтому нельзя проверить, "
                  "перекрывает ли он пропущенные файлы.")
    raise ImportOrderError(
        f"Пропущены файлы от телефона {phone}: после №{last} ожидался №{last + 1}, а в файле №{sequence}.{reason} "
        "Сначала импортируйте пропущенные файлы или, если их нет, повторите с --allow-gaps (найденные позже файлы "
        "импортируются ключом --allow-older). Ничего не изменено.", "gap",
        phone_id=phone, sequence=sequence, last_sequence=last, covered_until=covered, period_start=period_start,
        **gap)


def _check_continuity(conn, header, warnings):
    """Следующий по номеру файл, который начинается позже данных в базе: промежуток никто не выгружал
    (например, экспорт за произвольный период). Это не ошибка, но пользователь должен об этом знать."""
    period_start = header.get("period_start")
    if not period_start:
        return
    covered = covered_until(conn, header["phone_id"])
    if covered and period_start > covered:
        _warn(warnings, f"файл читает данные с {_show_time(period_start)}, а данные телефона в базе есть до "
                        f"{_show_time(covered)}: промежуток между ними не выгружался ни одним принятым файлом")


# ---------- Источники и удаления ----------

def _upsert_sources(conn, parsed):
    used = {rec["source"] for kind in health_format.RECORD_TYPES for rec in parsed.records[kind]}
    ids = {}
    for sid in sorted(used):
        s = parsed.sources[sid]
        conn.execute(
            "INSERT INTO data_sources (source_key, platform, package_name, device_manufacturer, device_model, device_type)"
            " VALUES (?, 'health_connect', ?, ?, ?, ?) ON CONFLICT(source_key) DO NOTHING",
            (s["key"], s["package"], s["manufacturer"], s["model"], s["type"]))
        ids[sid] = conn.execute("SELECT id FROM data_sources WHERE source_key = ?", (s["key"],)).fetchone()[0]
    return ids


def _apply_deletions(conn, parsed, batch_id, counts, notes, warnings):
    """Удаления из списка deleted. Запись попадает под удаление, только если она «своя» для этого файла:
    1) её источник описан в sources файла; если таких записей нет, то
    2) её последний импорт был от того же телефона (phone_id файла).
    Чужая запись (другой телефон и другой источник) не затрагивается и попадает в отчёт. Если id подходит больше чем
    к одной «своей» записи (другие источники или типы), удаление неоднозначно и файл отклоняется целиком.
    Запись, которую более поздний экспорт уже менял, устаревшим удалением не удаляется."""
    header = parsed.header
    declared = {s["key"] for s in parsed.sources.values()}
    unmatched, out_of_scope = [], []
    for hc_id in parsed.deleted:
        rows = []
        for kind, table in TABLES.items():
            for row in conn.execute(
                    f"SELECT t.id AS id, t.deleted_at AS deleted_at, s.source_key AS source_key, "
                    f"b.phone_id AS phone_id, b.exported_at AS last_exported "
                    f"FROM {table} t JOIN data_sources s ON s.id = t.source_id JOIN import_batches b ON b.id = t.batch_id "
                    f"WHERE t.hc_id = ?", (hc_id,)).fetchall():
                rows.append((kind, table, row))
        if not rows:
            unmatched.append(hc_id)
            _remember_pending(conn, header, declared, hc_id, batch_id)
            continue
        # Источник из sources файла точнее телефона: если он подошёл, остальные записи с этим id не рассматриваются.
        scoped = [r for r in rows if r[2]["source_key"] in declared] or                  [r for r in rows if r[2]["phone_id"] == header["phone_id"]]
        if not scoped:
            out_of_scope.append(hc_id)
            notes["deletion_out_of_scope"] += 1
            _warn(warnings, f"удаление {hc_id!r} не применено: запись принадлежит другому телефону и источнику")
            continue
        if len(scoped) > 1:
            where = ", ".join(f"{kind} ({row['source_key']})" for kind, _, row in scoped)
            raise ImportFileError(
                [(f"deleted", f"hc_id {hc_id!r} соответствует нескольким записям: {where}; удаление неоднозначно, "
                              "файл отклонён")], header=header)
        kind, table, row = scoped[0]
        if row["deleted_at"] is not None:
            continue
        if row["last_exported"] > header["exported_at"]:
            notes["deletion_stale"] += 1
            _warn(warnings, f"удаление {hc_id!r} не применено: запись обновлена более поздним экспортом, чем этот файл")
            continue
        conn.execute(f"UPDATE {table} SET deleted_at = {NOW}, batch_id = ? WHERE id = ?", (batch_id, row["id"]))
        counts[kind]["deleted"] += 1
    return unmatched, out_of_scope


# ---------- Записи ----------

def _remember_pending(conn, header, declared, hc_id, batch_id):
    """Удаление записи, которой в базе нет: запомнить его (таблица health_pending_deletions), чтобы старый файл с этой
    записью, найденный позже, её не восстановил. Область та же, что у обычного удаления: источники из sources файла,
    иначе телефон. Повторное удаление того же id хранит самое позднее время экспорта (строка одна)."""
    scopes = [("source_key", key) for key in sorted(declared)] or [("phone_id", header["phone_id"])]
    for column, value in scopes:
        conn.execute(
            f"INSERT INTO health_pending_deletions ({column}, hc_id, batch_id, exported_at) VALUES (?, ?, ?, ?) "
            f"ON CONFLICT ({column}, hc_id) WHERE {column} IS NOT NULL DO UPDATE SET "
            f"batch_id = excluded.batch_id, exported_at = excluded.exported_at "
            f"WHERE excluded.exported_at > health_pending_deletions.exported_at",
            (value, hc_id, batch_id, header["exported_at"]))


def _pending_for(conn, source_id, batch_id, hc_id):
    """Ожидающие удаления, под которые подпадает новая запись: тот же источник или тот же телефон (если удаление
    было без sources). Возвращает (id строк, самое позднее время экспорта удаления)."""
    rows = conn.execute(
        "SELECT p.id AS id, p.exported_at AS exported_at FROM health_pending_deletions p "
        "WHERE p.hc_id = ? AND (p.source_key = (SELECT source_key FROM data_sources WHERE id = ?) "
        "OR p.phone_id = (SELECT phone_id FROM import_batches WHERE id = ?))", (hc_id, source_id, batch_id)).fetchall()
    return [r["id"] for r in rows], max((r["exported_at"] for r in rows), default=None)


def _children(conn, kind, row_id):
    if kind == "heart_rate":
        return {(r["time_utc"], r["bpm"]) for r in
                conn.execute("SELECT time_utc, bpm FROM heart_rate_samples WHERE record_id = ?", (row_id,))}
    return {(r["stage"], r["start_utc"], r["end_utc"]) for r in
            conn.execute("SELECT stage, start_utc, end_utc FROM sleep_stages WHERE session_id = ?", (row_id,))}


def _is_enrichment(conn, kind, row_id, rec):
    """Равное время изменения, другое содержимое: допустимо только если запись лишь пополнилась — все прежние
    поля те же, а прежние отсчёты (стадии) целиком входят в новые. Любая потеря или замена данных — конфликт."""
    if kind == "steps":
        return False
    row = conn.execute(f"SELECT * FROM {TABLES[kind]} WHERE id = ?", (row_id,)).fetchone()
    for column, key in _COMMON_COLUMNS:
        if row[column] != rec[key]:
            return False
    for column in _KIND_COLUMNS[kind]:
        if row[column] != rec[column]:
            return False
    new = set(rec["samples"] if kind == "heart_rate" else rec["stages"])
    return _children(conn, kind, row_id) <= new


def _decide(conn, kind, existing, rec, exported_at):
    """Что делать с записью файла. Возвращает (действие, примечание).

    Действия: insert, update, restore, enrich (применяются); unchanged; stale, replay (не применяются, версия
    устарела); conflict (не применяется, противоречие)."""
    if existing is None:
        return "insert", None
    same = existing["payload_hash"] == rec["payload_hash"]
    older = rec["last_modified"] < existing["hc_last_modified"]
    if existing["deleted_at"] is not None:
        # Удалённую запись возвращает только версия, которая не старше сохранённой и пришла в экспорте,
        # сделанном после экспорта, в котором удаление было принято. Иначе это повтор старых данных.
        if older:
            return "replay", "версия старше сохранённой"
        if not exported_at > existing["batch_exported_at"]:
            return "replay", "файл экспортирован не позже удаления"
        return ("restore" if same else "update"), None
    if same:
        return "unchanged", None
    if older:
        return "stale", None
    if rec["last_modified"] == existing["hc_last_modified"]:
        if _is_enrichment(conn, kind, existing["id"], rec):
            return "enrich", None
        return "conflict", None
    return "update", None


def _common_values(rec, batch_id):
    return [rec["client_record_id"], rec["client_record_version"], rec["start"], rec["end"],
            rec["start_offset_min"], rec["end_offset_min"], rec["local_date"], rec["last_modified"],
            rec["payload_hash"], batch_id]


def _write_record(conn, kind, rec, source_id, batch_id, action, row_id):
    """Вставка или обновление одной записи вместе с дочерними строками. Возвращает id записи."""
    table = TABLES[kind]
    if kind == "steps":
        extra_cols, extra_vals = ["count"], [rec["count"]]
    elif kind == "sleep":
        extra_cols, extra_vals = ["title", "notes"], [rec["title"], rec["notes"]]
    else:
        extra_cols, extra_vals = [], []
    cols = ["client_record_id", "client_record_version", "start_utc", "end_utc", "start_offset_min", "end_offset_min",
            "local_date", "hc_last_modified", "payload_hash", "batch_id"] + extra_cols
    values = _common_values(rec, batch_id) + extra_vals
    if action == "insert":
        names = ", ".join(["source_id", "hc_id"] + cols)
        marks = ", ".join("?" for _ in range(len(cols) + 2))
        row_id = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})",
                              [source_id, rec["hc_id"]] + values).lastrowid
    else:
        assignments = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(f"UPDATE {table} SET {assignments}, revision = revision + 1, deleted_at = NULL WHERE id = ?",
                     values + [row_id])
    if kind == "heart_rate":
        conn.execute("DELETE FROM heart_rate_samples WHERE record_id = ?", (row_id,))
        conn.executemany("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, ?)",
                         [(row_id, t, bpm) for t, bpm in rec["samples"]])
    elif kind == "sleep":
        conn.execute("DELETE FROM sleep_stages WHERE session_id = ?", (row_id,))
        conn.executemany("INSERT INTO sleep_stages (session_id, stage, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                         [(row_id, stage, a, b) for stage, a, b in rec["stages"]])
    return row_id


def _upsert_records(conn, kind, records, source_ids, batch_id, exported_at, counts, notes, warnings):
    table = TABLES[kind]
    for rec in records:
        source_id = source_ids[rec["source"]]
        existing = conn.execute(
            f"SELECT t.id AS id, t.payload_hash AS payload_hash, t.hc_last_modified AS hc_last_modified, "
            f"t.deleted_at AS deleted_at, b.exported_at AS batch_exported_at "
            f"FROM {table} t JOIN import_batches b ON b.id = t.batch_id WHERE t.source_id = ? AND t.hc_id = ?",
            (source_id, rec["hc_id"])).fetchone()
        action, reason = _decide(conn, kind, existing, rec, exported_at)
        pending_ids = []
        if action == "insert":
            pending_ids, pending_at = _pending_for(conn, source_id, batch_id, rec["hc_id"])
            if pending_ids and not exported_at > pending_at:
                # Запись удалена ранее (удаление пришло раньше неё), а этот файл экспортирован не позже удаления.
                action, reason = "replay", "ожидающее удаление: файл экспортирован не позже удаления"
        if action == "insert":
            _write_record(conn, kind, rec, source_id, batch_id, "insert", None)
            if pending_ids:      # файл экспортирован после удаления: запись действительно вернулась, удаление отработало
                conn.execute(f"DELETE FROM health_pending_deletions WHERE id IN ({','.join('?' * len(pending_ids))})", pending_ids)
            counts[kind]["inserted"] += 1
        elif action in ("update", "restore", "enrich"):
            if action == "update" and kind != "steps":
                before = len(_children(conn, kind, existing["id"]))
                after = len(rec["samples"] if kind == "heart_rate" else rec["stages"])
                if after < before:
                    _warn(warnings, f"{kind}: в записи {rec['hc_id']!r} стало меньше данных ({before} → {after}): "
                                    "новая версия заменила прежнюю")
            _write_record(conn, kind, rec, source_id, batch_id, "update", existing["id"])
            counts[kind]["updated"] += 1
        elif action == "conflict":
            counts[kind]["rejected"] += 1
            notes["conflict"] += 1
            _warn(warnings, f"{kind}: запись {rec['hc_id']!r} не применена: то же время изменения, но содержимое "
                            "отличается и заменило бы существующие данные (конфликт)")
        else:
            counts[kind]["unchanged"] += 1
            if action == "unchanged" and rec["last_modified"] > existing["hc_last_modified"]:
                # Содержимое то же, но версия уже видена с более поздним временем: запоминаем его, чтобы пропущенный
                # позже файл с промежуточным временем не откатил запись (порядок файлов и актуальность записи разделены).
                conn.execute(f"UPDATE {table} SET hc_last_modified = ? WHERE id = ?", (rec["last_modified"], existing["id"]))
            if action == "unchanged" and exported_at > existing["batch_exported_at"]:
                # Более поздний экспорт подтвердил запись: привязываем её к этому батчу, чтобы пропущенное позже удаление
                # из более раннего файла (--allow-older) не сочло её устаревшей и не удалило. Никогда назад по времени.
                conn.execute(f"UPDATE {table} SET batch_id = ? WHERE id = ?", (batch_id, existing["id"]))
            if action == "stale":
                notes["stale"] += 1
                _warn(warnings, f"{kind}: запись {rec['hc_id']!r} в файле старше сохранённой, не применена")
            elif action == "replay":
                notes["replay_of_deleted"] += 1
                _warn(warnings, f"{kind}: запись {rec['hc_id']!r} удалена и не восстановлена ({reason})")


# ---------- Один файл ----------

class _DryRun(Exception):
    """Служебное исключение: откатить транзакцию пробного запуска."""


def _import_in_transaction(conn, parsed, file_name, allow_gaps, allow_older, dry_run):
    warnings = list(parsed.warnings)
    counts = _empty_counts()
    notes = _empty_notes()
    unmatched, out_of_scope = [], []
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = conn.execute("SELECT id FROM import_batches WHERE sha256 = ? AND status = 'ok'", (parsed.sha256,)).fetchone()
        if done:
            conn.execute("ROLLBACK")
            return {"status": "already_imported", "batch_id": done[0], "counts": counts, "notes": notes,
                    "unmatched": [], "out_of_scope": [], "warnings": warnings, "gap": None}
        gap = _check_order(conn, parsed.header, allow_gaps, allow_older, warnings)
        source_ids = _upsert_sources(conn, parsed)
        h = parsed.header
        batch_id = conn.execute(
            "INSERT INTO import_batches (file_name, sha256, format_version, phone_id, sequence, exported_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'ok')",
            (file_name, parsed.sha256, h["format_version"], h["phone_id"], h["sequence"], h["exported_at"])).lastrowid
        unmatched, out_of_scope = _apply_deletions(conn, parsed, batch_id, counts, notes, warnings)
        for kind in health_format.RECORD_TYPES:
            _upsert_records(conn, kind, parsed.records[kind], source_ids, batch_id, h["exported_at"], counts, notes, warnings)
        conn.executemany(
            "INSERT INTO import_batch_counts (batch_id, record_type, inserted, updated, unchanged, deleted, rejected)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(batch_id, kind) + tuple(counts[kind][c] for c in COUNTERS) for kind in health_format.RECORD_TYPES])
        if dry_run:
            raise _DryRun()
        conn.execute("COMMIT")
        return {"status": "imported", "batch_id": batch_id, "counts": counts, "notes": notes, "unmatched": unmatched,
                "out_of_scope": out_of_scope, "warnings": warnings, "gap": gap}
    except _DryRun:
        conn.execute("ROLLBACK")
        return {"status": "dry_run", "batch_id": None, "counts": counts, "notes": notes, "unmatched": unmatched,
                "out_of_scope": out_of_scope, "warnings": warnings, "gap": gap}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _journal_failure(db_path, header, sha256, file_name, error, rejected=None):
    """Записать неудачную попытку в журнал отдельной транзакцией (после отката). Возвращает id или None.
    Повтор той же неудачи (тот же файл и тот же текст ошибки) новую строку не создаёт."""
    if not header or not header.get("phone_id") or not header.get("sequence") or not header.get("exported_at"):
        return None
    error = error[:MAX_ERROR_TEXT]
    try:
        conn = _open_ready(db_path)
    except TrackerError:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            same = conn.execute("SELECT id FROM import_batches WHERE status = 'failed' AND sha256 = ? AND error = ?",
                                (sha256, error)).fetchone()
            if same:
                conn.execute("ROLLBACK")
                return same[0]
            batch_id = conn.execute(
                "INSERT INTO import_batches (file_name, sha256, format_version, phone_id, sequence, exported_at, status, error)"
                " VALUES (?, ?, ?, ?, ?, ?, 'failed', ?)",
                (file_name, sha256, header["format_version"], header["phone_id"], header["sequence"],
                 header["exported_at"], error)).lastrowid
            rejected = rejected or {}
            conn.executemany(
                "INSERT INTO import_batch_counts (batch_id, record_type, rejected) VALUES (?, ?, ?)",
                [(batch_id, kind, rejected.get(kind, 0)) for kind in health_format.RECORD_TYPES])
            conn.execute("COMMIT")
            return batch_id
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except (sqlite3.Error, UnicodeError):
        return None
    finally:
        conn.close()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _display_name(path):
    """Имя файла для журнала: символы, которые нельзя записать в базу, заменяются."""
    return Path(path).name.encode("utf-8", "replace").decode("utf-8")


def import_file(file_path, db_path=None, *, dry_run=False, allow_gaps=False, allow_older=False):
    """Импортировать один файл. Возвращает отчёт (словарь) или выбрасывает TrackerError.

    dry_run: те же шаги на копии базы в памяти; исходная база открывается только на чтение, журнал не пишется.
    allow_gaps: разрешить пропуск номеров файлов. allow_older: принять пропущенный ранее файл с номером меньше
    последнего принятого. Неудача после успешной проверки заголовка записывается в журнал со статусом failed.
    """
    return import_bytes(_read(file_path), _display_name(file_path), db_path, dry_run=dry_run, allow_gaps=allow_gaps,
                        allow_older=allow_older)


def _data_range(parsed):
    """Диапазон данных файла: самое раннее начало и самый поздний конец записей (UTC) и местные даты записей."""
    starts, ends, dates = [], [], []
    for kind in health_format.RECORD_TYPES:
        for rec in parsed.records[kind]:
            starts.append(rec["start"])
            ends.append(rec["end"])
            dates.append(rec["local_date"])
    if not starts:
        return None
    return {"start_utc": min(starts), "end_utc": max(ends), "local_date_from": min(dates), "local_date_to": max(dates)}


def import_bytes(data, file_name, db_path=None, *, dry_run=False, allow_gaps=False, allow_older=False):
    """То же, что import_file, но содержимое уже в памяти (например, получено через MCP). file_name — только для
    журнала и отчёта; путь на диске не используется."""
    if len(data) > health_format.MAX_FILE_BYTES:
        raise ImportFileError([("файл", f"файл слишком большой ({len(data)} байт, максимум {health_format.MAX_FILE_BYTES})")])
    file_name = str(file_name).encode("utf-8", "replace").decode("utf-8") or "health-export.json"
    try:
        parsed = health_format.parse_export(data)
    except ImportFileError as exc:
        if not dry_run:
            exc.journal_batch_id = _journal_failure(db_path, exc.header, _sha(data), file_name, str(exc), exc.rejected)
        raise

    conn = _open_snapshot(db_path) if dry_run else _open_ready(db_path)
    try:
        try:
            result = _import_in_transaction(conn, parsed, file_name, allow_gaps, allow_older, dry_run)
        except TrackerError as exc:
            conn.close()
            conn = None
            if not dry_run:
                exc.journal_batch_id = _journal_failure(db_path, parsed.header, parsed.sha256, file_name, str(exc))
            raise
        except (sqlite3.Error, UnicodeError) as exc:
            conn.close()
            conn = None
            if not dry_run:
                _journal_failure(db_path, parsed.header, parsed.sha256, file_name, f"Ошибка при импорте: {exc}")
            raise TrackerError(f"Ошибка при импорте, ничего не сохранено: {exc}") from None
    finally:
        if conn is not None:
            conn.close()

    h = parsed.header
    return {"status": result["status"], "file": file_name, "sha256": parsed.sha256, "phone_id": h["phone_id"],
            "sequence": h["sequence"], "exported_at": h["exported_at"], "data_range": _data_range(parsed),
            "batch_id": result["batch_id"], "counts": result["counts"],
            "not_applied": result["notes"],
            "unmatched_deletions": len(result["unmatched"]), "unmatched_ids": result["unmatched"][:20],
            "out_of_scope_deletions": len(result["out_of_scope"]), "out_of_scope_ids": result["out_of_scope"][:20],
            "warnings": result["warnings"], "dry_run": bool(dry_run), "gap": result["gap"],
            "period": {"start_utc": h["period_start"], "end_utc": h["period_end"]} if h.get("period_start") else None}
