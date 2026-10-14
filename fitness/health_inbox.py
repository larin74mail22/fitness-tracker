"""Автоматический импорт выгрузок Health Connect из входящей папки.

Папки (рядом с базой, по умолчанию <папка проекта>\\data):
- health_syncthing — папка Syncthing «Только приём». Только читается: готовые файлы копируются в health_inbox
                     (health_handoff.py), сама папка не меняется, иначе Syncthing требует «Отменить локальные
                     изменения»;
- health_inbox     — сюда попадают новые файлы *.json (копии из health_syncthing, вручную, через adb и т. п.);
- health_processed — файлы, которые уже есть в базе (импортированы сейчас или раньше);
- health_failed    — файлы, которые не прошли проверку; рядом лежит <имя>.error.txt с причиной;
- health_sync      — отчёт последнего запуска (last_run.json), журнал запусков (runs.log), отметка проверки.

Гарантии:
- импорт идёт только через health_import.import_file (тот же проверенный импортёр, что у import_health.py и MCP):
  проверка файла целиком, одна транзакция, защита от повторов и устаревших данных;
- файл перемещается только ПОСЛЕ завершения импорта; сбой между импортом и перемещением безопасен: при следующем
  запуске файл узнаётся по SHA-256 (already_imported) и просто переносится в health_processed;
- файлы не удаляются: ни после импорта, ни после ошибки;
- файл, который ещё копируется (свежее время изменения, меняется размер, занят другим процессом, или недописанный
  JSON, изменённый меньше минуты назад), пропускается до следующего запуска;
- ошибка одного файла не останавливает остальные; при проблеме с самой базой (нет базы, нужна миграция, база
  занята) запуск останавливается, и все файлы остаются во входящей папке;
- файлы с пропуском номеров, которые нельзя принять без потери периода, остаются во входящей папке («ожидают»):
  вдруг пропущенный файл ещё придёт;
- одновременно работает только один обработчик (блокировка health_sync/inbox.lock).
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, health_format, health_handoff, health_import
from .errors import (DatabaseMissingError, ImportFileError, ImportOrderError, MigrationError, MigrationPendingError,
                     TrackerError)

INBOX, PROCESSED, FAILED, STATE = "health_inbox", "health_processed", "health_failed", "health_sync"
LAST_RUN_FILE, RUNS_LOG, CHECK_FILE, LOCK_FILE = "last_run.json", "runs.log", "last_check.json", "inbox.lock"
MAX_LOG_BYTES = 2 * 1024 * 1024
MIN_AGE_SEC = 5            # файл, изменённый позже, считается копирующимся
STABLE_WAIT_SEC = 1.0      # пауза между двумя замерами размера
INCOMPLETE_GRACE_SEC = 60  # недописанный JSON моложе этого возраста не считается ошибкой: его, скорее всего, ещё копируют
DEFAULT_INTERVAL_SEC = 30
# Временные файлы программ копирования и синхронизации (Syncthing: .syncthing.*.tmp; браузеры: .crdownload).
TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", ".!sync", ".filepart")

OUTCOME_TEXT = {
    "imported": "импортирован", "already_imported": "уже был в базе", "dry_run": "пробный запуск",
    "failed": "ошибка, перенесён в health_failed", "held": "ожидает (остался во входящей папке)",
    "busy": "ещё копируется, обработка позже",
}


class InboxLocked(TrackerError):
    code = "inbox_locked"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def folders(db_path=None):
    data_dir = db.resolve_db_path(db_path).parent
    return {"data": data_dir, "syncthing": data_dir / health_handoff.SOURCE, "inbox": data_dir / INBOX,
            "processed": data_dir / PROCESSED, "failed": data_dir / FAILED, "state": data_dir / STATE}


def ensure_folders(db_path=None):
    dirs = folders(db_path)
    for key in ("inbox", "processed", "failed", "state"):
        dirs[key].mkdir(parents=True, exist_ok=True)
    return dirs


def is_candidate(path):
    name = path.name.lower()
    return (path.is_file() and name.endswith(".json") and not name.startswith((".", "~"))
            and not name.endswith(TEMP_SUFFIXES))


def list_inbox(inbox):
    """Файлы *.json во входящей папке (без подпапок и временных файлов)."""
    if not inbox.is_dir():
        return []
    return sorted((p for p in inbox.iterdir() if is_candidate(p)), key=lambda p: p.name)


# ---------- Блокировка: один обработчик одновременно ----------

class _Lock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise InboxLocked("Входящую папку уже обрабатывает другой запуск (например, режим наблюдения в другом "
                              "окне). Дождитесь его окончания или закройте то окно.") from None
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.handle.close()
            self.handle = None


# ---------- Готовность файла ----------

def _snapshot(paths):
    result = {}
    for p in paths:
        try:
            st = p.stat()
            result[p] = (st.st_size, st.st_mtime_ns, st.st_mtime)
        except OSError:
            result[p] = None
    return result


def _in_use(path):
    """True, если файл открыт другим процессом на запись (Windows не даёт открыть его на запись)."""
    if os.name != "nt":
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        try:
            return os.access(path, os.W_OK)     # файл «только для чтения» не считается занятым
        except OSError:
            return True
    except OSError:
        return True


def split_ready(paths, clock=time.time, sleep=time.sleep, min_age=MIN_AGE_SEC, stable_wait=STABLE_WAIT_SEC):
    """(готовые, копирующиеся). Готов файл, который не менялся min_age секунд, не изменился за паузу
    stable_wait и не занят другим процессом."""
    if not paths:
        return [], []
    first = _snapshot(paths)
    if stable_wait:
        sleep(stable_wait)
    second = _snapshot(paths)
    now = clock()
    ready, busy = [], []
    for p in paths:
        a, b = first[p], second[p]
        if a is None or b is None or a[:2] != b[:2] or now - b[2] < min_age or _in_use(p):
            busy.append(p)
        else:
            ready.append(p)
    return ready, busy


def _order_key(path):
    """Порядок обработки: телефон, номер файла, имя. Нечитаемые файлы — в конце (дадут понятную ошибку)."""
    order = None
    try:
        if path.stat().st_size <= health_format.MAX_FILE_BYTES:
            order = health_format.peek_order(path.read_bytes())
    except OSError:
        order = None
    return (order is None, order[0] if order else "", order[1] if order else 0, path.name)


# ---------- Перемещение ----------

def _unique(target):
    if not target.exists():
        return target
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    for n in range(1, 1000):
        candidate = target.with_name(f"{target.stem}__{stamp}{'' if n == 1 else f'_{n}'}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"не удалось подобрать свободное имя для {target.name}")


def _move(path, directory):
    directory.mkdir(parents=True, exist_ok=True)
    target = _unique(directory / path.name)
    os.replace(path, target)
    return target


def _write_error_note(target, text):
    note = target.with_name(target.name + ".error.txt")
    try:
        note.write_text(f"{_now()}\n{text}\n", encoding="utf-8")
    except OSError:
        pass


# ---------- Один запуск ----------

def _totals(report):
    return {c: sum(report["counts"][k][c] for k in health_format.RECORD_TYPES) for c in health_import.COUNTERS}


def _age(path, clock):
    try:
        return clock() - path.stat().st_mtime
    except OSError:
        return 0


def _looks_incomplete(exc):
    return any(path == "файл" and ("JSON" in message or "пуст" in message) for path, message in exc.problems)


def _is_transient_db_error(exc):
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _process_one(path, db_path, dirs, dry_run, allow_gaps, allow_older, clock):
    item = {"file": path.name}
    try:
        report = health_import.import_file(path, db_path, dry_run=dry_run, allow_gaps=allow_gaps,
                                           allow_older=allow_older)
    except ImportFileError as exc:
        if _looks_incomplete(exc) and _age(path, clock) < INCOMPLETE_GRACE_SEC:
            item.update(outcome="busy", error="файл ещё не дописан (недописанный JSON), проверю позже")
            return item
        item.update(outcome="failed", error_code=exc.code, error=str(exc))
    except ImportOrderError as exc:
        item.update(error_code=exc.code, error=str(exc), order_problem=exc.kind,
                    **{k: v for k, v in exc.details.items() if k in ("phone_id", "sequence", "last_sequence",
                                                                     "missing_from", "missing_to", "covered_until",
                                                                     "period_start")})
        item["outcome"] = "failed" if exc.kind == "taken" else "held"
    except (DatabaseMissingError, MigrationPendingError, MigrationError):
        raise
    except TrackerError as exc:
        if _is_transient_db_error(exc):
            raise
        item.update(outcome="failed", error_code=exc.code, error=str(exc))
    except Exception as exc:                          # noqa: BLE001 — файл сохраняется для диагностики
        item.update(outcome="failed", error_code="unexpected", error=f"{type(exc).__name__}: {exc}")
    else:
        item.update(outcome=report["status"], batch_id=report["batch_id"], phone_id=report["phone_id"],
                    sequence=report["sequence"], exported_at=report["exported_at"], data_range=report["data_range"],
                    totals=_totals(report), warnings=report["warnings"], gap=report.get("gap"),
                    conflicts=report["not_applied"].get("conflict", 0))
        if not dry_run:
            try:
                item["moved_to"] = str(_move(path, dirs["processed"]))
            except OSError as exc:
                # Данные уже в базе; файл занят (например, его держит программа синхронизации). При следующем
                # запуске он будет узнан как already_imported и перенесён.
                item["warnings"] = list(item["warnings"] or []) + [
                    f"файл импортирован, но не перенесён в {PROCESSED} ({exc}); перенос будет при следующем запуске"]
        return item
    if item["outcome"] == "failed" and not dry_run:
        try:
            target = _move(path, dirs["failed"])
        except OSError as exc:
            item["error"] += f"\n(файл не удалось перенести в {FAILED}: {exc}; он остался во входящей папке)"
            return item
        _write_error_note(target, item["error"])
        item["moved_to"] = str(target)
    return item


def process_inbox(db_path=None, *, dry_run=False, allow_gaps=False, allow_older=False, clock=time.time,
                  sleep=time.sleep, min_age=MIN_AGE_SEC, stable_wait=STABLE_WAIT_SEC):
    """Обработать все готовые файлы входящей папки. Возвращает отчёт запуска.

    dry_run: только проверка на копии базы, файлы не перемещаются, отчёт на диск не пишется.
    """
    dirs = folders(db_path) if dry_run else ensure_folders(db_path)
    run = {"started_at": _now(), "finished_at": None, "dry_run": bool(dry_run), "files": [], "error": None,
           "inbox": str(dirs["inbox"]), "handoff": None}
    lock = _Lock(dirs["state"] / LOCK_FILE) if not dry_run else None
    try:
        if lock:
            lock.__enter__()
        # Сначала копии новых файлов из папки Syncthing (сама она не меняется), затем обычный импорт входящей папки.
        try:
            run["handoff"], dry_paths = health_handoff.transfer(
                dirs, db_path, dry_run=dry_run, clock=clock, sleep=sleep, min_age=min_age, stable_wait=stable_wait,
                incomplete_grace=INCOMPLETE_GRACE_SEC)
        except OSError as exc:
            run["handoff"], dry_paths = {"folder": str(dirs["syncthing"]), "exists": True, "copied": [],
                                         "already_in_db": [], "known": 0, "busy": [], "would_copy": [],
                                         "errors": [{"file": "*", "error": f"папка Syncthing недоступна: {exc}"}]}, []
        candidates = list_inbox(dirs["inbox"])
        ready, busy = split_ready(candidates, clock=clock, sleep=sleep, min_age=min_age, stable_wait=stable_wait)
        ready += dry_paths          # только пробный запуск: проверить файлы Syncthing на месте, не копируя
        for path in busy:
            run["files"].append({"file": path.name, "outcome": "busy", "error": "файл ещё копируется или занят"})
        for path in sorted(ready, key=_order_key):
            try:
                run["files"].append(_process_one(path, db_path, dirs, dry_run, allow_gaps, allow_older, clock))
            except TrackerError as exc:
                run["error"] = {"code": exc.code, "message": str(exc),
                                "hint": "Файлы остались во входящей папке и будут обработаны при следующем запуске."}
                break
    except InboxLocked as exc:
        run["error"] = {"code": exc.code, "message": str(exc)}
        run["finished_at"] = _now()
        run["summary"] = summarize(run)
        return run
    finally:
        if lock:
            lock.__exit__(None, None, None)
    run["finished_at"] = _now()
    run["summary"] = summarize(run)
    if not dry_run:
        _save_run(dirs["state"], run)
    return run


def summarize(run):
    summary = {k: 0 for k in OUTCOME_TEXT}
    for item in run["files"]:
        summary[item["outcome"]] = summary.get(item["outcome"], 0) + 1
    return summary


def _write_json_atomic(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _save_run(state_dir, run):
    """Отчёт запуска: last_run.json — если были файлы или ошибка; last_check.json — при каждом запуске."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(state_dir / CHECK_FILE, {"checked_at": run["finished_at"], "summary": run["summary"],
                                                     "error": run["error"]})
        if not run["files"] and not run["error"] and not _handoff_news(run.get("handoff")):
            return
        _write_json_atomic(state_dir / LAST_RUN_FILE, run)
        log = state_dir / RUNS_LOG
        if log.exists() and log.stat().st_size > MAX_LOG_BYTES:
            os.replace(log, log.with_name(RUNS_LOG + ".1"))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Не удалось записать отчёт запуска в {state_dir}: {exc}", file=sys.stderr)


def _handoff_news(report):
    return bool(report and (report["copied"] or report["already_in_db"] or report["errors"]))


def retry_failed(db_path=None):
    """Вернуть файлы из health_failed во входящую папку (например, после исправления причины). Заметки об ошибках
    остаются в health_failed. Возвращает список имён."""
    dirs = ensure_folders(db_path)
    moved = []
    with _Lock(dirs["state"] / LOCK_FILE):
        for path in list_inbox(dirs["failed"]):
            moved.append(_move(path, dirs["inbox"]).name)
    return moved


# ---------- Состояние для MCP и панели (только чтение) ----------

def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first_line(text, limit=300):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[:limit - 1] + "…"


def inbox_status(db_path=None, failed_limit=10):
    """Состояние папок без изменений на диске: очередь, ошибки, отчёт последнего запуска."""
    dirs = folders(db_path)
    waiting = list_inbox(dirs["inbox"])
    failed = sorted(list_inbox(dirs["failed"]), key=lambda p: p.stat().st_mtime, reverse=True)
    last_run = _read_json(dirs["state"] / LAST_RUN_FILE)
    held = {}
    if last_run:
        for item in last_run.get("files", []):
            if item.get("outcome") in ("held", "busy"):
                held[item["file"]] = item
    failed_items = []
    for p in failed[:failed_limit]:
        note = p.with_name(p.name + ".error.txt")
        text = None
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            pass
        lines = (text or "").splitlines()
        failed_items.append({"file": p.name, "failed_at": lines[0] if lines else None,
                             "error": _first_line("\n".join(lines[1:]))})
    syncthing = dirs["syncthing"]
    return {
        "folders": {k: str(v) for k, v in dirs.items() if k != "data"},
        "syncthing": {"exists": syncthing.is_dir(), "files": len(list_inbox(syncthing)),
                      "not_handed_off": health_handoff.pending_count(syncthing, dirs["state"]),
                      # Syncthing всё ещё настроен на входящую папку: импорт меняет её, и Syncthing требует «Отменить».
                      "old_setup": (dirs["inbox"] / ".stfolder").exists() and not (syncthing / ".stfolder").exists()},
        "inbox_exists": dirs["inbox"].is_dir(),
        "waiting_files": len(waiting),
        "waiting": [{"file": p.name, "reason": _first_line(held[p.name].get("error")) if p.name in held else None,
                     "outcome": held[p.name]["outcome"] if p.name in held else "new"} for p in waiting[:20]],
        "failed_files": len(failed),
        "failed": failed_items,
        "processed_files": len(list_inbox(dirs["processed"])),
        "last_check": _read_json(dirs["state"] / CHECK_FILE),
        "last_run": _compact_run(last_run),
    }


def _compact_run(run):
    if not run:
        return None
    handoff = run.get("handoff") or {}
    return {"started_at": run.get("started_at"), "finished_at": run.get("finished_at"), "summary": run.get("summary"),
            "error": run.get("error"),
            "handoff": {"copied": [i.get("file") for i in handoff.get("copied", [])][:20],
                        "already_in_db": handoff.get("already_in_db", [])[:20],
                        "errors": [{"file": e.get("file"), "error": _first_line(e.get("error"))}
                                   for e in handoff.get("errors", [])[:20]]} if handoff.get("exists") else None,
            "files": [{k: item.get(k) for k in ("file", "outcome", "sequence", "batch_id", "totals")
                       if item.get(k) is not None} | ({"error": _first_line(item["error"])} if item.get("error") else {})
                      for item in run.get("files", [])[:20]]}


# ---------- Текст для командной строки ----------

def describe_run(run):
    lines = []
    if run["dry_run"]:
        lines.append("ПРОБНЫЙ ЗАПУСК: база и файлы не меняются.")
    lines.extend(health_handoff.describe(run.get("handoff")))
    if not run["files"] and not run["error"]:
        lines.append(f"Новых файлов нет ({run['inbox']}).")
    for item in run["files"]:
        head = f"{item['file']}: {OUTCOME_TEXT.get(item['outcome'], item['outcome'])}"
        if item.get("sequence") is not None and item["outcome"] in ("imported", "already_imported", "dry_run"):
            head += f" (файл №{item['sequence']}" + (f", импорт {item['batch_id']}" if item.get("batch_id") else "") + ")"
        lines.append(head)
        if item.get("totals"):
            t = item["totals"]
            lines.append(f"  добавлено {t['inserted']}, обновлено {t['updated']}, без изменений {t['unchanged']}, "
                         f"удалено {t['deleted']}, конфликтов {item.get('conflicts', 0)}")
        for warning in item.get("warnings") or []:
            lines.append(f"  Предупреждение: {warning}")
        if item.get("error"):
            lines.append("  " + item["error"].replace("\n", "\n  "))
    if run["error"]:
        lines.append(f"ОСТАНОВЛЕНО: {run['error']['message']}")
        if run["error"].get("hint"):
            lines.append(run["error"]["hint"])
    s = run["summary"]
    if run["files"]:
        lines.append("Итого: " + ", ".join(f"{OUTCOME_TEXT[k]} — {v}" for k, v in s.items() if v))
    return lines


def exit_code(run):
    """0 — всё обработано или нечего обрабатывать, 1 — ошибки файлов или запуска, 2 — есть ожидающие файлы,
    3 — папку уже обрабатывает другой запуск."""
    if run["error"]:
        return 3 if run["error"]["code"] == InboxLocked.code else 1
    if run["summary"].get("failed") or (run.get("handoff") or {}).get("errors"):
        return 1
    return 2 if run["summary"].get("held") else 0


def watch(db_path=None, interval=DEFAULT_INTERVAL_SEC, *, allow_gaps=False, allow_older=False, out=print,
          sleep=time.sleep, max_runs=None):
    """Наблюдение за входящей папкой: обработка раз в interval секунд, пока не нажат Ctrl+C."""
    out(f"Наблюдение за папкой {folders(db_path)['inbox']} (раз в {interval} с). Остановить: Ctrl+C.")
    runs, previous = 0, None
    try:
        while max_runs is None or runs < max_runs:
            try:
                run = process_inbox(db_path, allow_gaps=allow_gaps, allow_older=allow_older, sleep=sleep)
            except Exception as exc:                  # noqa: BLE001 — наблюдение не должно падать из-за одного сбоя
                # Например, папка на время недоступна. Файлы остаются на месте, следующая проверка повторит попытку.
                run = {"files": [], "error": {"code": "unexpected", "message": f"{type(exc).__name__}: {exc}",
                                              "hint": "Наблюдение продолжается, проверка повторится."},
                       "dry_run": False, "inbox": str(folders(db_path)["inbox"]), "summary": {}}
            runs += 1
            # Печатать только перемены: ожидающий файл не должен повторяться в окне каждые полминуты.
            handoff = run.get("handoff") or {}
            signature = (tuple((i["file"], i["outcome"], i.get("error")) for i in run["files"] if i["outcome"] != "busy"),
                         repr(run["error"]),
                         tuple(i["file"] for i in handoff.get("copied", [])) + tuple(handoff.get("already_in_db", [])),
                         repr(handoff.get("errors")))
            if signature != previous and (signature[0] or run["error"] or signature[2] or handoff.get("errors")):
                out(f"--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                for line in describe_run(run):
                    out(line)
            previous = signature
            if max_runs is not None and runs >= max_runs:
                break
            sleep(interval)
    except KeyboardInterrupt:
        out("Наблюдение остановлено.")
    return runs
