# Архитектура FITNESS Tracker

## Компоненты

```
server.py                  MCP-сервер (stdio) — его запускает tunnel-client
dashboard.py               локальная веб-панель (127.0.0.1:8765)
import_health.py           ручной импорт выгрузок Health Connect
health_inbox.py            автоимпорт из входящей папки (разово или --watch)
migrate_db.py              явное обновление схемы базы (с резервной копией)
tracker.py / tracker.pyw   единый запуск для Windows: значок в трее, туннель, автоимпорт, панель
fitness/
  protocol.py              JSON-RPC/MCP поверх stdio (stdout — только протокол, диагностика — в stderr)
  tools.py                 описания MCP-инструментов и их обработчики
  db.py                    подключение, миграции, резервные копии
  migrations/*.sql         схема базы по версиям
  measurements, workouts, nutrition, alcohol, notes, records, period_stats
                           операции с данными
  validation.py, errors.py проверки входных данных и понятные ошибки
  health_*.py              формат выгрузки, импорт, входящая папка, передача, статус синхронизации
  photo_store.py, photo_trash.py   фото и корзина фото
  web/                     страницы панели
  i18n.py, locales/        язык интерфейса: функция t() и словари en.py, ru.py с одинаковыми ключами
  launcher/                единый запуск (только Windows)
tests/                     unittest, только временные базы
data/                      создаётся автоматически, в репозиторий не попадает
```

## Основные решения

- **Только стандартная библиотека Python** (`sqlite3`, `json`, `http.server`, `unittest`). MCP SDK не используется:
  свой небольшой протокол совместим с tunnel-client и не требует установки пакетов.
- **Поддерживаемые методы MCP:** `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
  Неизвестный метод получает ошибку −32601. На любой запрос всегда приходит ответ, в том числе при сбое.
- **Кодировка:** stdin/stdout принудительно в UTF-8 (в Windows иначе ломается кириллица).
- **Даты:** `YYYY-MM-DD`, время отдельным полем `HH:MM` (местное время). Дата обязательна; сервер никогда
  не подставляет «сегодня» сам.
- **Единицы:** вес в кг, замеры в см, объём в мл. Другие единицы — ошибка с понятным текстом, а не тихая конвертация.
- **Защита от дублей:** запись может нести `client_request_id`; повторный вызов возвращает уже созданную запись.
- **Удаление в два шага:** `prepare_delete_record` выдаёт код подтверждения, `delete_record` удаляет с этим кодом.
  Перед удалением делается резервная копия.
- **Резервные копии:** `sqlite3.backup()` в `data/backups/`; старые копии не перезаписываются.
- **Миграции схемы** не применяются молча к существующей базе: только командой `migrate_db.py`, которая делает
  резервную копию. Новая база создаётся при первой записи.
- **Одновременный доступ:** режим WAL и таймаут блокировки; панель открывает базу только на чтение (кроме раздела «Фото»).
- **Сеть:** панель слушает только 127.0.0.1, проверяет Host и Origin. Наружу сервер ничего не отправляет.
- **Язык интерфейса:** английский по умолчанию и русский. Тексты панели и значка в трее — в словарях
  `fitness/locales`, код вызывает `t("ключ")`. Язык хранится в `data/launcher.json` (ключ `language`) и читается
  на каждом запросе; переключатель `EN | RU` — форма POST `/settings/language` с теми же проверками, что у форм «Фото».
  Названия инструментов MCP, поля базы, коды и тексты ошибок ядра не переводятся.

## Схема базы (кратко)

Служебная таблица `schema_migrations`. У основных таблиц есть `created_at`, `updated_at`, `client_request_id`.

- **Замеры:** `measurement_sessions` (дата, время, вес, заметки) → `body_measurements` (тип из справочника
  `measurement_types`, значение в см); `photos` привязаны к дате или сеансу замеров.
- **Тренировки:** `workouts` → `workout_exercises` (справочник `exercises`, порядок, заметки, дискомфорт) →
  `exercise_sets` (вес, повторения, RIR, время, дистанция, разминка). Сохраняются одной транзакцией.
- **Питание:** `meals` → `meal_items` (количество, единица, ккал, БЖУ, источник оценки, признак приблизительности).
  Неизвестное хранится как NULL, а не 0.
- **Алкоголь:** `alcohol_entries` (объём, крепость, этанол считается только если известны объём и крепость).
- **Заметки:** `notes` → `note_fields` (дополнительные структурированные поля).
- **Health Connect:** шаги, пульс, сон, источники данных и журнал импортов — см. `docs/HEALTH_SCHEMA.md`.

## MCP-инструменты

Полные описания аргументов — в `fitness/tools.py` (их же видит ChatGPT через `tools/list`).

| Группа | Инструменты |
|---|---|
| Состояние | `get_tracker_status`, `backup_database` |
| Замеры и фото | `log_measurement`, `get_measurements`, `add_photo` |
| Тренировки | `log_workout`, `get_workouts`, `list_exercises` |
| Питание и алкоголь | `log_meal`, `get_nutrition_summary`, `log_alcohol`, `get_alcohol_summary` |
| Заметки и поиск | `add_note`, `search_records`, `get_record`, `get_period_stats` |
| Удаление | `prepare_delete_record`, `delete_record` |
| Health Connect | `get_daily_activity`, `get_health_summary`, `get_health_sync_status`, `get_health_imports`, `import_health_export`, `import_health_inbox`, `start_health_upload`, `upload_health_chunk` |

Ответ инструмента — JSON-текст с результатом. При ошибке — `isError: true` и объяснение, что уточнить у пользователя.
