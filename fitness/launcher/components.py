"""Компоненты единого запуска: туннель (tunnel-client → server.py), автоимпорт Health Connect, веб-панель,
проверка MCP. Каждый компонент работает в своём потоке и сам переживает сбои; состояние — в status()."""

import collections
import json
import os
import subprocess
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from .. import health_inbox
from . import config, winproc

STOPPED, STARTING, RUNNING, ERROR, DISABLED, EXTERNAL = "stopped", "starting", "running", "error", "disabled", "external"
STATE_TEXT = {STOPPED: "остановлен", STARTING: "запускается", RUNNING: "работает", ERROR: "ошибка",
              DISABLED: "выключен в настройках", EXTERNAL: "запущен вне launcher"}

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_ok(url, timeout=3.0):
    try:
        with _NO_PROXY.open(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:                                    # noqa: BLE001 — любой сбой означает «не отвечает»
        return False


def _stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Component:
    name = "component"
    title = "компонент"

    def __init__(self, log):
        self.log = log
        self.state = STOPPED
        self.message = ""
        self.since = _stamp()
        self.on_change = None
        self._lock = threading.Lock()

    def _set(self, state, message=""):
        with self._lock:
            changed = (state, message) != (self.state, self.message)
            if state != self.state:
                self.since = _stamp()
            self.state, self.message = state, message
        if changed and self.on_change:
            try:
                self.on_change(self)
            except Exception:                            # noqa: BLE001 — уведомление не должно ронять компонент
                self.log.exception("on_change failed")

    def status(self):
        with self._lock:
            return {"state": self.state, "state_text": STATE_TEXT.get(self.state, self.state),
                    "message": self.message, "since": self.since}


# ---------- Туннель ----------

class Tunnel(Component):
    """Запускает tunnel-client с профилем fitness-local; tunnel-client сам запускает «python server.py» (MCP по stdio).

    Не запускает второй экземпляр, если tunnel-client уже работает (например, запущен вручную). После падения
    перезапускает с растущей паузой 5 с … 5 мин и пишет причину и последние строки вывода в журнал.
    """
    name, title = "tunnel", "Туннель и MCP-сервер"
    BACKOFF_START, BACKOFF_MAX, STABLE_SEC, PROBE_SEC = 5, 300, 600, 15
    NOT_FOUND_WAIT, EXTERNAL_WAIT = 60, 30

    def __init__(self, settings, log, output_log, env_overrides=None, cwd=None):
        super().__init__(log)
        self.settings = settings
        self.output_log = output_log
        self.env_overrides = dict(env_overrides or {})
        self.cwd = str(cwd or config.project_dir())
        self.process = None
        self.ready = None
        self.restarts = 0
        self.health_url = None
        self._tail = collections.deque(maxlen=15)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._job = None
        self._backoff = self.BACKOFF_START
        self._restart_requested = False
        # Только для тестов: другая команда вместо tunnel-client.exe и имя exe для поиска «чужих» экземпляров.
        self.command_override = None
        self.image_name = None

    # управление
    def start(self):
        if not self.settings.get("tunnel_enabled", True):
            self._set(DISABLED, "tunnel_enabled=false в data\\launcher.json")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._job = winproc.Job()
        self._thread = threading.Thread(target=self._loop, name="tunnel", daemon=True)
        self._thread.start()

    def restart(self):
        if self.state == DISABLED:
            return
        self.log.info("Туннель: перезапуск по команде")
        self._backoff = self.BACKOFF_START
        self._restart_requested = True
        self._wake.set()
        if not (self._thread and self._thread.is_alive()):
            self.start()

    def stop(self, timeout=15):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        self._kill()
        if self._job:
            self._job.close()
            self._job = None
        if self.state != DISABLED:
            self._set(STOPPED, "")

    # внутреннее
    def _wait(self, seconds):
        """Пауза, которую прерывают остановка и перезапуск. True — пора выходить."""
        self._wake.wait(seconds)
        self._wake.clear()
        return self._stop.is_set()

    def _prepare(self):
        """(команда, окружение, exe) или None с состоянием ошибки."""
        exe = Path(self.image_name) if self.command_override else config.find_tunnel_client(self.settings)
        if exe is None:
            self._set(ERROR, "не найден tunnel-client.exe: укажите путь в data\\launcher.json (tunnel_client)")
            return None
        profile = config.profile_path(self.settings)
        if not profile.is_file():
            self._set(ERROR, f"не найден профиль tunnel-client: {profile}")
            return None
        try:
            info = config.read_profile(profile)
        except OSError as exc:
            self._set(ERROR, f"не удалось прочитать профиль {profile}: {exc}")
            return None
        self.health_url = f"http://{info['listen_addr']}"
        env = dict(os.environ)
        env.update(self.env_overrides)
        name = info["api_key_env"]
        if name and not env.get(name):
            value = winproc.user_environment_value(name)
            if not value:
                self._set(ERROR, f"не задана переменная окружения {name} с ключом туннеля")
                return None
            env[name] = value
        if self.command_override:
            return list(self.command_override) + [str(profile)], env, exe
        return [str(exe), "run", "--profile-file", str(profile)], env, exe

    def _external(self, exe):
        own = {self.process.pid} if self.process and self.process.poll() is None else set()
        others = [p for p in winproc.find_processes(exe.name) if p not in own]
        if others:
            return f"tunnel-client уже запущен (PID {', '.join(map(str, others))}); второй экземпляр не запускаю"
        if not own and self.health_url and http_ok(self.health_url + "/healthz", 2):
            return f"адрес {self.health_url} уже занят другим процессом"
        return None

    # Служебные строки запуска tunnel-client (внедрение зависимостей fx): ~200 строк на каждый старт, без пользы.
    _NOISE = ('"msg":"provided"', '"msg":"supplied"', '"msg":"invoking"', '"msg":"invoked"', '"msg":"decorated"',
              '"msg":"replaced"', '"msg":"run"', '"msg":"OnStart', '"msg":"OnStop', '"msg":"started"')

    def _read_output(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._tail.append(line)
                if not any(marker in line for marker in self._NOISE):
                    self.output_log.info(line)
        proc.stdout.close()

    def _kill(self):
        proc, self.process = self.process, None
        if proc is None or proc.poll() is not None:
            return
        # server.py получает конец stdin и завершается сам, не прерывая начатую запись в базу.
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.log.info("Туннель: tunnel-client остановлен")

    def _loop(self):
        self._backoff = self.BACKOFF_START
        while not self._stop.is_set():
            self._restart_requested = False
            prepared = self._prepare()
            if prepared is None:
                self.log.error("Туннель: %s", self.message)
                if self._wait(self.NOT_FOUND_WAIT):
                    break
                continue
            cmd, env, exe = prepared
            external = self._external(exe)
            if external:
                if self.state != EXTERNAL:
                    self.log.warning("Туннель: %s", external)
                self._set(EXTERNAL, external)
                if self._wait(self.EXTERNAL_WAIT):
                    break
                continue
            self._set(STARTING, "запуск tunnel-client")
            self.log.info("Туннель: запуск %s run --profile-file %s", exe, cmd[-1])
            try:
                proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, **winproc.hidden_popen_kwargs())
            except OSError as exc:
                self._set(ERROR, f"не удалось запустить tunnel-client: {exc}")
                self.log.error("Туннель: %s", self.message)
                if self._wait(self.NOT_FOUND_WAIT):
                    break
                continue
            self.process = proc
            if self._job and not self._job.assign(proc):
                self.log.warning("Туннель: не удалось добавить процесс в Job Object")
            threading.Thread(target=self._read_output, args=(proc,), name="tunnel-output", daemon=True).start()
            started = time.monotonic()
            self.ready = None
            while proc.poll() is None and not self._stop.is_set() and not self._restart_requested:
                ready = http_ok(self.health_url + "/readyz")
                if proc.poll() is not None:
                    break
                if ready != self.ready:
                    self.log.info("Туннель: %s", "готов (readyz)" if ready else "ещё не готов (readyz)")
                self.ready = ready
                self._set(RUNNING if ready else STARTING,
                          f"PID {proc.pid}, " + ("готов" if ready else "подключается к серверу"))
                self._wake.wait(self.PROBE_SEC)
                self._wake.clear()
            if self._stop.is_set() or self._restart_requested:
                self._kill()
                continue
            code = proc.wait()
            self.process = None
            self.ready = False
            uptime = time.monotonic() - started
            if uptime > self.STABLE_SEC:
                self._backoff = self.BACKOFF_START
            self.restarts += 1
            time.sleep(0.2)                               # дочитать последние строки вывода
            tail = " | ".join(list(self._tail)[-5:])
            self._set(ERROR, f"tunnel-client завершился (код {code}), перезапуск через {self._backoff} с")
            self.log.error("Туннель: tunnel-client завершился с кодом %s через %.0f с. Последний вывод: %s",
                           code, uptime, tail or "(нет)")
            if self._wait(self._backoff):
                break
            self._backoff = min(self._backoff * 2, self.BACKOFF_MAX)

    def status(self):
        result = super().status()
        proc = self.process
        result.update(pid=proc.pid if proc and proc.poll() is None else None,
                      ready=self.ready, restarts=self.restarts, health_url=self.health_url)
        return result


# ---------- Автоимпорт Health Connect ----------

class ImportWatcher(Component):
    """Раз в interval секунд — тот же проход, что health_inbox.py: копия новых файлов из health_syncthing в
    health_inbox и импорт. Импорт пишет в базу одной транзакцией на файл и переносит файл только после неё, поэтому
    обрыв (выключение Windows, сбой) не теряет данные: при следующем проходе файл узнаётся по SHA-256."""
    name, title = "import", "Автоимпорт Health Connect"

    def __init__(self, settings, log, db_path=None, notify=None, process=None):
        super().__init__(log)
        self.settings = settings
        self.db_path = db_path
        self.notify = notify
        self.interval = max(5, int(settings.get("import_interval_sec", 30)))
        self._process = process or health_inbox.process_inbox
        self._stop = threading.Event()
        self._now = threading.Event()
        self._thread = None
        self._signature = None
        self._notified = None
        self.last_check = None
        self.last_summary = None
        self.last_error = None
        self.imported = 0
        self.runs = 0

    def start(self):
        if not self.settings.get("import_enabled", True):
            self._set(DISABLED, "import_enabled=false в data\\launcher.json")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._now.clear()
        self._thread = threading.Thread(target=self._loop, name="import", daemon=True)
        self._thread.start()
        self._set(RUNNING, f"проверка каждые {self.interval} с")

    def run_now(self):
        self._now.set()

    def restart(self):
        self.stop()
        self.start()

    def stop(self, timeout=120):
        """Дождаться окончания текущего прохода (импорт файла не прерывается посередине)."""
        self._stop.set()
        self._now.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.log.warning("Импорт: проход не завершился за %s с", timeout)
        if self.state != DISABLED:
            self._set(STOPPED, "")

    def _loop(self):
        self.log.info("Импорт: наблюдение за %s (каждые %s с)", health_inbox.folders(self.db_path)["inbox"],
                      self.interval)
        while not self._stop.is_set():
            self.run_once()
            self._now.wait(self.interval)
            self._now.clear()

    def run_once(self):
        try:
            run = self._process(self.db_path)
        except Exception as exc:                          # noqa: BLE001 — наблюдение не должно падать
            run = {"files": [], "error": {"code": "unexpected", "message": f"{type(exc).__name__}: {exc}"},
                   "dry_run": False, "inbox": "", "summary": {}, "handoff": None}
            self.log.exception("Импорт: непредвиденный сбой прохода")
        self.runs += 1
        self.last_check = _stamp()
        self.last_summary = {k: v for k, v in (run.get("summary") or {}).items() if v}
        self.last_error = run["error"]["message"] if run.get("error") else None
        handoff = run.get("handoff") or {}
        signature = (tuple((i["file"], i["outcome"], i.get("error")) for i in run["files"] if i["outcome"] != "busy"),
                     repr(run["error"]), tuple(i.get("file") for i in handoff.get("copied", [])),
                     repr(handoff.get("errors")))
        if signature != self._signature and (signature[0] or run["error"] or signature[2] or handoff.get("errors")):
            for line in health_inbox.describe_run(run):
                self.log.info("Импорт: %s", line)
        self._signature = signature
        imported = [i for i in run["files"] if i["outcome"] == "imported"]
        self.imported += len(imported)
        error = run.get("error")
        if error and error.get("code") == health_inbox.InboxLocked.code:
            self._set(RUNNING, "папку сейчас обрабатывает другой запуск (health_import_inbox.bat?)")
        elif error:
            self._set(ERROR, error["message"][:300])
        elif (run.get("summary") or {}).get("failed"):
            self._set(ERROR, f"файлов с ошибкой: {run['summary']['failed']} (data\\health_failed)")
        else:
            self._set(RUNNING, f"проверка каждые {self.interval} с")
        if self.notify:
            for item in imported:
                totals = item.get("totals") or {}
                self.notify("Импорт Health Connect",
                            f"Файл №{item.get('sequence')}: добавлено {totals.get('inserted', 0)}, "
                            f"обновлено {totals.get('updated', 0)}")
            failed = [i for i in run["files"] if i["outcome"] == "failed"]
            if failed and signature != self._notified:
                self.notify("Импорт Health Connect: ошибка", f"{failed[0]['file']}: см. data\\health_failed")
                self._notified = signature
        return run

    def status(self):
        result = super().status()
        result.update(last_check=self.last_check, last_summary=self.last_summary, last_error=self.last_error,
                      imported_since_start=self.imported, runs=self.runs)
        return result


# ---------- Веб-панель ----------

class Dashboard(Component):
    """Панель запускается только по запросу (пункт меню «Открыть панель») и работает до выхода из трекера."""
    name, title = "dashboard", "Веб-панель"

    def __init__(self, settings, log, db_path=None, open_browser=webbrowser.open):
        super().__init__(log)
        self.port = int(settings.get("dashboard_port", 8765))
        self.db_path = db_path
        self.server = None
        self._thread = None
        self._open_browser = open_browser
        self.message = "откроется по запросу"

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def ensure_running(self):
        if self.server is not None:
            return True
        from ..web.app import make_server
        try:
            self.server = make_server(self.port, self.db_path)
        except OSError as exc:
            if http_ok(self.url):
                self._set(EXTERNAL, f"{self.url} уже открыт другим процессом (dashboard.bat?)")
                return True
            self._set(ERROR, f"порт {self.port} занят: {exc}")
            self.log.error("Панель: %s", self.message)
            return False
        self._thread = threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        self.log.info("Панель: запущена на %s", self.url)
        self._set(RUNNING, self.url)
        return True

    def open(self):
        if self.ensure_running():
            self._open_browser(self.url)
            return True
        return False

    def stop(self, timeout=5):
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()
            if self._thread:
                self._thread.join(timeout)
            self.log.info("Панель: остановлена")
        self._set(STOPPED, "откроется по запросу")


# ---------- Проверка MCP ----------

def mcp_selfcheck(python=None, script=None, env_overrides=None, timeout=30):
    """Запустить server.py так же, как tunnel-client (python server.py, stdio), и спросить initialize, tools/list,
    get_tracker_status. Только чтение. Возвращает словарь с ok, версией, числом инструментов и состоянием базы."""
    python = python or config.python_for_mcp()
    script = script or config.server_script()
    env = dict(os.environ)
    env.update(env_overrides or {})
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "fitness-launcher", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_tracker_status", "arguments": {}}},
    ]
    payload = "".join(json.dumps(r) + "\n" for r in requests).encode("utf-8")
    started = time.monotonic()
    result = {"ok": False, "checked_at": _stamp(), "python": str(python), "script": str(script)}
    try:
        proc = subprocess.run([str(python), str(script)], input=payload, capture_output=True, timeout=timeout,
                              cwd=str(config.project_dir()), env=env, **winproc.hidden_popen_kwargs())
    except subprocess.TimeoutExpired:
        result["error"] = f"server.py не ответил за {timeout} с"
        return result
    except OSError as exc:
        result["error"] = f"не удалось запустить {python}: {exc}"
        return result
    result["seconds"] = round(time.monotonic() - started, 2)
    responses = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            result["error"] = f"в stdout не JSON: {line[:200]}"
            return result
        if isinstance(message, dict) and "id" in message:
            responses[message["id"]] = message
    try:
        init = responses[1]["result"]
        tools = responses[2]["result"]["tools"]
        status = json.loads(responses[3]["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        stderr = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        result["error"] = "server.py ответил не полностью" + (f": {stderr[-1]}" if stderr else "")
        return result
    result.update(ok=True, server_version=init.get("serverInfo", {}).get("version"), tools=len(tools),
                  database_ok=bool(status.get("database_ok")), database_path=status.get("database_path"),
                  schema_version=status.get("schema_version"))
    if not result["database_ok"]:
        result["warning"] = status.get("message") or "база недоступна или схема устарела"
    return result
