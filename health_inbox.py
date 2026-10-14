"""Автоматический импорт выгрузок Health Connect из входящей папки data\\health_inbox.

Запуск (из любой папки; проще — двойным щелчком по health_import_inbox.bat):
    python health_inbox.py              # обработать новые файлы один раз
    python health_inbox.py --watch      # наблюдать за папкой, пока окно открыто
    python health_inbox.py --status     # состояние синхронизации
    python health_inbox.py --dry-run    # проверить без записи и перемещения

Новые файлы из папки Syncthing data\\health_syncthing сначала копируются в data\\health_inbox; саму папку Syncthing
импорт не меняет, поэтому Syncthing не просит «Отменить локальные изменения» (docs/HEALTH_SYNCTHING.md).

Импортированные файлы переносятся в data\\health_processed, неверные — в data\\health_failed (с причиной в
<имя>.error.txt). Файлы не удаляются. Миграции не запускаются, база не создаётся.
Подробности: docs/HEALTH_AUTO_IMPORT.md.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness import health_inbox, health_status  # noqa: E402
from fitness.errors import TrackerError  # noqa: E402


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            # UTF-8 и при выводе в файл или канал (журнал, планировщик): иначе Windows пишет русский текст в cp1251.
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Импорт выгрузок Health Connect из входящей папки")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="наблюдать за папкой и импортировать новые файлы, пока не "
                                                          "нажат Ctrl+C")
    mode.add_argument("--status", action="store_true", help="показать состояние синхронизации и выйти")
    mode.add_argument("--retry-failed", action="store_true",
                      help="вернуть файлы из health_failed во входящую папку и обработать их снова")
    parser.add_argument("--dry-run", action="store_true", help="проверить файлы на копии базы, ничего не меняя")
    parser.add_argument("--interval", type=int, default=health_inbox.DEFAULT_INTERVAL_SEC,
                        help="пауза между проверками в режиме --watch, секунд (по умолчанию 30)")
    parser.add_argument("--allow-gaps", action="store_true",
                        help="принять файл, несмотря на пропуск номеров, даже если период пропущенных файлов не перекрыт")
    parser.add_argument("--allow-older", action="store_true",
                        help="принять пропущенный ранее файл с номером меньше последнего принятого")
    parser.add_argument("--db", help="путь к базе (по умолчанию data/fitness.db); папки берутся рядом с ней")
    parser.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    args = parser.parse_args(argv)

    try:
        if args.status:
            status = health_status.sync_status(args.db)
            print(json.dumps(status, ensure_ascii=False, indent=1) if args.json
                  else "\n".join(health_status.describe(status)))
            return 0 if status["database"]["ok"] else 1
        if args.watch:
            if args.dry_run:
                parser.error("--dry-run нельзя совместить с --watch")
            if args.interval < 5:
                parser.error("--interval: не меньше 5 секунд")
            health_inbox.watch(args.db, args.interval, allow_gaps=args.allow_gaps, allow_older=args.allow_older)
            return 0
        if args.retry_failed:
            moved = health_inbox.retry_failed(args.db)
            print(f"Возвращено во входящую папку: {len(moved)}" + (f" ({', '.join(moved)})" if moved else ""))
        run = health_inbox.process_inbox(args.db, dry_run=args.dry_run, allow_gaps=args.allow_gaps,
                                         allow_older=args.allow_older)
    except TrackerError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(run, ensure_ascii=False, indent=1) if args.json else "\n".join(health_inbox.describe_run(run)))
    return health_inbox.exit_code(run)


if __name__ == "__main__":
    sys.exit(main())
