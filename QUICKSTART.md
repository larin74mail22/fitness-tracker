# FITNESS Tracker 1.0 — быстрый старт

Трекер хранит данные на этом компьютере (`data\fitness.db`), ChatGPT работает с ними через MCP-сервер
(`server.py`), который запускает tunnel-client (профиль `fitness-local`). Выгрузки Health Connect приходят с
телефона через Syncthing в `data\health_syncthing` и импортируются автоматически.

## Запуск

Двойной щелчок по **`tracker.pyw`** (или `pythonw tracker.pyw`). Окна консоли нет — у часов появляется значок
с гантелей:

| Цвет | Значение |
|---|---|
| зелёный | туннель готов, MCP отвечает, импорт работает |
| жёлтый | туннель ещё подключается (например, нет интернета) |
| красный | ошибка: подробности — пункт «Состояние…» и журнал |

Один запуск поднимает всё нужное в фоне:
- **tunnel-client** с профилем `%APPDATA%\tunnel-client\fitness-local.yaml` (профиль не меняется), он запускает
  `python server.py` — MCP для ChatGPT; после сбоя tunnel-client перезапускается сам (пауза 5 с … 5 мин);
- **автоимпорт Health Connect** раз в 30 секунд (то же, что `health_import_inbox.bat --watch`);
- **веб-панель** не запускается, пока её не попросить.

Повторный запуск ничего не дублирует: появится сообщение «уже запущен». Если tunnel-client уже запущен вручную,
трекер второй не запускает (состояние «запущен вне launcher») и подхватит туннель, когда тот завершится.

## Меню значка (правая кнопка)

- **Открыть панель** — запускает панель и открывает http://127.0.0.1:8765/ (двойной щелчок по значку — то же);
- **Состояние…** — туннель, импорт, проверка MCP, последние ошибки;
- **Открыть журнал** / **Открыть папку данных**;
- **Импортировать сейчас**, **Проверить MCP**, **Перезапустить туннель**, **Перезапустить всё**;
- **Выход** — импорт дописывает текущий файл, затем останавливается туннель.

## Из командной строки

```
python tracker.py --status          состояние
python tracker.py --open            открыть панель
python tracker.py --import-now      проверить входящую папку сейчас
python tracker.py --restart tunnel  перезапустить туннель (all | tunnel | import | dashboard)
python tracker.py --check-mcp       проверить server.py (только чтение, работает и без запущенного трекера)
python tracker.py --stop            остановить трекер
python tracker.py --no-tray         запуск без значка (для отладки; остановка Ctrl+C)
```

## Автозапуск вместе с Windows

Включается только вручную, одной командой в PowerShell из папки проекта:

```
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
```

Создаётся задача Планировщика «FITNESS Tracker»: запуск `pythonw tracker.pyw` через 30 секунд после входа,
без окон, без прав администратора, второй экземпляр не запускается. Старый ярлык автозапуска окна наблюдения
(если был) удаляется — наблюдение теперь внутри трекера.

- состояние: `... install_autostart.ps1 -Status`
- выключить: `... install_autostart.ps1 -Remove` (или Планировщик заданий → «FITNESS Tracker» → Отключить)
- посмотреть без изменений: `... install_autostart.ps1 -DryRun`

## Подключение ChatGPT (MCP)

Цепочка: приложение (коннектор) в ChatGPT → tunnel-client (профиль `fitness-local`) → `python server.py`.
Настройка профиля — в README.md, пример — `examples/tunnel-client/fitness-local.example.yaml`. Трекер лишь
держит tunnel-client запущенным. После
обновления кода трекер перезапускает туннель сам при запуске; новые инструменты (например, `get_period_stats`)
ChatGPT покажет после обновления приложения/коннектора в его настройках.

## Где что лежит

| Путь | Что |
|---|---|
| `data\fitness.db` | база (резервные копии — `data\backups`) |
| `data\launcher.json` | настройки трекера (путь к tunnel-client, интервал импорта, порт панели, язык `language`: `en` или `ru`); секретов нет |
| `data\logs\tracker.log` | журнал трекера и импорта (1 МБ × 5 файлов) |
| `data\logs\tunnel.log` | вывод tunnel-client и server.py (2 МБ × 3) |
| `data\health_syncthing` | папка Syncthing (только чтение, трекер её не меняет) |
| `data\health_inbox` → `health_processed` / `health_failed` | очередь импорта и её результат |

Ключ туннеля берётся из переменной окружения пользователя `CONTROL_PLANE_API_KEY`; трекер его
никуда не записывает.

## Если что-то не так

- значок красный, «туннель: ошибка» — «Состояние…» покажет причину, подробности в `data\logs\tunnel.log`;
- «tunnel-client.exe не найден» — укажите путь в `data\launcher.json`, поле `tunnel_client`;
- файл Health Connect в `data\health_failed` — причина в `<имя>.error.txt`; повтор: `health_import_inbox.bat --retry-failed`;
- обновление и восстановление: [docs/UPDATE_AND_RECOVERY.md](docs/UPDATE_AND_RECOVERY.md).
