"""Импорт файлов выгрузки Health Connect (шаги, пульс, сон) в базу FITNESS Tracker.

Запуск (из любой папки):
    python import_health.py --dry-run ФАЙЛ_ИЛИ_ПАПКА
    python import_health.py ФАЙЛ_ИЛИ_ПАПКА ...

Файл проверяется целиком, потом записывается одной транзакцией: либо всё, либо ничего. Повторный импорт того же файла
ничего не меняет. Файлы не перемещаются и не удаляются. Миграции не запускаются, база не создаётся.
--dry-run работает на копии базы в памяти и ничего не меняет в самой базе.
Формат файла: docs/HEALTH_EXPORT_FORMAT.md.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness import health_format, health_import  # noqa: E402
from fitness.errors import ImportFileError, TrackerError  # noqa: E402

TYPE_NAMES = {"steps": "Шаги", "heart_rate": "Пульс", "sleep": "Сон"}
NOT_APPLIED_TEXT = {
    "stale": "записей с версией старше сохранённой",
    "replay_of_deleted": "записей, возвращающих удалённое старыми данными",
    "conflict": "КОНФЛИКТОВ (то же время изменения, другое содержимое)",
    "deletion_out_of_scope": "удалений чужих записей (другой телефон и источник)",
    "deletion_stale": "удалений записей, уже обновлённых более поздним экспортом",
}


def collect_files(paths):
    """Файлы как заданы; папки — *.json по порядку (телефон, номер файла, имя). Нечитаемые файлы в папке идут в конец:
    при импорте они дадут понятную ошибку."""
    result = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            items = []
            for f in sorted(path.glob("*.json")):
                order = None
                try:
                    if f.stat().st_size <= health_format.MAX_FILE_BYTES:
                        order = health_format.peek_order(f.read_bytes())
                except OSError:
                    order = None
                items.append((order is None, order or ("", 0), f.name, f))
            result.extend(f for _, _, _, f in sorted(items, key=lambda x: (x[0], x[1][0], x[1][1], x[2])))
        else:
            result.append(path)
    return result


def describe(report):
    lines = []
    name = report["file"]
    if report["status"] == "already_imported":
        return [f"{name}: уже был импортирован (батч {report['batch_id']}), пропущен."]
    head = {"imported": "импортирован", "dry_run": "пробный запуск, в базу ничего не записано"}[report["status"]]
    lines.append(f"{name}: {head} (телефон {report['phone_id']}, файл №{report['sequence']}"
                 + (f", батч {report['batch_id']}" if report["batch_id"] else "") + ")")
    for kind, title in TYPE_NAMES.items():
        c = report["counts"][kind]
        lines.append(f"  {title}: добавлено {c['inserted']}, обновлено {c['updated']}, без изменений {c['unchanged']}, "
                     f"удалено {c['deleted']}, не применено из-за конфликта {c['rejected']}")
    if report["unmatched_deletions"]:
        lines.append(f"  Удалений для неизвестных записей (в базе их нет): {report['unmatched_deletions']}")
    for key, text in NOT_APPLIED_TEXT.items():
        if report["not_applied"].get(key):
            lines.append(f"  Не применено: {report['not_applied'][key]} {text}")
    if report["not_applied"].get("conflict"):
        lines.append("  ВНИМАНИЕ: конфликтные записи в базе не изменены. Подробности ниже.")
    for warning in report["warnings"]:
        lines.append(f"  Предупреждение: {warning}")
    return lines


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Импорт выгрузки Health Connect в FITNESS Tracker")
    parser.add_argument("paths", nargs="+", help="файлы выгрузки или папки с файлами *.json")
    parser.add_argument("--db", help="путь к базе (по умолчанию тот же, что у сервера: data/fitness.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="проверить и показать результат на копии базы в памяти, ничего не записывая")
    parser.add_argument("--allow-gaps", action="store_true", help="разрешить пропуск номеров файлов от телефона")
    parser.add_argument("--allow-older", action="store_true",
                        help="принять ранее пропущенный файл с номером меньше последнего принятого "
                             "(более новые данные в базе при этом не затираются)")
    args = parser.parse_args(argv)

    files = collect_files(args.paths)
    if not files:
        print("ОШИБКА: не найдено ни одного файла *.json.", file=sys.stderr)
        return 1
    for path in files:
        try:
            report = health_import.import_file(path, args.db, dry_run=args.dry_run, allow_gaps=args.allow_gaps,
                                               allow_older=args.allow_older)
        except ImportFileError as exc:
            print(f"ОШИБКА в файле {path.name}: {exc}", file=sys.stderr)
            if getattr(exc, "journal_batch_id", None):
                print(f"Попытка записана в журнал импортов (батч {exc.journal_batch_id}).", file=sys.stderr)
            return 1
        except TrackerError as exc:
            print(f"ОШИБКА при импорте {path.name}: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:                      # noqa: BLE001 — последняя защита: не показывать traceback
            print(f"НЕОЖИДАННАЯ ОШИБКА при импорте {path.name} ({type(exc).__name__}): {exc}. Импорт файла выполняется "
                  "одной транзакцией, поэтому при сбое в базе ничего не сохраняется.", file=sys.stderr)
            return 2
        for line in describe(report):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
