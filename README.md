# FITNESS Tracker

Локальный трекер фитнеса: хранит вес, замеры, фото, тренировки, питание, алкоголь, заметки и данные часов
(шаги, пульс, сон из Health Connect) в SQLite на вашем компьютере. ChatGPT работает с этими данными через
MCP-сервер: вы пишете обычными сообщениями, ChatGPT сохраняет записи и анализирует историю.

Данные никуда не отправляются: нет облачной базы, внешних API и платных сервисов. Интерфейс на русском.

## Возможности

- **MCP-сервер** (`server.py`, stdio): 26 инструментов — записать и прочитать замеры, тренировки (упражнения, подходы,
  RIR), питание (КБЖУ, признак приблизительной оценки), алкоголь, заметки; поиск, статистика за период,
  удаление в два шага с кодом подтверждения и резервной копией.
- **Веб-панель** (`dashboard.py`, только `127.0.0.1:8765`): графики и история, страницы «Здоровье»,
  «Синхронизация», «Фото» (загрузка и корзина фото).
- **Импорт Health Connect**: шаги, пульс, сон из JSON-выгрузки; проверка файла целиком, одна транзакция,
  повторный импорт не создаёт дублей. Автоимпорт из входящей папки (`health_inbox.py --watch`).
- **Единый запуск для Windows** (`tracker.pyw`): значок в трее, tunnel-client под наблюдением, автоимпорт,
  панель по запросу.
- Резервные копии базы, явные миграции схемы, строгая проверка дат, чисел и единиц.

Подробнее: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Требования

- Python **3.11** или новее. Сторонних пакетов нет, `pip install` не нужен.
- Ядро, MCP-сервер, панель и импорт работают на любой ОС. Единый запуск (`tracker.pyw`, трей, автозапуск,
  `.bat` и `.ps1`) — только Windows.
- Для работы с ChatGPT: официальный tunnel-client для подключения локального MCP-сервера, профиль и ключ
  в переменной окружения (ниже). Без него трекер, панель и импорт работают локально.
- Для данных часов: Android-телефон с Health Connect и отдельное приложение-мост, которое пишет файлы в формате
  [docs/HEALTH_EXPORT_FORMAT.md](docs/HEALTH_EXPORT_FORMAT.md). Приложение-мост в этот репозиторий **не входит**.

## Установка

```
git clone <адрес репозитория> FITNESS-Tracker
cd FITNESS-Tracker
python --version            # 3.11+
```

Больше ничего ставить не нужно. Папка `data/` и база `data/fitness.db` создаются сами при первой записи.
Путь к базе можно переопределить переменной окружения `FITNESS_DB_PATH`.

## Запуск

**MCP-сервер** (обычно его запускает tunnel-client, вручную нужен только для проверки):

```
python server.py
```

Сервер общается через stdin/stdout по протоколу MCP; stdout — только сообщения протокола, диагностика — в stderr.

**Веб-панель:**

```
python dashboard.py            # затем открыть http://127.0.0.1:8765/
python dashboard.py --port 8800
```

**Импорт выгрузки Health Connect:**

```
python import_health.py --dry-run examples/health-sample.json   # проверка без записи
python import_health.py ПУТЬ_К_ФАЙЛУ_ИЛИ_ПАПКЕ
python health_inbox.py --watch                                   # следить за data/health_inbox
```

Импорт не создаёт базу и не меняет её схему: сначала сделайте хотя бы одну запись (через MCP) или выполните
`python migrate_db.py` для существующей базы старой версии.

**Единый запуск (Windows):** двойной щелчок по `tracker.pyw`. Подробности: [QUICKSTART.md](QUICKSTART.md).
Настройки создаются в `data/launcher.json` при первом запуске; пример — [launcher.example.json](launcher.example.json).

## Подключение к ChatGPT через tunnel-client

1. Установите tunnel-client и создайте в ChatGPT приложение (коннектор) по его инструкции.
2. Скопируйте пример профиля [examples/tunnel-client/fitness-local.example.yaml](examples/tunnel-client/fitness-local.example.yaml)
   в `%APPDATA%\tunnel-client\fitness-local.yaml` и заполните свои значения. В поле `command` укажите
   абсолютный путь к `server.py` вашей копии.
3. Ключ задайте переменной окружения пользователя `CONTROL_PLANE_API_KEY` — не в файле профиля и не в коде.
4. Запустите tunnel-client с профилем `fitness-local` (или `tracker.pyw`, он сделает это сам).
5. В ChatGPT обновите приложение/коннектор, чтобы он увидел список инструментов.

Работа через tunnel-client проверялась автором на своей установке. Установка с нуля по этой инструкции
на другом компьютере пока не проверялась.

## Тесты

```
python -m unittest discover -s tests -t .
```

Тесты используют только временные базы и синтетические данные; рабочая `data/fitness.db` не открывается.
Часть тестов лаунчера рассчитана на Windows.

## Где хранятся данные

| Путь | Что |
|---|---|
| `data/fitness.db` | база SQLite |
| `data/backups/` | резервные копии (перед миграцией и удалением) |
| `data/photos/` | фото |
| `data/health_inbox/`, `health_processed/`, `health_failed/` | очередь импорта Health Connect |
| `data/logs/`, `data/run/` | журналы и состояние единого запуска |

Всё это личные данные: папка `data/` исключена в `.gitignore`. Не публикуйте её и не добавляйте реальные
выгрузки `fitness-health-*.json` в репозиторий.

## Ограничения

- Однопользовательское локальное приложение: нет учётных записей и шифрования базы; доступ к данным — у всех,
  кто имеет доступ к вашему профилю Windows.
- Панель слушает только `127.0.0.1` и не предназначена для доступа из сети.
- Схема базы обновляется только явной командой `python migrate_db.py` (с резервной копией).
- Формат выгрузки Health Connect — собственный формат проекта (версия 1), а не стандарт Google.

## Документация

- [QUICKSTART.md](QUICKSTART.md) — единый запуск в Windows
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — устройство, схема базы, инструменты MCP
- [docs/HEALTH_SCHEMA.md](docs/HEALTH_SCHEMA.md), [docs/HEALTH_EXPORT_FORMAT.md](docs/HEALTH_EXPORT_FORMAT.md) — данные Health Connect
- [docs/HEALTH_AUTO_IMPORT.md](docs/HEALTH_AUTO_IMPORT.md), [docs/HEALTH_PHONE_TRANSFER.md](docs/HEALTH_PHONE_TRANSFER.md),
  [docs/HEALTH_SYNCTHING.md](docs/HEALTH_SYNCTHING.md), [docs/HEALTH_MCP_IMPORT.md](docs/HEALTH_MCP_IMPORT.md) — передача и импорт
- [docs/UPDATE_AND_RECOVERY.md](docs/UPDATE_AND_RECOVERY.md) — обновление и восстановление

## Лицензия

Пока не выбрана.
