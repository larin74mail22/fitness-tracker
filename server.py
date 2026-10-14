"""MCP-сервер FITNESS Tracker (stdio).

Запускается tunnel-client'ом (профиль fitness-local) командой:
    python server.py

Вся логика — в пакете fitness/. stdout — только сообщения MCP, диагностика — в stderr.
"""

import sys
from pathlib import Path

# Пакет fitness лежит рядом с этим файлом; текущая папка при запуске может быть любой.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fitness.protocol import main  # noqa: E402

if __name__ == "__main__":
    main()
