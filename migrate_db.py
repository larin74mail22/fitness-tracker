"""Явное обновление схемы базы FITNESS Tracker.

Запуск (из любой папки):
    python migrate_db.py --dry-run     # только показать, что будет применено
    python migrate_db.py               # резервная копия, обновление, проверки

Перед обновлением MCP-сервер (туннель) нужно остановить. Скрипт сам делает резервную копию
в data/backups, проверяет её и только потом применяет миграции. Отсутствующая база не создаётся.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness import db  # noqa: E402
from fitness.errors import TrackerError  # noqa: E402


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Обновление схемы базы FITNESS Tracker с резервной копией")
    parser.add_argument("--db", help="путь к базе (по умолчанию тот же, что у сервера: data/fitness.db)")
    parser.add_argument("--backup-dir", help="папка для резервной копии (по умолчанию data/backups рядом с базой)")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет применено; ничего не менять")
    args = parser.parse_args(argv)

    try:
        report = db.upgrade_database(args.db, backup_dir=args.backup_dir, dry_run=args.dry_run)
    except TrackerError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ОШИБКА SQLite: {exc}. Миграция не начиналась, база не изменена.", file=sys.stderr)
        return 1

    print(f"База: {report['database_path']}")
    print(f"Версия схемы: {report['from_version']}")
    if not report["pending"]:
        print("Схема уже последней версии, делать нечего.")
        return 0
    print("Ожидают применения: " + ", ".join(report["pending"]))
    if report["dry_run"]:
        print("Пробный запуск: база и файлы не изменены.")
        return 0
    print(f"Резервная копия: {report['backup']}")
    print(f"Применено версий: {report['applied']}, теперь версия схемы {report['to_version']}")
    print(f"Проверка: целостность {report['integrity']}, нарушений внешних ключей {report['foreign_key_violations']}")
    print("Готово. Теперь можно запускать туннель.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
