"""MCP: протокол, список инструментов, вызовы, ошибки.

Все вызовы идут во временную базу (переменная FITNESS_DB_PATH).
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fitness import db, protocol, tools

PROJECT_DIR = Path(__file__).resolve().parent.parent
SERVER = PROJECT_DIR / "server.py"

REQUIRED_TOOLS = {
    "get_tracker_status", "log_measurement", "get_measurements", "log_workout", "get_workouts", "log_meal",
    "get_nutrition_summary", "log_alcohol", "get_alcohol_summary", "add_note", "search_records",
    "prepare_delete_record", "delete_record",
}

class McpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-mcp-test-"))
        self.db_path = self.tmp / "data" / "test.db"
        patcher = mock.patch.dict(os.environ, {"FITNESS_DB_PATH": str(self.db_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch("sys.stderr", io.StringIO())   # диагностика сервера не засоряет вывод тестов
        quiet.start()
        self.addCleanup(quiet.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        response = protocol.handle_line(json.dumps(msg, ensure_ascii=False))
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], self.next_id)
        json.dumps(response)   # ответ обязан сериализоваться
        return response

    def call(self, name, arguments=None):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result = response["result"]
        self.assertIsInstance(result["content"], list)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIsInstance(result["isError"], bool)
        return result

    def call_ok(self, name, arguments=None):
        result = self.call(name, arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload, result["structuredContent"])
        self.assertTrue(payload["ok"])
        return payload

    def call_error(self, name, arguments=None):
        result = self.call(name, arguments)
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(payload["ok"])
        return payload


class ProtocolTests(McpTestCase):
    def test_initialize(self):
        res = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "fitness-tracker")
        self.assertIn("YYYY-MM-DD", res["instructions"])

    def test_initialize_without_version(self):
        self.assertEqual(self.rpc("initialize", {})["result"]["protocolVersion"], "2024-11-05")

    def test_ping(self):
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_notifications_get_no_response(self):
        for method in ("notifications/initialized", "notifications/cancelled", "notifications/unknown"):
            self.assertIsNone(protocol.handle_line(json.dumps({"jsonrpc": "2.0", "method": method})))

    def test_unknown_method(self):
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)

    def test_parse_error(self):
        res = protocol.handle_line("{not json")
        self.assertEqual(res["error"]["code"], -32700)
        self.assertIsNone(res["id"])

    def test_invalid_requests(self):
        self.assertEqual(protocol.handle_line("[]")["error"]["code"], -32600)
        self.assertEqual(protocol.handle_line("42")["error"]["code"], -32600)
        res = protocol.handle_line(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": []}))
        self.assertEqual(res["error"]["code"], -32602)
        res = protocol.handle_line(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {}}))
        self.assertEqual(res["error"]["code"], -32602)
        self.assertEqual(res["id"], 6)

    def test_empty_line_ignored(self):
        self.assertIsNone(protocol.handle_line("   \n"))

    def test_batch(self):
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        res = protocol.handle_line(json.dumps(batch))
        self.assertEqual([r["id"] for r in res], [1, 2])

    def test_internal_error_still_answers(self):
        with mock.patch.object(protocol, "list_tools", side_effect=RuntimeError("boom")):
            res = self.rpc("tools/list")
        self.assertEqual(res["error"]["code"], -32603)


class ToolListTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.tools = self.rpc("tools/list")["result"]["tools"]
        self.by_name = {t["name"]: t for t in self.tools}

    def test_required_tools_present(self):
        self.assertTrue(REQUIRED_TOOLS <= set(self.by_name), REQUIRED_TOOLS - set(self.by_name))

    def test_schemas_are_strict(self):
        def walk(schema, path):
            if schema.get("type") == "object":
                self.assertIs(schema.get("additionalProperties"), False, path)
                for key, sub in schema.get("properties", {}).items():
                    walk(sub, f"{path}.{key}")
            if schema.get("type") == "array":
                walk(schema["items"], f"{path}[]")
        for tool in self.tools:
            self.assertTrue(tool["description"], tool["name"])
            self.assertNotIn("handler", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")
            walk(tool["inputSchema"], tool["name"])

    def test_measurement_enum_matches_database(self):
        conn = db.open_database(self.db_path)
        try:
            codes = {r[0] for r in conn.execute("SELECT code FROM measurement_types")}
        finally:
            conn.close()
        self.assertEqual(set(tools.MEASUREMENT_TYPES), codes)

    def test_read_tools_marked_read_only(self):
        for name in ("get_tracker_status", "get_measurements", "get_workouts", "search_records"):
            self.assertTrue(self.by_name[name]["annotations"]["readOnlyHint"], name)
        self.assertTrue(self.by_name["delete_record"]["annotations"]["destructiveHint"])


class ToolCallTests(McpTestCase):
    def test_status_without_database_does_not_create_it(self):
        payload = self.call_ok("get_tracker_status")
        self.assertFalse(payload["database_exists"])
        self.assertFalse(self.db_path.exists())

    def test_measurement_roundtrip_with_russian_text(self):
        res = self.call_ok("log_measurement", {"measured_on": "2025-03-01", "weight_kg": 80.5,
                                               "measurements": [{"type": "waist", "value": 85}],
                                               "notes": "Утром, после сна — ё"})
        self.assertEqual(res["record"]["notes"], "Утром, после сна — ё")
        got = self.call_ok("get_measurements", {"date_from": "2025-03-01", "date_to": "2025-03-31"})
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["records"][0]["notes"], "Утром, после сна — ё")
        status = self.call_ok("get_tracker_status")
        self.assertTrue(status["database_ok"])
        self.assertEqual(status["record_counts"]["measurement"], 1)

    def test_workout_roundtrip(self):
        self.call_ok("log_workout", {"workout_date": "2025-04-02", "title": "Ноги", "workout_type": "strength",
                                     "exercises": [{"name": "Присед", "discomfort": "колено",
                                                    "sets": [{"weight_kg": 80, "reps": 8, "rir": 2}]}]})
        got = self.call_ok("get_workouts", {"date_from": "2025-04-01", "date_to": "2025-04-30",
                                            "exercise": "присед"})
        self.assertEqual(got["records"][0]["exercises"][0]["sets"][0]["reps"], 8)
        self.assertEqual(self.call_ok("list_exercises")["exercises"][0]["name"], "Присед")

    def test_meal_roundtrip(self):
        self.call_ok("log_meal", {"eaten_on": "2025-06-01", "meal_type": "breakfast", "items": [
            {"name": "Омлет", "quantity": 1, "unit": "serving", "kcal": 300, "nutrition_source": "estimate"}]})
        got = self.call_ok("get_nutrition_summary", {"date_from": "2025-06-01", "date_to": "2025-06-01"})
        self.assertEqual(got["days"][0]["kcal"], 300)
        self.assertEqual(got["days"][0]["items_estimated"], 1)

    def test_alcohol_roundtrip(self):
        self.call_ok("log_alcohol", {"consumed_on": "2025-07-01", "drink": "Пиво", "volume_ml": 500,
                                     "abv_percent": 5})
        got = self.call_ok("get_alcohol_summary", {"date_from": "2025-07-01", "date_to": "2025-07-31"})
        self.assertAlmostEqual(got["ethanol_g_total"], 19.7)

    def test_note_and_search(self):
        self.call_ok("add_note", {"note_date": "2025-08-01", "category": "сон", "text": "Проснулся бодрым",
                                  "fields": [{"key": "часы", "value": 8, "unit": "ч"}]})
        got = self.call_ok("search_records", {"query": "БОДРЫМ"})
        self.assertEqual(got["results"][0]["record_type"], "note")
        rec = self.call_ok("get_record", {"record_type": "note", "id": got["results"][0]["id"]})
        self.assertEqual(rec["record"]["fields"][0]["value"], 8)

    def test_duplicate_request_id(self):
        args = {"note_date": "2025-08-01", "category": "тест", "text": "один раз", "client_request_id": "req-1"}
        first = self.call_ok("add_note", args)
        second = self.call_ok("add_note", args)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.call_ok("search_records", {"query": "один раз"})["count"], 1)

    def test_validation_error_is_reported(self):
        err = self.call_error("log_measurement", {"weight_kg": 80})
        self.assertEqual(err["error"], "validation_error")
        self.assertEqual(err["field"], "measured_on")
        self.assertIn("hint", err)
        err = self.call_error("get_workouts", {"date_from": "2025-01-01", "date_to": "2025-01-31", "x": 1})
        self.assertIn("неизвестные поля", err["message"])
        err = self.call_error("log_meal", {"eaten_on": "вчера", "items": [{"name": "Суп"}]})
        self.assertEqual(err["field"], "eaten_on")

    def test_non_object_arguments(self):
        res = self.rpc("tools/call", {"name": "add_note", "arguments": ["a"]})["result"]
        self.assertTrue(res["isError"])

    def test_unknown_tool(self):
        res = self.call("drop_everything")
        self.assertTrue(res["isError"])
        self.assertEqual(res["content"][0]["text"], "Unknown tool")

    def test_unexpected_exception_does_not_crash(self):
        with mock.patch.dict(tools._BY_NAME["list_exercises"], {"handler": mock.Mock(side_effect=KeyError("x"))}):
            err = self.call_error("list_exercises")
        self.assertEqual(err["error"], "internal_error")
        self.assertEqual(self.rpc("ping")["result"], {})   # сервер жив

    def test_sqlite_error_reported(self):
        self.db_path.parent.mkdir(parents=True)
        self.db_path.write_bytes(b"not a database at all" * 200)
        err = self.call_error("add_note", {"note_date": "2025-08-01", "category": "x", "text": "y"})
        self.assertEqual(err["error"], "database_error")
        status = self.call_ok("get_tracker_status")
        self.assertFalse(status["database_ok"])

    def test_two_step_delete_with_backup(self):
        rec = self.call_ok("log_alcohol", {"consumed_on": "2025-07-01", "drink": "Вино"})
        err = self.call_error("delete_record", {"record_type": "alcohol", "id": rec["id"],
                                                "confirmation_code": "GUESS"})
        self.assertEqual(err["error"], "confirmation_required")
        preview = self.call_ok("prepare_delete_record", {"record_type": "alcohol", "id": rec["id"]})
        done = self.call_ok("delete_record", {"record_type": "alcohol", "id": rec["id"],
                                              "confirmation_code": preview["confirmation_code"]})
        self.assertTrue(done["deleted"])
        backup = Path(done["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(backup.parent, self.db_path.parent / "backups")
        self.assertEqual(self.call_ok("get_alcohol_summary", {"date_from": "2025-07-01",
                                                              "date_to": "2025-07-31"})["entries_count"], 0)

    def test_delete_backup_failure_is_clear_tool_error(self):
        rec = self.call_ok("log_alcohol", {"consumed_on": "2025-07-01", "drink": "Вино"})
        preview = self.call_ok("prepare_delete_record", {"record_type": "alcohol", "id": rec["id"]})
        with mock.patch("fitness.records.backup", side_effect=OSError("диск полон")):
            err = self.call_error("delete_record", {"record_type": "alcohol", "id": rec["id"],
                                                    "confirmation_code": preview["confirmation_code"]})
        self.assertEqual(err["error"], "backup_failed")
        self.assertIn("Удаление не выполнено", err["message"])
        self.assertIn("НЕ удалена", err["hint"])
        self.assertIsNone(err["backup"])
        # сервер жив, запись на месте
        self.call_ok("get_record", {"record_type": "alcohol", "id": rec["id"]})

    def test_delete_refused_when_backup_check_fails(self):
        rec = self.call_ok("log_alcohol", {"consumed_on": "2025-07-01", "drink": "Вино"})
        preview = self.call_ok("prepare_delete_record", {"record_type": "alcohol", "id": rec["id"]})
        real_backup = db.backup

        def damaged(conn, backup_dir, label=None):
            path = real_backup(conn, backup_dir, label=label)
            path.write_bytes(path.read_bytes()[:4096])
            return path
        with mock.patch("fitness.records.backup", side_effect=damaged):
            err = self.call_error("delete_record", {"record_type": "alcohol", "id": rec["id"],
                                                    "confirmation_code": preview["confirmation_code"]})
        self.assertEqual(err["error"], "backup_failed")
        self.assertEqual(Path(err["backup"]).parent, self.db_path.parent / "backups")
        self.call_ok("get_record", {"record_type": "alcohol", "id": rec["id"]})

    def test_backup_tool(self):
        err = self.call_error("backup_database")
        self.assertIn("Базы ещё нет", err["message"])
        self.call_ok("add_note", {"note_date": "2025-08-01", "category": "x", "text": "y"})
        first = self.call_ok("backup_database")["backup"]
        second = self.call_ok("backup_database")["backup"]
        self.assertNotEqual(first, second)


class StdioServerTests(unittest.TestCase):
    """Запуск настоящего server.py как отдельного процесса — так же, как это делает tunnel-client."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-stdio-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_server(self, lines):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["FITNESS_DB_PATH"] = str(self.tmp / "data" / "test.db")
        payload = "".join(line + "\n" for line in lines).encode("utf-8")
        proc = subprocess.run([sys.executable, str(SERVER)], input=payload, capture_output=True,
                              cwd=str(self.tmp), env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        out_lines = proc.stdout.decode("ascii").splitlines()   # stdout — только ASCII
        return [json.loads(line) for line in out_lines], proc.stderr.decode("utf-8", "replace")

    def test_full_session_with_russian_text_and_errors(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add_note", "arguments": {
                "note_date": "2025-08-01", "category": "самочувствие", "text": "Жим лёжа — плечо ноет"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_records",
                                                                           "arguments": {"query": "ПЛЕЧО"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "log_meal", "arguments": {
                "eaten_on": "2025-08-01"}}},
        ]
        lines = [json.dumps(m, ensure_ascii=False) for m in msgs]
        lines.insert(3, "{это не json")
        lines.append(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "ping"}))
        lines.append(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                 "params": {"name": "test_write", "arguments": {"confirm": "WRITE_TEST"}}}))
        responses, stderr = self.run_server(lines)

        by_id = {r["id"]: r for r in responses}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("test_write", [t["name"] for t in by_id[2]["result"]["tools"]])
        self.assertIsNone(by_id[None]["id"])                      # ответ на битую строку
        self.assertEqual(by_id[None]["error"]["code"], -32700)
        note = json.loads(by_id[3]["result"]["content"][0]["text"])
        self.assertEqual(note["record"]["text"], "Жим лёжа — плечо ноет")
        found = json.loads(by_id[4]["result"]["content"][0]["text"])
        self.assertEqual(found["count"], 1)
        self.assertTrue(by_id[5]["result"]["isError"])
        self.assertEqual(by_id[6]["result"], {})
        self.assertTrue(by_id[7]["result"]["isError"])           # test_write удалён
        self.assertEqual(by_id[7]["result"]["content"][0]["text"], "Unknown tool")
        self.assertEqual(len(responses), 8)                       # 7 запросов + битая строка; на уведомление ответа нет
        self.assertIn("started", stderr)


if __name__ == "__main__":
    unittest.main()
