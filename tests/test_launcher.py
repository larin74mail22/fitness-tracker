"""Единый запуск (fitness.launcher): один экземпляр, туннель и его сбои, автоимпорт, панель по запросу, управление,
проверка MCP, журнал, остановка и аварийное завершение.

Вместо tunnel-client — поддельный процесс (tests/fake_tunnel.py пишется во временную папку). Все базы и папки
временные, в путях есть пробелы и кириллица. Рабочая база проекта и настоящий tunnel-client не используются.
"""

import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from fitness import db, tools
from fitness.launcher import cli, components, config, supervisor as sv, winproc
from tests import health_samples as hs

PROJECT = Path(__file__).resolve().parent.parent
OLD = time.time() - 3600

FAKE_TUNNEL = r'''
import http.server, re, sys, threading, time, os
mode, pid_file, profile = sys.argv[1], sys.argv[2], sys.argv[3]
open(pid_file, "a").write(f"{os.getpid()}\n")
addr = re.search(r'listen_addr:\s*"?([^"\s]+)', open(profile, encoding="utf-8").read()).group(1)
host, port = addr.rsplit(":", 1)
print("fake tunnel started", flush=True)
print('{"msg":"provided","noise":true}', flush=True)
if mode.startswith("crash"):
    time.sleep(float(mode.split(":")[1]))
    print("fake tunnel crashing", flush=True)
    sys.exit(3)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.HTTPServer((host, int(port)), H).serve_forever()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate, timeout=15.0, step=0.05):
    return sv.wait_until(predicate, timeout, step)


class LauncherTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness launcher тест-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "данные с пробелом"
        self.db_path = self.data / "fitness.db"
        self.assertNotEqual(self.db_path.resolve(), db.DEFAULT_DB_PATH.resolve())
        db.open_database(self.db_path).close()
        self.log = logging.getLogger("fitness.tracker.test")
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False
        self.port = free_port()
        self.profile = self.tmp / "профиль tunnel.yaml"
        self.profile.write_text(
            f'control_plane:\n  api_key: "env:FAKE_TUNNEL_KEY"\nhealth:\n  listen_addr: "127.0.0.1:{self.port}"\n'
            f'mcp:\n  commands:\n    - channel: main\n      command: "python server.py"\n', encoding="utf-8")
        self.fake = self.tmp / "fake_tunnel.py"
        self.fake.write_text(FAKE_TUNNEL, encoding="utf-8")
        self.pid_file = self.tmp / "pids.txt"
        env = mock.patch.dict(os.environ, {"FAKE_TUNNEL_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)

    def settings(self, **overrides):
        values = dict(config.DEFAULTS, tunnel_profile=str(self.profile), import_interval_sec=5,
                      dashboard_port=free_port(), mcp_check_minutes=0)
        values.update(overrides)
        return values

    def make_tunnel(self, mode="run", settings=None):
        output = logging.getLogger("fitness.tunnel.test")
        output.propagate = False
        self.output_lines = []
        handler = logging.Handler()
        handler.emit = lambda record: self.output_lines.append(record.getMessage())
        output.handlers = [handler]
        tunnel = components.Tunnel(settings or self.settings(), self.log, output)
        tunnel.command_override = [sys.executable, str(self.fake), mode, str(self.pid_file)]
        tunnel.image_name = "fake-tunnel-not-running.exe"
        tunnel.PROBE_SEC = 0.2
        tunnel.BACKOFF_START = 0.2
        self.addCleanup(tunnel.stop, 5)
        return tunnel

    def pids(self):
        if not self.pid_file.exists():
            return []
        return [int(x) for x in self.pid_file.read_text().split()]


class InstanceTest(LauncherTestCase):
    def test_second_lock_is_refused_and_released_after_close(self):
        path = config.paths(self.db_path)["lock"]
        first = sv.InstanceLock(path).acquire()
        with self.assertRaises(sv.AlreadyRunning):
            sv.InstanceLock(path).acquire()
        self.assertTrue(sv.is_running(self.db_path))
        first.release()
        self.assertFalse(sv.is_running(self.db_path))

    def test_second_supervisor_is_not_started(self):
        first = sv.Supervisor(self.db_path, self.settings(tunnel_enabled=False, import_enabled=False))
        first.start()
        self.addCleanup(first.stop)
        second = sv.Supervisor(self.db_path, self.settings(tunnel_enabled=False, import_enabled=False))
        with self.assertRaises(sv.AlreadyRunning):
            second.start()
        self.assertEqual(first.tunnel.state, components.DISABLED)

    def test_other_database_is_an_independent_instance(self):
        other = self.tmp / "другая" / "fitness.db"
        db.open_database(other).close()
        a = sv.Supervisor(self.db_path, self.settings(tunnel_enabled=False, import_enabled=False)).start()
        self.addCleanup(a.stop)
        b = sv.Supervisor(other, self.settings(tunnel_enabled=False, import_enabled=False)).start()
        self.addCleanup(b.stop)
        self.assertTrue(sv.is_running(self.db_path) and sv.is_running(other))


class TunnelTest(LauncherTestCase):
    def test_start_ready_and_stop_kills_process(self):
        tunnel = self.make_tunnel()
        tunnel.start()
        self.assertTrue(wait_until(lambda: tunnel.state == components.RUNNING), tunnel.status())
        self.assertTrue(tunnel.ready)
        pid = tunnel.status()["pid"]
        self.assertTrue(winproc.pid_alive(pid))
        tunnel.stop()
        self.assertEqual(tunnel.state, components.STOPPED)
        self.assertTrue(wait_until(lambda: not winproc.pid_alive(pid), 10))
        self.assertIn("fake tunnel started", self.output_lines)
        self.assertFalse(any('"msg":"provided"' in line for line in self.output_lines), "служебный шум отфильтрован")

    def test_crash_is_logged_and_restarted(self):
        tunnel = self.make_tunnel("crash:0.3")
        with self.assertLogs(self.log, "ERROR") as logs:
            tunnel.start()
            self.assertTrue(wait_until(lambda: tunnel.restarts >= 2, 20), tunnel.status())
        self.assertGreaterEqual(len(self.pids()), 2, "после сбоя запущен новый процесс")
        self.assertTrue(any("кодом 3" in line and "fake tunnel crashing" in line for line in logs.output), logs.output)

    def test_restart_command_replaces_process(self):
        tunnel = self.make_tunnel()
        tunnel.start()
        self.assertTrue(wait_until(lambda: tunnel.state == components.RUNNING))
        first = tunnel.status()["pid"]
        tunnel.restart()
        self.assertTrue(wait_until(lambda: tunnel.status()["pid"] not in (None, first)
                                   and tunnel.state == components.RUNNING, 15), tunnel.status())
        self.assertTrue(wait_until(lambda: not winproc.pid_alive(first), 10))

    def test_running_tunnel_client_is_not_duplicated(self):
        tunnel = self.make_tunnel()
        tunnel.EXTERNAL_WAIT = 0.2
        with mock.patch.object(winproc, "find_processes", return_value=[424242]):
            tunnel.start()
            self.assertTrue(wait_until(lambda: tunnel.state == components.EXTERNAL))
            time.sleep(0.5)
            self.assertEqual(self.pids(), [], "второй tunnel-client не запускается")
            self.assertIn("424242", tunnel.message)
        self.assertTrue(wait_until(lambda: tunnel.state == components.RUNNING, 15), "чужой процесс исчез — запуск свой")

    def test_busy_health_port_is_reported_not_duplicated(self):
        tunnel = self.make_tunnel()
        tunnel.EXTERNAL_WAIT = 0.2
        with mock.patch.object(components, "http_ok", return_value=True):
            tunnel.start()
            self.assertTrue(wait_until(lambda: tunnel.state == components.EXTERNAL))
        self.assertEqual(self.pids(), [])

    def test_missing_executable_profile_or_key(self):
        settings = self.settings(tunnel_client=str(self.tmp / "нет.exe"))
        tunnel = components.Tunnel(settings, self.log, self.log)
        self.assertIsNone(tunnel._prepare())
        self.assertEqual(tunnel.state, components.ERROR)
        self.assertIn("tunnel-client.exe", tunnel.message)

        tunnel = self.make_tunnel(settings=self.settings(tunnel_profile=str(self.tmp / "нет.yaml")))
        self.assertIsNone(tunnel._prepare())
        self.assertIn("профиль", tunnel.message)

        tunnel = self.make_tunnel()
        with mock.patch.dict(os.environ, {"FAKE_TUNNEL_KEY": ""}), \
                mock.patch.object(winproc, "user_environment_value", return_value=None):
            self.assertIsNone(tunnel._prepare())
        self.assertIn("FAKE_TUNNEL_KEY", tunnel.message)

    def test_key_from_user_environment_is_passed_but_not_logged(self):
        tunnel = self.make_tunnel()
        with mock.patch.dict(os.environ, {"FAKE_TUNNEL_KEY": ""}), \
                mock.patch.object(winproc, "user_environment_value", return_value="secret-value") as reg:
            cmd, env, _ = tunnel._prepare()
        reg.assert_called_once_with("FAKE_TUNNEL_KEY")
        self.assertEqual(env["FAKE_TUNNEL_KEY"], "secret-value")
        self.assertNotIn("secret-value", " ".join(cmd) + tunnel.message)

    def test_disabled_tunnel_is_not_started(self):
        tunnel = self.make_tunnel(settings=self.settings(tunnel_enabled=False))
        tunnel.start()
        self.assertEqual(tunnel.state, components.DISABLED)
        self.assertIsNone(tunnel._thread)

    def test_profile_parsing_reads_only_needed_fields(self):
        info = config.read_profile(self.profile)
        self.assertEqual(info, {"listen_addr": f"127.0.0.1:{self.port}", "api_key_env": "FAKE_TUNNEL_KEY",
                                "mcp_command": "python server.py"})

    @unittest.skipUnless(os.name == "nt", "Job Object — только Windows")
    def test_job_object_kills_children_when_closed(self):
        job = winproc.Job()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                **winproc.hidden_popen_kwargs())
        self.addCleanup(proc.kill)
        self.assertTrue(job.assign(proc))
        job.close()
        self.assertTrue(wait_until(lambda: proc.poll() is not None, 10))


class ImportWatcherTest(LauncherTestCase):
    def count_steps(self):
        conn = db.connect(self.db_path, create=False)
        try:
            return conn.execute("SELECT COUNT(*) FROM step_intervals").fetchone()[0]
        finally:
            conn.close()

    def put(self, name, doc):
        inbox = self.data / "health_inbox"
        path = hs.write(inbox, name, doc)
        os.utime(path, (OLD, OLD))
        return path

    def test_imports_automatically_and_does_not_duplicate(self):
        notes = []
        watcher = components.ImportWatcher(self.settings(), self.log, self.db_path,
                                           notify=lambda t, x: notes.append((t, x)))
        doc = hs.day_file(1)
        self.put("a.json", doc)
        run = watcher.run_once()
        self.assertEqual(run["summary"]["imported"], 1)
        steps = self.count_steps()
        self.assertEqual(watcher.state, components.RUNNING)
        self.assertEqual(watcher.imported, 1)
        self.assertTrue(notes and "Файл №1" in notes[0][1])
        # Тот же файл ещё раз (повторная выгрузка) — уже в базе, дубликатов нет.
        self.put("a-copy.json", doc)
        run = watcher.run_once()
        self.assertEqual(run["summary"]["already_imported"], 1)
        self.assertEqual(self.count_steps(), steps)
        conn = db.connect(self.db_path, create=False)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM import_batches WHERE status = 'ok'").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(len(list((self.data / "health_processed").glob("*.json"))), 2)

    def test_failed_file_sets_error_and_is_kept(self):
        watcher = components.ImportWatcher(self.settings(), self.log, self.db_path)
        inbox = self.data / "health_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        bad = inbox / "плохой.json"
        bad.write_text("{не json", encoding="utf-8")
        os.utime(bad, (OLD, OLD))
        watcher.run_once()
        self.assertEqual(watcher.state, components.ERROR)
        self.assertTrue((self.data / "health_failed" / "плохой.json").is_file())

    def test_loop_survives_crash_and_restart_keeps_monitoring(self):
        calls = []

        def flaky(db_path):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("папка недоступна")
            return {"files": [], "error": None, "summary": {}, "handoff": None, "dry_run": False, "inbox": ""}

        watcher = components.ImportWatcher(self.settings(), self.log, self.db_path, process=flaky)
        with self.assertLogs(self.log, "ERROR"):
            watcher.start()
            watcher.run_now()
            self.assertTrue(wait_until(lambda: len(calls) >= 2, 10))
        self.assertEqual(watcher.state, components.RUNNING)
        watcher.restart()
        before = len(calls)
        watcher.run_now()
        self.assertTrue(wait_until(lambda: len(calls) > before, 10), "после перезапуска наблюдение продолжается")
        watcher.stop()
        self.assertEqual(watcher.state, components.STOPPED)

    def test_locked_inbox_is_not_an_error(self):
        from fitness import health_inbox
        lock = health_inbox._Lock(health_inbox.ensure_folders(self.db_path)["state"] / health_inbox.LOCK_FILE)
        with lock:
            watcher = components.ImportWatcher(self.settings(), self.log, self.db_path)
            watcher.run_once()
        self.assertEqual(watcher.state, components.RUNNING)
        self.assertIn("другой запуск", watcher.message)

    def test_stop_waits_for_running_import(self):
        started, finished = [], []

        def slow(db_path):
            started.append(1)
            time.sleep(0.5)
            finished.append(1)
            return {"files": [], "error": None, "summary": {}, "handoff": None, "dry_run": False, "inbox": ""}

        watcher = components.ImportWatcher(self.settings(), self.log, self.db_path, process=slow)
        watcher.start()
        self.assertTrue(wait_until(lambda: started))
        watcher.stop()
        self.assertEqual(len(finished), len(started), "проход импорта не обрывается посередине")


class SupervisorTest(LauncherTestCase):
    def start(self, **settings):
        sup = sv.Supervisor(self.db_path, self.settings(**settings))
        sup.dashboard._open_browser = lambda url: self.opened.append(url)
        self.opened = []
        sup.start()
        self.addCleanup(sup.stop)
        return sup

    def test_control_commands(self):
        sup = self.start(tunnel_enabled=False)
        status = sv.send_command("status", db_path=self.db_path)["status"]
        self.assertEqual(status["components"]["tunnel"]["state"], components.DISABLED)
        self.assertEqual(status["components"]["dashboard"]["state"], components.STOPPED, "панель не запущена заранее")
        self.assertIn("Автоимпорт", "\n".join(sv.describe_status(status)))
        self.assertTrue(sv.send_command("import_now", db_path=self.db_path)["ok"])
        self.assertTrue(sv.send_command("restart", {"component": "import"}, db_path=self.db_path)["ok"])
        self.assertTrue(wait_until(lambda: sup.importer.state == components.RUNNING))
        bad = sv.send_command("rm -rf", db_path=self.db_path)
        self.assertFalse(bad["ok"])
        self.assertTrue(sv.send_command("stop", db_path=self.db_path)["ok"])
        self.assertTrue(wait_until(lambda: not sv.is_running(self.db_path), 30))
        self.assertFalse(config.paths(self.db_path)["control"].exists())
        with self.assertRaises(sv.NotRunning):
            sv.send_command("status", db_path=self.db_path)

    def test_wrong_token_is_rejected(self):
        self.start(tunnel_enabled=False, import_enabled=False)
        info = json.loads(config.paths(self.db_path)["control"].read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as s:
            s.sendall(b'{"token": "x", "command": "stop"}\n')
            answer = json.loads(s.makefile(encoding="utf-8").readline())
        self.assertFalse(answer["ok"])
        self.assertTrue(sv.is_running(self.db_path))

    def test_dashboard_opens_on_request_only(self):
        sup = self.start(tunnel_enabled=False, import_enabled=False)
        self.assertIsNone(sup.dashboard.server)
        response = sv.send_command("open", db_path=self.db_path)
        self.assertTrue(response["ok"])
        self.assertEqual(self.opened, [response["url"]])
        with urllib.request.urlopen(response["url"], timeout=10) as page:
            self.assertEqual(page.status, 200)
            self.assertIn("FITNESS", page.read().decode("utf-8"))
        sv.send_command("open", db_path=self.db_path)
        self.assertEqual(len(self.opened), 2, "повторное открытие не запускает второй сервер")
        sup.stop()
        self.assertIsNone(sup.dashboard.server)

    def test_full_start_with_tunnel_and_orderly_stop(self):
        sup = sv.Supervisor(self.db_path, self.settings())
        sup.tunnel.command_override = [sys.executable, str(self.fake), "run", str(self.pid_file)]
        sup.tunnel.image_name = "fake-tunnel-not-running.exe"
        sup.tunnel.PROBE_SEC = 0.2
        sup.start()
        self.addCleanup(sup.stop)
        self.assertTrue(wait_until(lambda: sup.tunnel.state == components.RUNNING and sup.mcp_check, 30),
                        sup.status())
        self.assertTrue(sup.mcp_check["ok"], sup.mcp_check)
        self.assertEqual(sup.overall(), "ok")
        pid = sup.tunnel.status()["pid"]
        sup.stop()
        self.assertTrue(wait_until(lambda: not winproc.pid_alive(pid), 10))
        self.assertEqual(sup.tunnel.state, components.STOPPED)
        self.assertEqual(sup.importer.state, components.STOPPED)
        sup.stop()                                             # повторная остановка безопасна

    def test_tunnel_env_points_to_selected_database(self):
        sup = sv.Supervisor(self.db_path, self.settings())
        self.assertEqual(sup.tunnel.env_overrides["FITNESS_DB_PATH"], str(self.db_path))
        sup.tunnel.command_override = ["x"]
        sup.tunnel.image_name = "x.exe"
        _, env, _ = sup.tunnel._prepare()
        self.assertEqual(env["FITNESS_DB_PATH"], str(self.db_path))


class McpCheckTest(LauncherTestCase):
    def test_selfcheck_against_temporary_database(self):
        result = components.mcp_selfcheck(env_overrides={"FITNESS_DB_PATH": str(self.db_path)})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["database_ok"])
        self.assertEqual(result["tools"], len(tools.TOOLS))
        self.assertEqual(Path(result["database_path"]), self.db_path)

    def test_selfcheck_reports_missing_database_without_creating_it(self):
        missing = self.tmp / "нет базы" / "fitness.db"
        result = components.mcp_selfcheck(env_overrides={"FITNESS_DB_PATH": str(missing)})
        self.assertTrue(result["ok"])
        self.assertFalse(result["database_ok"])
        self.assertFalse(missing.exists())

    def test_selfcheck_reports_broken_server(self):
        broken = self.tmp / "broken server.py"
        broken.write_text("import sys; sys.stderr.write('boom\\n')", encoding="utf-8")
        result = components.mcp_selfcheck(script=broken)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])


class ConfigAndLoggingTest(LauncherTestCase):
    def test_defaults_written_once_and_bad_file_reported(self):
        settings = config.load(self.db_path)
        self.assertEqual(settings, config.DEFAULTS)
        path = config.paths(self.db_path)["config"]
        self.assertTrue(path.is_file())
        path.write_text('{"import_interval_sec": 60, "unknown": 1}', encoding="utf-8")
        self.assertEqual(config.load(self.db_path)["import_interval_sec"], 60)
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError):
            config.load(self.db_path)

    def test_config_holds_no_secrets_and_lives_next_to_database(self):
        config.load(self.db_path)
        paths = config.paths(self.db_path)
        for key in ("config", "logs", "run"):
            self.assertEqual(paths[key].parent, self.data)
        text = paths["config"].read_text(encoding="utf-8").lower()
        self.assertNotIn("key", text.replace("api_key_env", ""))

    def test_russian_log_is_utf8(self):
        logs = self.data / "logs"
        log = sv.setup_logging(logs)[0]
        self.addCleanup(sv.close_logging)
        log.info("Импорт: файл №1 — проверка «ёлки»")
        sv.close_logging()
        self.assertIn("файл №1 — проверка «ёлки»", (logs / "tracker.log").read_text(encoding="utf-8"))

    def test_finds_newest_downloaded_tunnel_client(self):
        home = self.tmp / "home"
        for version in ("0.0.9", "0.0.16"):
            exe = home / "Downloads" / f"tunnel-client-v{version}-windows-amd64" / "tunnel-client.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
        with mock.patch.object(config.shutil, "which", return_value=None), \
                mock.patch.object(Path, "home", return_value=home), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(home / "nolocal")}):
            found = config.find_tunnel_client(dict(config.DEFAULTS))
        self.assertIn("0.0.16", str(found))


class CliProcessTest(LauncherTestCase):
    """tracker.py как отдельный процесс: запуск в фоне, повторный запуск, --status, --stop, аварийное завершение."""

    def run_cli(self, *args, timeout=60):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        return subprocess.run([sys.executable, "-B", str(PROJECT / "tracker.py"), "--db", str(self.db_path), *args],
                              capture_output=True, timeout=timeout, env=env, cwd=str(self.tmp))

    def start_background(self):
        config.paths(self.db_path)["config"].parent.mkdir(parents=True, exist_ok=True)
        config.paths(self.db_path)["config"].write_text(
            json.dumps({"tunnel_enabled": False, "mcp_check_minutes": 0, "dashboard_port": free_port()}),
            encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-B", str(PROJECT / "tracker.py"), "--no-tray", "--db",
                                 str(self.db_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=str(self.tmp), **winproc.hidden_popen_kwargs())
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        control = config.paths(self.db_path)["control"]

        def started():
            try:
                return json.loads(control.read_text(encoding="utf-8"))["pid"] == proc.pid
            except (OSError, ValueError, KeyError):
                return False

        self.assertTrue(wait_until(started, 30))
        return proc

    def test_background_status_duplicate_and_stop(self):
        proc = self.start_background()
        status = self.run_cli("--status")
        self.assertEqual(status.returncode, 0, status.stderr)
        text = status.stdout.decode("utf-8")              # русский текст в UTF-8 без PYTHONIOENCODING
        self.assertIn("Автоимпорт: работает", text)
        second = self.run_cli("--no-tray")
        self.assertEqual(second.returncode, 0)
        self.assertIn("уже запущен", second.stdout.decode("utf-8"))
        self.assertIsNone(proc.poll(), "первый экземпляр продолжает работать")
        stop = self.run_cli("--stop")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(proc.wait(30), 0)
        self.assertEqual(self.run_cli("--status").returncode, cli.EXIT_NOT_RUNNING)
        log = (self.data / "logs" / "tracker.log").read_text(encoding="utf-8")
        self.assertIn("второй экземпляр не создан", log)
        self.assertIn("FITNESS Tracker остановлен", log)

    def test_restart_after_hard_kill(self):
        proc = self.start_background()
        proc.kill()                                      # аварийное завершение: блокировка снимается системой
        proc.wait(10)
        self.assertFalse(sv.is_running(self.db_path))
        again = self.start_background()
        self.assertEqual(self.run_cli("--status").returncode, 0)
        self.assertEqual(self.run_cli("--stop").returncode, 0)
        self.assertEqual(again.wait(30), 0)

    def test_check_mcp_command(self):
        result = self.run_cli("--check-mcp", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.decode("utf-8"))
        self.assertTrue(data["ok"] and data["database_ok"])

    def test_health_inbox_cli_works_from_path_with_spaces(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        for args in (["--status"], ["--dry-run"], ["--retry-failed"]):
            result = subprocess.run([sys.executable, "-B", str(PROJECT / "health_inbox.py"), "--db", str(self.db_path),
                                     *args], capture_output=True, timeout=60, cwd=str(self.tmp), env=env)
            self.assertIn(result.returncode, (0, 2), (args, result.stderr.decode("utf-8", "replace")))
            self.assertTrue(result.stdout.decode("utf-8"), args)


if __name__ == "__main__":
    unittest.main()
