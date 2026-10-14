"""MCP через stdio: JSON-RPC 2.0, одно сообщение на строку.

stdout — только сообщения протокола (чистый ASCII, русский текст экранируется как \\uXXXX).
Диагностика — только в stderr. На любой запрос с id обязательно уходит ответ, даже при ошибке.
"""

import json
import re
import sys
import traceback

from . import __version__
from .tools import call_tool, list_tools

SERVER_NAME = "fitness-tracker"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INSTRUCTIONS = (
    "Локальный фитнес-трекер пользователя (данные хранятся на его компьютере). "
    "Даты передавай в формате YYYY-MM-DD, время — HH:MM. Не подставляй сегодняшнюю дату, "
    "если пользователь не сказал, что запись относится к сегодняшнему дню; при сомнениях уточни. "
    "Не выдумывай отсутствующие значения: если калории, объём или вес неизвестны, просто не передавай поле. "
    "Для каждой новой записи передавай уникальный client_request_id и при повторе того же вызова "
    "используй тот же id, чтобы не создать дубликат. "
    "Если инструмент вернул ошибку validation_error, задай пользователю уточняющий вопрос. "
    "Удаление: сначала prepare_delete_record, покажи запись пользователю, "
    "и только после его явного согласия вызывай delete_record с полученным кодом. "
    "Файл выгрузки Health Connect (fitness-health-export), прикреплённый к сообщению, передавай в "
    "import_health_export параметром file; не утверждай, что данные импортированы, пока инструмент не вернул "
    "status imported или already_imported. "
    "Шаги, сон и пульс по дням (для расчёта активности и калорий) получай через get_daily_activity; "
    "null в нём означает отсутствие данных, а не ноль. Итоги и средние за период — get_health_summary."
)


def log(message):
    print(f"[fitness-tracker] {message}", file=sys.stderr, flush=True)


def _response(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    return message


def _error(request_id, code, text):
    return _response(request_id, error={"code": code, "message": text})


def _negotiate_version(requested):
    # Как и прежний server.py, принимаем версию клиента: набор возможностей сервера (только tools)
    # одинаков во всех версиях протокола.
    if isinstance(requested, str) and _VERSION_RE.match(requested):
        return requested
    return DEFAULT_PROTOCOL_VERSION


def handle_message(msg):
    """Обработать одно сообщение. Возвращает ответ (dict) или None для уведомлений."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request")

    method = msg.get("method")
    request_id = msg.get("id")
    is_notification = "id" not in msg

    if method is None:
        # Ответ клиента на наш запрос или мусор — мы ничего не запрашиваем, поэтому игнорируем.
        return None if is_notification or "result" in msg or "error" in msg else _error(
            request_id, -32600, "Invalid Request")

    if is_notification:
        # notifications/initialized, notifications/cancelled и т. п. ответа не требуют.
        return None

    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params must be an object")

    try:
        if method == "initialize":
            return _response(request_id, {
                "protocolVersion": _negotiate_version(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            })

        if method == "ping":
            return _response(request_id, {})

        if method == "tools/list":
            return _response(request_id, {"tools": list_tools()})

        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return _error(request_id, -32602, "tools/call requires 'name'")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            return _response(request_id, call_tool(name, arguments))

        return _error(request_id, -32601, "Method not found")
    except Exception as exc:  # последняя линия защиты: клиент не должен ждать вечно
        log(f"internal error in {method}: {exc!r}\n{traceback.format_exc()}")
        return _error(request_id, -32603, "Internal error")


def handle_line(line):
    """Строка stdin -> ответ (dict/list) или None."""
    line = line.strip()
    if not line:
        return None
    try:
        message = json.loads(line)
    except ValueError as exc:
        log(f"parse error: {exc}")
        return _error(None, -32700, "Parse error")
    if isinstance(message, list):
        if not message:
            return _error(None, -32600, "Invalid Request")
        responses = [r for r in (handle_message(m) for m in message) if r is not None]
        return responses or None
    return handle_message(message)


def send(message, stream=None):
    stream = stream or sys.stdout
    stream.write(json.dumps(message, ensure_ascii=True) + "\n")
    stream.flush()


def configure_stdio():
    """Windows по умолчанию читает stdin в cp1251 — переводим потоки в UTF-8."""
    for stream, kwargs in ((sys.stdin, {"errors": "replace"}),
                           (sys.stdout, {"errors": "strict", "newline": "\n"}),
                           (sys.stderr, {"errors": "backslashreplace"})):
        try:
            stream.reconfigure(encoding="utf-8", **kwargs)
        except (AttributeError, ValueError):
            pass


def main():
    configure_stdio()
    log(f"started, version {__version__}")
    for line in sys.stdin:
        response = None
        try:
            response = handle_line(line)
            if response is not None:
                send(response)
        except Exception as exc:
            log(f"error while handling a message: {exc!r}\n{traceback.format_exc()}")
            # Например, ответ не удалось сериализовать: всё равно сообщаем клиенту об ошибке.
            if isinstance(response, dict) and response.get("id") is not None:
                try:
                    send(_error(response["id"], -32603, "Internal error"))
                except Exception:
                    pass
    log("stdin closed, exiting")
