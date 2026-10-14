"""Пути и настройки единого запуска.

Все рабочие файлы лежат рядом с базой (по умолчанию <проект>\data), а не рядом с программой: так EXE-сборку можно
заменить или удалить, не трогая базу, настройки и журналы.

data\launcher.json  — настройки (создаются при первом запуске; секретов не содержат);
data\logs\         — tracker.log (launcher, импорт), tunnel.log (вывод tunnel-client), с ротацией;
data\run\          — launcher.lock (один экземпляр), control.json (адрес управления работающим экземпляром).
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

from .. import db

CONFIG_FILE = "launcher.json"
LOG_DIR, RUN_DIR = "logs", "run"
TUNNEL_PROFILE_NAME = "fitness-local"

DEFAULTS = {
    # Путь к tunnel-client.exe. Пусто — искать автоматически (PATH, %LOCALAPPDATA%\Programs, Загрузки).
    "tunnel_client": "",
    # Профиль tunnel-client. Пусто — %APPDATA%\tunnel-client\fitness-local.yaml. Launcher его только читает.
    "tunnel_profile": "",
    # false — launcher не запускает туннель (например, если он запускается другим способом).
    "tunnel_enabled": True,
    "import_enabled": True,
    "import_interval_sec": 30,
    "dashboard_port": 8765,
    # Как часто проверять, что server.py отвечает по MCP (минуты; 0 — только при запуске и по команде).
    "mcp_check_minutes": 60,
}


def project_dir():
    """Папка проекта (где лежит server.py). В EXE-сборке — ближайшая папка выше exe с server.py и fitness/,
    иначе путь из переменной FITNESS_PROJECT_DIR."""
    if not getattr(sys, "frozen", False):
        return db.PROJECT_DIR
    env = os.environ.get("FITNESS_PROJECT_DIR")
    if env:
        return Path(env)
    here = Path(sys.executable).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "server.py").is_file() and (candidate / "fitness").is_dir():
            return candidate
    return here


def server_script():
    return project_dir() / "server.py"


def data_dir(db_path=None):
    return db.resolve_db_path(db_path).parent


def paths(db_path=None):
    data = data_dir(db_path)
    return {"data": data, "config": data / CONFIG_FILE, "logs": data / LOG_DIR, "run": data / RUN_DIR,
            "tracker_log": data / LOG_DIR / "tracker.log", "tunnel_log": data / LOG_DIR / "tunnel.log",
            "lock": data / RUN_DIR / "launcher.lock", "control": data / RUN_DIR / "control.json"}


def load(db_path=None, *, write_defaults=True):
    """Настройки: значения по умолчанию + data\launcher.json. Неверный файл не исправляется молча: ошибка."""
    path = paths(db_path)["config"]
    settings = dict(DEFAULTS)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ValueError(f"Файл настроек {path} повреждён: {exc}. Исправьте или удалите его.") from None
        if not isinstance(stored, dict):
            raise ValueError(f"Файл настроек {path}: ожидается JSON-объект.")
        settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    elif write_defaults:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return settings


# ---------- tunnel-client ----------

def default_profile():
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "tunnel-client" / f"{TUNNEL_PROFILE_NAME}.yaml"


def profile_path(settings):
    return Path(settings["tunnel_profile"]) if settings.get("tunnel_profile") else default_profile()


def _version_key(path):
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)", str(path))
    return tuple(int(x) for x in match.groups()) if match else (0, 0, 0)


def find_tunnel_client(settings):
    """Путь к tunnel-client.exe или None."""
    configured = settings.get("tunnel_client")
    if configured:
        return Path(configured) if Path(configured).is_file() else None
    found = shutil.which("tunnel-client")
    if found:
        return Path(found)
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    candidates = [local / "Programs" / "tunnel-client" / "tunnel-client.exe"]
    downloads = home / "Downloads"
    if downloads.is_dir():
        candidates += sorted(downloads.glob("tunnel-client*/tunnel-client.exe"), key=_version_key, reverse=True)
    return next((c for c in candidates if c.is_file()), None)


def read_profile(path):
    """Нужное launcher'у из профиля: адрес /healthz, имя переменной с ключом, команда MCP. Только чтение."""
    text = Path(path).read_text(encoding="utf-8-sig")
    info = {"listen_addr": "127.0.0.1:8080", "api_key_env": None, "mcp_command": None}
    match = re.search(r'^\s*listen_addr:\s*"?([^"\s#]+)"?', text, re.M)
    if match:
        info["listen_addr"] = match.group(1)
    match = re.search(r'^\s*api_key:\s*"?env:([A-Za-z_][A-Za-z0-9_]*)"?', text, re.M)
    if match:
        info["api_key_env"] = match.group(1)
    match = re.search(r'^\s*command:\s*"([^"]+)"', text, re.M)
    if match:
        info["mcp_command"] = match.group(1)
    return info


def python_for_mcp():
    """Тот же python, которым tunnel-client запускает server.py (команда профиля: «python ...»)."""
    found = shutil.which("python")
    if found and "WindowsApps" not in found:
        return found
    if not getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        console = exe.with_name("python.exe")
        return str(console if console.is_file() else exe)
    return found or "python"
