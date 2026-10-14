"""Управляющий процесс: один экземпляр, журнал, компоненты, локальное управление (команды --status, --stop, ...).

Управление — TCP только на 127.0.0.1, случайный порт и одноразовый токен в data\\run\\control.json (файл виден
только пользователю, как и сама база). Сетевых портов наружу launcher не открывает.
"""

import hmac
import json
import logging
import logging.handlers
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime

from .. import __version__, db
from . import components, config

LOGGER_NAME, TUNNEL_LOGGER_NAME = "fitness.tracker", "fitness.tunnel"


class AlreadyRunning(Exception):
    pass


class NotRunning(Exception):
    pass


# ---------- Журнал ----------

def setup_logging(log_dir, also_stderr=False):
    """tracker.log (1 МБ × 5) и tunnel.log (2 МБ × 3), UTF-8. Повторный вызов не дублирует обработчики."""
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    loggers = []
    for name, file_name, size, count in ((LOGGER_NAME, "tracker.log", 1_000_000, 5),
                                         (TUNNEL_LOGGER_NAME, "tunnel.log", 2_000_000, 3)):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        handler = logging.handlers.RotatingFileHandler(log_dir / file_name, maxBytes=size, backupCount=count,
                                                       encoding="utf-8", delay=True)
        handler.setFormatter(fmt if name == LOGGER_NAME else logging.Formatter("%(asctime)s %(message)s",
                                                                               "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        if also_stderr and name == LOGGER_NAME and sys.stderr is not None:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(fmt)
            logger.addHandler(stream)
        loggers.append(logger)
    return loggers


def close_logging():
    for name in (LOGGER_NAME, TUNNEL_LOGGER_NAME):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def install_crash_hooks(log):
    """Необработанные исключения (в том числе в потоках) попадают в журнал, а не теряются вместе с окном."""
    def hook(exc_type, exc, tb):
        log.critical("Необработанная ошибка", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            log.critical("Необработанная ошибка в потоке %s", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


# ---------- Один экземпляр ----------

class InstanceLock:
    """Блокировка файла data\\run\\launcher.lock. Снимается сама, если процесс завершился любым способом."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(f"FITNESS Tracker уже запущен (блокировка {self.path})") from None
        self.handle = handle
        return self

    def release(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.handle.close()
        self.handle = None


def is_running(db_path=None):
    lock = InstanceLock(config.paths(db_path)["lock"])
    try:
        lock.acquire()
    except AlreadyRunning:
        return True
    lock.release()
    return False


# ---------- Управление работающим экземпляром ----------

class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline(65536).decode("utf-8"))
            token = request.get("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, self.server.token):
                response = {"ok": False, "error": "неверный токен"}
            else:
                response = self.server.supervisor.handle_command(request.get("command"), request.get("args") or {})
        except Exception as exc:                          # noqa: BLE001
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write((json.dumps(response, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


class _ControlServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def send_command(command, args=None, db_path=None, timeout=30):
    """Отправить команду работающему экземпляру. NotRunning — если он не запущен."""
    if not is_running(db_path):
        raise NotRunning("FITNESS Tracker не запущен")
    path = config.paths(db_path)["control"]
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotRunning(f"FITNESS Tracker запускается или не отвечает (нет {path})") from None
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout) as sock:
            sock.sendall((json.dumps({"token": info["token"], "command": command, "args": args or {}}) + "\n")
                         .encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise NotRunning(f"FITNESS Tracker не отвечает: {exc}") from None
    return json.loads(data.decode("utf-8"))


# ---------- Supervisor ----------

class Supervisor:
    """Собирает компоненты. start() — один раз; stop() безопасно вызывать повторно и из любого потока."""

    def __init__(self, db_path=None, settings=None, notify=None, env_overrides=None):
        self.db_path = db_path
        self.paths = config.paths(db_path)
        self.settings = settings if settings is not None else config.load(db_path)
        self.log = logging.getLogger(LOGGER_NAME)
        self.notify = notify or (lambda title, text: None)
        self.on_change = None
        self.started_at = None
        self.mcp_check = None
        overrides = dict(env_overrides or {})
        if db_path is not None:
            overrides.setdefault("FITNESS_DB_PATH", str(db.resolve_db_path(db_path)))
        self.env_overrides = overrides
        self.tunnel = components.Tunnel(self.settings, self.log, logging.getLogger(TUNNEL_LOGGER_NAME), overrides)
        self.importer = components.ImportWatcher(self.settings, self.log, db_path, notify=self._notify)
        self.dashboard = components.Dashboard(self.settings, self.log, db_path)
        self.components = [self.tunnel, self.importer, self.dashboard]
        for comp in self.components:
            comp.on_change = self._changed
        self._lock = InstanceLock(self.paths["lock"])
        self._control = None
        self._stopping = threading.Event()
        self._stopped = threading.Event()
        self._stop_guard = threading.Lock()
        self._mcp_thread = None

    def _notify(self, title, text):
        try:
            self.notify(title, text)
        except Exception:                                 # noqa: BLE001
            self.log.exception("notify failed")

    def _changed(self, comp):
        level = logging.ERROR if comp.state == components.ERROR else logging.INFO
        self.log.log(level, "%s: %s%s", comp.title, components.STATE_TEXT.get(comp.state, comp.state),
                     f" — {comp.message}" if comp.message else "")
        if comp is self.tunnel and comp.state == components.ERROR and comp.restarts in (3, 10):
            self._notify("FITNESS Tracker: туннель", comp.message)
        if self.on_change:
            try:
                self.on_change()
            except Exception:                             # noqa: BLE001
                self.log.exception("on_change failed")

    # запуск и остановка
    def start(self):
        self._lock.acquire()                              # AlreadyRunning — второй экземпляр не стартует
        try:
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log.info("==== FITNESS Tracker %s: запуск (PID %s, база %s)", __version__, os.getpid(),
                          db.resolve_db_path(self.db_path))
            self._start_control()
            self.tunnel.start()
            self.importer.start()
            self._mcp_thread = threading.Thread(target=self._mcp_loop, name="mcp-check", daemon=True)
            self._mcp_thread.start()
        except BaseException:
            self.stop()
            raise
        return self

    def _start_control(self):
        server = _ControlServer(("127.0.0.1", 0), _ControlHandler)
        server.supervisor = self
        server.token = secrets.token_urlsafe(24)
        threading.Thread(target=server.serve_forever, name="control", daemon=True).start()
        self._control = server
        info = {"pid": os.getpid(), "port": server.server_address[1], "token": server.token,
                "started_at": self.started_at, "version": __version__}
        path = self.paths["control"]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, path)

    def stop(self, reason="остановка", fast=False):
        """Порядок: импорт дописывает текущий файл → туннель (server.py получает конец stdin) → панель.

        fast — выход из Windows: ждать меньше. Если Windows всё же прервёт импорт, транзакция SQLite откатится,
        а файл останется во входящей папке и будет импортирован при следующем запуске."""
        with self._stop_guard:
            if self._stopped.is_set():
                return
            self._stopping.set()
            self.log.info("Остановка: %s", reason)
            for comp, kwargs in ((self.importer, {"timeout": 15 if fast else 120}),
                                 (self.tunnel, {"timeout": 5 if fast else 15}), (self.dashboard, {})):
                try:
                    comp.stop(**kwargs)
                except Exception:                         # noqa: BLE001 — остановить остальные любой ценой
                    self.log.exception("Ошибка при остановке: %s", comp.title)
            if self._control is not None:
                self._control.shutdown()
                self._control.server_close()
                self._control = None
                try:
                    self.paths["control"].unlink()
                except OSError:
                    pass
            self._lock.release()
            self.log.info("==== FITNESS Tracker остановлен")
            self._stopped.set()

    def wait(self, timeout=None):
        return self._stopping.wait(timeout)

    @property
    def stopping(self):
        return self._stopping.is_set()

    # проверка MCP
    def _mcp_loop(self):
        minutes = float(self.settings.get("mcp_check_minutes") or 0)
        while not self._stopping.is_set():
            self.check_mcp()
            if minutes <= 0 or self._stopping.wait(minutes * 60):
                break

    def check_mcp(self):
        result = components.mcp_selfcheck(env_overrides=self.env_overrides)
        previous = self.mcp_check
        self.mcp_check = result
        if result["ok"]:
            text = (f"MCP: server.py отвечает (версия {result['server_version']}, инструментов {result['tools']}, "
                    f"база {'в порядке' if result['database_ok'] else 'НЕДОСТУПНА: ' + result.get('warning', '')})")
            if not previous or not previous.get("ok") or previous.get("database_ok") != result["database_ok"]:
                self.log.log(logging.INFO if result["database_ok"] else logging.ERROR, text)
        else:
            self.log.error("MCP: server.py не отвечает: %s", result.get("error"))
            if not previous or previous.get("ok"):
                self._notify("FITNESS Tracker: MCP", result.get("error") or "server.py не отвечает")
        if self.on_change:
            try:
                self.on_change()
            except Exception:                             # noqa: BLE001
                self.log.exception("on_change failed")
        return result

    # состояние
    def overall(self):
        """ok / warning / error — для цвета значка."""
        states = [self.tunnel.state, self.importer.state]
        if components.ERROR in states or (self.mcp_check and not self.mcp_check.get("ok")):
            return "error"
        if components.STARTING in states or (self.mcp_check and not self.mcp_check.get("database_ok")):
            return "warning"
        return "ok"

    def status(self):
        return {"version": __version__, "pid": os.getpid(), "started_at": self.started_at,
                "database": str(db.resolve_db_path(self.db_path)), "overall": self.overall(),
                "components": {c.name: c.status() for c in self.components}, "mcp_check": self.mcp_check,
                "logs": str(self.paths["logs"])}

    def handle_command(self, command, args):
        if command == "status":
            return {"ok": True, "status": self.status()}
        if command == "open":
            return {"ok": self.dashboard.open(), "url": self.dashboard.url, "status": self.dashboard.status()}
        if command == "import_now":
            self.importer.run_now()
            return {"ok": True}
        if command == "check_mcp":
            return {"ok": True, "mcp_check": self.check_mcp()}
        if command == "restart":
            target = args.get("component", "all")
            self.restart(target)
            return {"ok": True, "restarted": target}
        if command == "stop":
            threading.Thread(target=self.stop, args=("команда --stop",), daemon=True).start()
            return {"ok": True}
        if command == "ping":
            return {"ok": True, "pid": os.getpid()}
        return {"ok": False, "error": f"неизвестная команда {command!r}"}

    def restart(self, target="all"):
        self.log.info("Перезапуск: %s", target)
        if target in ("tunnel", "all"):
            self.tunnel.restart()
        if target in ("import", "all"):
            self.importer.restart()
        if target in ("dashboard", "all") and self.dashboard.server is not None:
            self.dashboard.stop()
            self.dashboard.ensure_running()
        if target == "all":
            threading.Thread(target=self.check_mcp, name="mcp-check-once", daemon=True).start()


def describe_status(status):
    """Текст состояния для окна «Состояние» и команды --status."""
    lines = [f"FITNESS Tracker {status['version']} — запущен {status['started_at']} (PID {status['pid']})",
             f"База: {status['database']}", ""]
    for comp_name, title in (("tunnel", "Туннель и MCP"), ("import", "Автоимпорт"), ("dashboard", "Панель")):
        comp = status["components"][comp_name]
        lines.append(f"{title}: {comp['state_text']}" + (f" — {comp['message']}" if comp["message"] else ""))
        if comp_name == "tunnel" and comp.get("restarts"):
            lines.append(f"  перезапусков после сбоя: {comp['restarts']}")
        if comp_name == "import" and comp.get("last_check"):
            summary = ", ".join(f"{k}: {v}" for k, v in (comp.get("last_summary") or {}).items()) or "новых файлов нет"
            lines.append(f"  последняя проверка {comp['last_check']} ({summary}); "
                         f"импортировано с запуска: {comp['imported_since_start']}")
            if comp.get("last_error"):
                lines.append(f"  ошибка: {comp['last_error']}")
    check = status.get("mcp_check")
    if check:
        if check.get("ok"):
            lines.append(f"Проверка MCP ({check['checked_at']}): server.py отвечает, версия {check['server_version']}, "
                         f"инструментов {check['tools']}, база " + ("в порядке" if check["database_ok"]
                                                                   else f"недоступна ({check.get('warning')})"))
        else:
            lines.append(f"Проверка MCP ({check['checked_at']}): ОШИБКА — {check.get('error')}")
    lines.append("")
    lines.append(f"Журналы: {status['logs']}")
    return lines


def wait_until(predicate, timeout, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
