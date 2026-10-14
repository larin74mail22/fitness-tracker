"""Командная строка единого запуска (tracker.py / tracker.pyw / FitnessTracker.exe)."""

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from .. import __version__
from . import components, config, supervisor as sv

EXIT_OK, EXIT_ERROR, EXIT_NOT_RUNNING = 0, 1, 3


def _has_console():
    return sys.stdout is not None and not getattr(sys, "frozen", False)


def _say(text, error=False):
    """Вывести сообщение: в консоль, а без консоли (pythonw, EXE) — окном."""
    if _has_console():
        print(text, file=sys.stderr if error else sys.stdout, flush=True)
    elif os.name == "nt":
        from .tray import message_box
        message_box(text, error=error)


def _configure_streams():
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _prepare_environment():
    """В EXE-сборке база и настройки — в папке проекта, а не внутри сборки."""
    if getattr(sys, "frozen", False) and not os.environ.get("FITNESS_DB_PATH"):
        os.environ["FITNESS_DB_PATH"] = str(config.project_dir() / "data" / "fitness.db")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tracker", description="FITNESS Tracker: единый запуск (туннель и MCP, автоимпорт Health Connect, панель)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="состояние работающего трекера")
    action.add_argument("--stop", action="store_true", help="остановить работающий трекер")
    action.add_argument("--restart", nargs="?", const="all", choices=["all", "tunnel", "import", "dashboard"],
                        help="перезапустить компоненты (по умолчанию все)")
    action.add_argument("--open", action="store_true", help="открыть веб-панель (трекер должен быть запущен)")
    action.add_argument("--import-now", action="store_true", help="проверить входящую папку Health Connect сейчас")
    action.add_argument("--check-mcp", action="store_true", help="проверить, что server.py отвечает по MCP (только чтение)")
    parser.add_argument("--no-tray", action="store_true", help="запуск без значка в трее (остановка: Ctrl+C или --stop)")
    parser.add_argument("--db", help="путь к базе (по умолчанию data\\fitness.db); журналы и настройки — рядом с ней")
    parser.add_argument("--json", action="store_true", help="вывести ответ в JSON")
    parser.add_argument("--version", action="version", version=f"FITNESS Tracker {__version__}")
    return parser


def main(argv=None, tray=True):
    _configure_streams()
    _prepare_environment()
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).resolve() if args.db else None

    if args.check_mcp:
        overrides = {"FITNESS_DB_PATH": str(db_path)} if db_path else None
        result = components.mcp_selfcheck(env_overrides=overrides)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        elif result["ok"]:
            _say(f"MCP в порядке: server.py {result['server_version']}, инструментов {result['tools']}, "
                 f"база {'в порядке' if result['database_ok'] else 'НЕДОСТУПНА: ' + result.get('warning', '')} "
                 f"({result['database_path']}), {result['seconds']} с")
        else:
            _say(f"MCP НЕ отвечает: {result.get('error')}", error=True)
        return EXIT_OK if result["ok"] and result.get("database_ok") else EXIT_ERROR

    command = ("status" if args.status else "stop" if args.stop else "restart" if args.restart else
               "open" if args.open else "import_now" if args.import_now else None)
    if command:
        return _client(command, args, db_path)
    return run(db_path, tray=tray and not args.no_tray)


def _client(command, args, db_path):
    payload = {"component": args.restart} if command == "restart" else None
    try:
        response = sv.send_command(command, payload, db_path)
    except sv.NotRunning as exc:
        _say(f"{exc}. Запуск: tracker.pyw (или ярлык FITNESS Tracker).", error=command != "status")
        return EXIT_NOT_RUNNING
    if args.json:
        print(json.dumps(response, ensure_ascii=False, indent=1, default=str))
    elif command == "status" and response.get("ok"):
        _say("\n".join(sv.describe_status(response["status"])))
    elif command == "stop" and response.get("ok"):
        stopped = sv.wait_until(lambda: not sv.is_running(db_path), 150, 0.5)
        _say("FITNESS Tracker остановлен." if stopped else "Команда остановки отправлена, трекер ещё завершается.")
        return EXIT_OK if stopped else EXIT_ERROR
    elif command == "open" and response.get("ok"):
        _say(f"Панель: {response['url']}")
    elif response.get("ok"):
        _say("Готово.")
    else:
        _say(f"Ошибка: {response.get('error') or response}", error=True)
    return EXIT_OK if response.get("ok") else EXIT_ERROR


def run(db_path=None, tray=True):
    """Запуск трекера. Возвращает код выхода после остановки."""
    paths = config.paths(db_path)
    try:
        log = sv.setup_logging(paths["logs"], also_stderr=_has_console() and not tray)[0]
    except OSError as exc:
        _say(f"Не удалось открыть журнал в {paths['logs']}: {exc}", error=True)
        return EXIT_ERROR
    sv.install_crash_hooks(log)
    try:
        try:
            settings = config.load(db_path)
        except ValueError as exc:
            log.error("%s", exc)
            _say(str(exc), error=True)
            return EXIT_ERROR
        sup = sv.Supervisor(db_path, settings)
        try:
            sup.start()
        except sv.AlreadyRunning:
            log.info("Повторный запуск: трекер уже работает, второй экземпляр не создан")
            if tray:
                _say("FITNESS Tracker уже запущен — значок в области уведомлений (возле часов).")
            else:
                _say("FITNESS Tracker уже запущен; второй экземпляр не создан.")
            return EXIT_OK
        if tray and os.name == "nt":
            from .tray import TrayApp
            try:
                TrayApp(sup).run()
            except Exception:                             # noqa: BLE001 — без трея трекер всё равно работает
                log.exception("Значок в трее не создан; трекер работает без него (остановка: tracker.py --stop)")
                _wait_headless(sup)
        else:
            _wait_headless(sup)
        sup.stop("завершение")
        return EXIT_OK
    finally:
        sv.close_logging()


def _wait_headless(sup):
    def handler(signum, frame):
        threading.Thread(target=sup.stop, args=(f"сигнал {signum}",), daemon=True).start()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError):
                pass
    while not sup.wait(0.5):
        pass
    sup._stopped.wait(180)
