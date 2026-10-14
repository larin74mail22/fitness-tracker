"""Передача файлов из папки Syncthing (data\\health_syncthing) во входящую папку импорта (data\\health_inbox).

Зачем: папка Syncthing на компьютере имеет тип «Только приём». Любое изменение в ней (перенос, удаление,
переименование, новый файл) Syncthing считает «локальным изменением» и останавливается с кнопкой «Отменить
локальные изменения». Поэтому эта папка только читается: готовые файлы КОПИРУЮТСЯ во входящую папку, а дальше
работает обычный импорт (health_inbox.process_inbox) — он и переносит копию в health_processed / health_failed.

Гарантии:
- в папке Syncthing ничего не создаётся, не меняется и не удаляется (файлы открываются только на чтение);
- файл, который ещё пишется (свежий, меняется размер или время, недописанный JSON моложе минуты), не копируется;
- копия пишется под временным именем «.<имя>.handoff.tmp» (импорт такие имена пропускает) и переименовывается,
  когда записана целиком и проверена по SHA-256; время изменения копии — как у исходного файла;
- одинаковое содержимое передаётся один раз: журнал передачи health_sync/syncthing_handoff.json хранит SHA-256
  каждого переданного файла; файл, уже импортированный в базу, не копируется вовсе; после сбоя между копированием
  и записью журнала копия узнаётся во входящей папке по SHA-256 и не дублируется;
- одинаковые имена с разным содержимым: копия получает свободное имя (как при переносе в health_processed);
- передача выполняется под той же блокировкой health_sync/inbox.lock, что и импорт: параллельно один файл не
  передаётся и не импортируется дважды;
- журнал повреждён или удалён — не страшно: повторы отсеивает проверка базы и импортёр (SHA-256).
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from . import health_format

SOURCE = "health_syncthing"
LEDGER_FILE = "syncthing_handoff.json"
TMP_SUFFIX = ".handoff.tmp"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Журнал передачи ----------

def load_ledger(state_dir):
    """{sha256: {name, size, mtime_ns, result, handed_at, inbox_name}}. Повреждённый журнал сохраняется рядом
    (.corrupt-<время>) и начинается заново."""
    path = state_dir / LEDGER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data.get("files")
        if isinstance(files, dict):
            return files
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        os.replace(path, path.with_name(f"{LEDGER_FILE}.corrupt-{datetime.now().strftime('%Y%m%dT%H%M%S')}"))
    except OSError:
        pass
    return {}


def save_ledger(state_dir, files):
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / LEDGER_FILE
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "files": files}, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _known_keys(ledger):
    """Быстрая проверка без чтения файла: (имя, размер, время изменения) уже переданных файлов."""
    keys = set()
    for entry in ledger.values():
        keys.add((entry.get("name"), entry.get("size"), entry.get("mtime_ns")))
        for alias in entry.get("aliases", []):
            keys.add(tuple(alias))
    return keys


def _key(path):
    st = path.stat()
    return (path.name, st.st_size, st.st_mtime_ns)


def pending_count(source, state_dir):
    """Сколько файлов в папке Syncthing ещё не переданы (по журналу, без чтения содержимого). Только чтение."""
    from .health_inbox import list_inbox
    known = _known_keys(_read_ledger_quiet(state_dir))
    count = 0
    for path in list_inbox(source):
        try:
            if _key(path) not in known:
                count += 1
        except OSError:
            count += 1
    return count


def _read_ledger_quiet(state_dir):
    try:
        files = json.loads((state_dir / LEDGER_FILE).read_text(encoding="utf-8")).get("files")
        return files if isinstance(files, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


# ---------- Готовность исходного файла ----------

def _stamp(path):
    st = path.stat()
    # Время создания (Windows) или смены метаданных (Linux): переименование временного файла Syncthing его обновляет,
    # а время изменения Syncthing переносит с телефона, и оно может быть старым.
    return st.st_size, st.st_mtime_ns, max(st.st_mtime, st.st_ctime)


def _sha_in_db(conn, sha):
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM import_batches WHERE sha256 = ? AND status = 'ok' LIMIT 1",
                            (sha,)).fetchone() is not None
    except Exception:                                  # noqa: BLE001 — проверка лишь экономит копирование
        return False


def _open_db(db_path):
    from .web.readonly import DatabaseUnavailable, connect_readonly
    try:
        return connect_readonly(db_path)
    except DatabaseUnavailable:
        return None


def _looks_incomplete(data):
    try:
        json.loads(data.decode("utf-8-sig"))
        return False
    except (UnicodeDecodeError, ValueError):
        return True


def _inbox_hashes(inbox):
    from .health_inbox import list_inbox
    result = {}
    for path in list_inbox(inbox):
        try:
            if path.stat().st_size <= health_format.MAX_FILE_BYTES:
                result[hashlib.sha256(path.read_bytes()).hexdigest()] = path.name
        except OSError:
            continue
    return result


def _copy(data, source_stat, target):
    """Записать копию под временным именем и переименовать, когда она целиком на диске."""
    tmp = target.with_name(f".{target.name}{TMP_SUFFIX}")
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.utime(tmp, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
            raise OSError("копия не совпала с исходным файлом")
        if target.exists():
            raise FileExistsError(f"{target.name} появился во входящей папке во время копирования")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------- Передача ----------

def transfer(dirs, db_path=None, *, dry_run=False, clock, sleep, min_age, stable_wait, incomplete_grace):
    """Скопировать новые готовые файлы из dirs["syncthing"] в dirs["inbox"]. Вызывать под inbox.lock.

    Возвращает (отчёт, пробные_пути). В пробном запуске ничего не копируется и журнал не пишется; пробные_пути —
    готовые непереданные файлы папки Syncthing, которые импорт проверит на месте (только чтение).
    """
    from .health_inbox import _unique, list_inbox
    source = dirs["syncthing"]
    report = {"folder": str(source), "exists": source.is_dir(), "copied": [], "already_in_db": [], "known": 0,
              "busy": [], "errors": [], "would_copy": []}
    if not report["exists"]:
        return report, []
    ledger = _read_ledger_quiet(dirs["state"]) if dry_run else load_ledger(dirs["state"])
    known = _known_keys(ledger)
    fresh = []
    for path in list_inbox(source):
        try:
            if _key(path) in known:
                report["known"] += 1
                continue
            fresh.append(path)
        except OSError:
            continue                                   # файл исчез между чтением папки и проверкой
    if not fresh:
        return report, []

    first = {}
    for path in fresh:
        try:
            first[path] = _stamp(path)
        except OSError:
            first[path] = None
    if stable_wait:
        sleep(stable_wait)
    conn = _open_db(db_path)
    inbox_hashes = None
    dry_paths = []
    try:
        for path in fresh:
            name = path.name
            try:
                stamp = _stamp(path)
                if first[path] is None or first[path][:2] != stamp[:2] or clock() - stamp[2] < min_age:
                    report["busy"].append({"file": name, "reason": "файл ещё записывается Syncthing"})
                    continue
                if stamp[0] > health_format.MAX_FILE_BYTES:
                    data = None
                else:
                    with open(path, "rb") as fh:
                        data = fh.read()
                    if _stamp(path)[:2] != stamp[:2] or len(data) != stamp[0]:
                        report["busy"].append({"file": name, "reason": "файл изменился во время чтения"})
                        continue
            except OSError as exc:
                report["busy"].append({"file": name, "reason": f"файл недоступен для чтения ({exc})"})
                continue
            if data is None:
                # Слишком большой файл копировать не нужно: импорт его всё равно отклонит. Ошибка видна в отчёте.
                report["errors"].append({"file": name, "error": f"файл больше {health_format.MAX_FILE_BYTES} байт, "
                                                                  "не передан на импорт"})
                continue
            if _looks_incomplete(data) and clock() - stamp[2] < incomplete_grace:
                report["busy"].append({"file": name, "reason": "недописанный JSON, проверю позже"})
                continue
            sha = hashlib.sha256(data).hexdigest()
            entry = {"name": name, "size": stamp[0], "mtime_ns": stamp[1], "sha256": sha}
            if sha in ledger:
                # То же содержимое уже передавалось (повторная синхронизация или другое имя): запомнить и пропустить.
                if not dry_run:
                    old = ledger[sha]
                    alias = [name, stamp[0], stamp[1]]
                    if alias not in old.setdefault("aliases", []) and \
                            (old.get("name"), old.get("size"), old.get("mtime_ns")) != tuple(alias):
                        old["aliases"].append(alias)
                    _save_or_report(dirs, ledger, report, name)
                report["known"] += 1
                continue
            if _sha_in_db(conn, sha):
                report["already_in_db"].append(name)
                if not dry_run:
                    ledger[sha] = entry | {"result": "already_in_db", "handed_at": _now(), "inbox_name": None}
                    _save_or_report(dirs, ledger, report, name)
                continue
            if dry_run:
                report["would_copy"].append(name)
                dry_paths.append(path)
                continue
            if inbox_hashes is None:
                inbox_hashes = _inbox_hashes(dirs["inbox"])
            if sha in inbox_hashes:
                # Копия уже лежит во входящей папке (сбой до записи журнала): второй не нужно.
                ledger[sha] = entry | {"result": "found_in_inbox", "handed_at": _now(),
                                       "inbox_name": inbox_hashes[sha]}
                _save_or_report(dirs, ledger, report, name)
                report["copied"].append({"file": name, "inbox_name": inbox_hashes[sha], "existing_copy": True})
                continue
            try:
                dirs["inbox"].mkdir(parents=True, exist_ok=True)
                target = _unique(dirs["inbox"] / name)
                _copy(data, os.stat(path), target)
            except OSError as exc:
                report["errors"].append({"file": name, "error": f"не удалось скопировать во входящую папку: {exc}; "
                                                                "попытка повторится при следующей проверке"})
                continue
            inbox_hashes[sha] = target.name
            ledger[sha] = entry | {"result": "copied", "handed_at": _now(), "inbox_name": target.name}
            _save_or_report(dirs, ledger, report, name)
            report["copied"].append({"file": name, "inbox_name": target.name})
    finally:
        if conn is not None:
            conn.close()
    return report, dry_paths


def _save_or_report(dirs, ledger, report, name):
    try:
        save_ledger(dirs["state"], ledger)
    except OSError as exc:
        # Копия уже во входящей папке; без журнала повтор отсеют проверка базы и поиск копии по SHA-256.
        report["errors"].append({"file": name, "error": f"не удалось записать журнал передачи: {exc}"})


def describe(report):
    """Строки для отчёта командной строки (пусто, если папки Syncthing нет или передавать нечего)."""
    if not report or not report.get("exists"):
        return []
    lines = []
    for item in report["copied"]:
        lines.append(f"Syncthing → входящая: {item['file']}"
                     + (f" (как {item['inbox_name']})" if item["inbox_name"] != item["file"] else "")
                     + (" — копия уже была во входящей папке" if item.get("existing_copy") else ""))
    for name in report["already_in_db"]:
        lines.append(f"Syncthing: {name} уже есть в базе, не передаётся")
    for name in report["would_copy"]:
        lines.append(f"Syncthing: {name} будет передан на импорт")
    for item in report["busy"]:
        lines.append(f"Syncthing: {item['file']} — {item['reason']}")
    for item in report["errors"]:
        lines.append(f"Syncthing: ОШИБКА {item['file']}: {item['error']}")
    return lines
