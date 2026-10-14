"""Локальная веб-панель FITNESS Tracker (только чтение).

Запуск:
    python dashboard.py
Затем открыть в браузере http://127.0.0.1:8765/

Панель слушает только 127.0.0.1, открывает базу в режиме только для чтения
и никак не связана с MCP-сервером (server.py) и туннелем.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness.web.app import main  # noqa: E402

if __name__ == "__main__":
    main()
