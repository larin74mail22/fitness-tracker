"""FITNESS Tracker: единый запуск и управление (с консолью).

    python tracker.py                 запустить трекер со значком в трее (лучше: tracker.pyw — без окна консоли)
    python tracker.py --no-tray       запустить без значка (остановка: Ctrl+C или --stop из другого окна)
    python tracker.py --status        состояние работающего трекера
    python tracker.py --open          открыть веб-панель
    python tracker.py --import-now    проверить входящую папку Health Connect сейчас
    python tracker.py --restart [tunnel|import|dashboard|all]
    python tracker.py --check-mcp     проверить, что server.py отвечает по MCP (только чтение)
    python tracker.py --stop          остановить трекер

Подробности: QUICKSTART.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness.launcher.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
