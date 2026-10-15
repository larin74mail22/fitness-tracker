"""MCP-импорт выгрузки Health Connect: инструмент import_health_export, передача частями, журнал.

Все базы временные (FITNESS_DB_PATH), все файлы синтетические. Скачивание по ссылке подменяется: сеть не нужна.
"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fitness import db, health_import, health_transfer, tools
from tests import health_samples as hs

PROJECT_DIR = Path(__file__).resolve().parent.parent
SERVER = PROJECT_DIR / "server.py"
URL = "https://files.example.com/file-abc?sig=1"
REAL_PUBLIC_HOST = health_transfer._public_host


def file_arg(name="export.json"):
    return {"download_url": URL, "file_id": "file-abc", "mime_type": "application/json", "file_name": name}


def night(seq=1, modified=hs.DEFAULT_MODIFIED, stages=None, end="2026-10-10T04:00:00Z", **kw):
    """Сон через полночь по местному времени (UTC+3): 22:30 9 октября → 07:00 10 октября."""
    stages = stages if stages is not None else [
        {"stage": "light", "start": "2026-10-09T19:30:00Z", "end": "2026-10-09T21:00:00Z"},   # 22:30–00:00 местн.
        {"stage": "deep", "start": "2026-10-09T21:00:00Z", "end": "2026-10-09T22:30:00Z"},    # через полночь
        {"stage": "rem", "start": "2026-10-09T22:30:00Z", "end": "2026-10-10T04:00:00Z"},
    ]
    return hs.sleep("night-1", "2026-10-09T19:30:00Z", end, stages=stages, modified=modified, **kw)


class McpImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-mcp-import-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "data" / "test.db"
        self.assertNotEqual(self.db_path.resolve(), db.DEFAULT_DB_PATH.resolve())
        patcher = mock.patch.dict(os.environ, {"FITNESS_DB_PATH": str(self.db_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch("sys.stderr", io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)
        db.open_database(self.db_path).close()
        self.served = {}
        for target, value in ((health_transfer, "_public_host"), (health_transfer, "_download")):
            p = mock.patch.object(target, value, (lambda host: None) if value == "_public_host" else self._serve)
            p.start()
            self.addCleanup(p.stop)

    def _serve(self, url):
        return self.served[url]

    def call(self, name, arguments):
        result = tools.call_tool(name, arguments)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload, result["structuredContent"])
        return result["isError"], payload

    def ok(self, name, arguments):
        is_error, payload = self.call(name, arguments)
        self.assertFalse(is_error, payload)
        return payload

    def err(self, name, arguments):
        is_error, payload = self.call(name, arguments)
        self.assertTrue(is_error, payload)
        return payload

    def send(self, doc, **flags):
        self.served[URL] = doc if isinstance(doc, bytes) else hs.encode(doc)
        return self.call("import_health_export", dict(file=file_arg(), **flags))

    def imported(self, doc, **flags):
        is_error, payload = self.send(doc, **flags)
        self.assertFalse(is_error, payload)
        return payload

    def failed(self, doc, **flags):
        is_error, payload = self.send(doc, **flags)
        self.assertTrue(is_error, payload)
        return payload

    def count(self, table, where="1"):
        conn = db.connect(self.db_path, create=False)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            conn.close()

    def data_rows(self):
        return {t: self.count(t) for t in ("step_intervals", "heart_rate_records", "heart_rate_samples",
                                           "sleep_sessions", "sleep_stages")}


class ToolListTests(McpImportTestCase):
    def test_import_tool_declares_file_param(self):
        by_name = {t["name"]: t for t in tools.list_tools()}
        tool = by_name["import_health_export"]
        self.assertEqual(tool["_meta"], {"openai/fileParams": ["file"]})
        file_schema = tool["inputSchema"]["properties"]["file"]
        self.assertEqual(set(file_schema["properties"]), {"download_url", "file_id", "mime_type", "file_name"})
        self.assertEqual(set(file_schema["required"]), {"download_url", "file_id"})
        self.assertNotIn("path", json.dumps(tool["inputSchema"]))       # путей к файлам на диске нет
        for name in ("start_health_upload", "upload_health_chunk", "get_health_imports"):
            self.assertIn(name, by_name)


class ImportScenarioTests(McpImportTestCase):
    def test_01_first_import(self):
        r = self.imported(hs.day_file(1))
        self.assertEqual(r["status"], "imported")
        self.assertIsInstance(r["import_id"], int)
        self.assertEqual((r["phone_id"], r["sequence"]), ("phone-01", 1))
        self.assertEqual(r["counts"]["steps"]["inserted"], 3)
        self.assertEqual(r["counts"]["heart_rate"]["inserted"], 3)
        self.assertEqual(r["counts"]["sleep"]["inserted"], 1)
        self.assertEqual(r["totals"]["inserted"], 7)
        self.assertEqual(r["data_range"]["start_utc"], "2026-10-10T00:00:00.000000000Z")
        self.assertEqual(r["data_range"]["local_date_from"], "2026-10-10")
        self.assertEqual(r["file"], "export.json")
        self.assertEqual(r["errors"], [])
        for key in ("updated", "unchanged", "deleted", "rejected"):
            self.assertEqual(r["totals"][key], 0)

    def test_02_same_file_twice_no_duplicates(self):
        doc = hs.day_file(1)
        self.imported(doc)
        before = self.data_rows()
        r = self.imported(doc)
        self.assertEqual(r["status"], "already_imported")
        self.assertEqual(r["totals"]["inserted"], 0)
        self.assertEqual(self.data_rows(), before)
        self.assertEqual(self.count("import_batches"), 1)

    def test_03_overlapping_period_adds_only_new(self):
        first = hs.day_file(1)
        self.imported(first)
        second = hs.day_file(2)       # те же записи (нахлёст) + новые
        second["steps"].append(hs.step("step-4", "2026-10-10T17:00:00Z", "2026-10-10T17:10:00Z", 100))
        second["heart_rate"].append(hs.heart("hr-4", "2026-10-10T19:20:00Z", "2026-10-10T19:21:00Z", (70,)))
        r = self.imported(second)
        self.assertEqual(r["counts"]["steps"], {"inserted": 1, "updated": 0, "unchanged": 3, "deleted": 0, "rejected": 0})
        self.assertEqual(r["counts"]["heart_rate"]["inserted"], 1)
        self.assertEqual(r["counts"]["heart_rate"]["unchanged"], 3)
        self.assertEqual(r["counts"]["sleep"]["unchanged"], 1)
        self.assertEqual(self.count("step_intervals"), 4)
        self.assertEqual(self.count("heart_rate_records"), 4)
        self.assertEqual(self.count("sleep_sessions"), 1)

    def test_04_changed_records_follow_importer_rules(self):
        self.imported(hs.export(1, [hs.step("s-a", count=100), hs.step("s-b", "2026-10-10T01:00:00Z",
                                                                          "2026-10-10T01:10:00Z", 50)]))
        newer = hs.step("s-a", count=120, modified="2026-10-10T17:30:00Z")
        older = hs.step("s-b", "2026-10-10T01:00:00Z", "2026-10-10T01:10:00Z", 999, modified="2026-10-10T16:00:00Z")
        r = self.imported(hs.export(2, [newer, older]))
        self.assertEqual(r["counts"]["steps"]["updated"], 1)
        self.assertEqual(r["counts"]["steps"]["unchanged"], 1)
        self.assertEqual(r["skipped"]["stale"], 1)
        self.assertEqual(self.count("step_intervals", "hc_id = 's-a' AND count = 120"), 1)
        self.assertEqual(self.count("step_intervals", "hc_id = 's-b' AND count = 50"), 1)

    def test_05_invalid_json(self):
        r = self.failed(b"{not json")
        self.assertEqual(r["error"], "import_file_invalid")
        self.assertIn("не изменены", r["hint"])
        self.assertEqual(self.count("import_batches"), 0)

    def test_06_unknown_format_version(self):
        r = self.failed(hs.day_file(1, format_version=2))
        self.assertEqual(r["error"], "import_file_invalid")
        self.assertTrue(any(p["path"] == "format_version" for p in r["problems"]))
        self.assertEqual(sum(self.data_rows().values()), 0)

    def test_08_repeated_sequence_with_other_content(self):
        self.imported(hs.day_file(1))
        r = self.failed(hs.export(1, [hs.step("other")], exported_at="2026-10-10T17:50:00Z"))
        self.assertEqual(r["error"], "import_order")
        self.assertEqual(self.count("step_intervals", "hc_id = 'other'"), 0)

    def test_09_gap_in_sequence(self):
        self.imported(hs.day_file(1))
        doc = hs.export(3, [hs.step("later", "2026-10-10T18:00:00Z", "2026-10-10T18:10:00Z")])
        r = self.failed(doc)
        self.assertEqual(r["error"], "import_order")
        self.assertIn("allow-gaps", r["message"])
        r = self.imported(doc, allow_gaps=True)
        self.assertEqual(r["status"], "imported")
        self.assertTrue(any("пропущены" in w for w in r["warnings"]))

    def test_10_older_export(self):
        self.imported(hs.day_file(1))
        self.imported(hs.export(3, [hs.step("s3", "2026-10-10T18:00:00Z", "2026-10-10T18:10:00Z")]), allow_gaps=True)
        old = hs.export(2, [hs.step("step-1", count=1, modified="2026-10-10T16:00:00Z")])
        self.assertEqual(self.failed(old)["error"], "import_order")
        r = self.imported(old, allow_older=True)
        self.assertEqual(r["skipped"]["stale"], 1)
        self.assertEqual(self.count("step_intervals", "hc_id = 'step-1' AND count = 420"), 1)   # не откатилось

    def test_11_sleep_across_midnight_is_one_session(self):
        r = self.imported(hs.export(1, sleep_sessions=[night()]))
        self.assertEqual(r["counts"]["sleep"]["inserted"], 1)
        conn = db.connect(self.db_path, create=False)
        try:
            row = conn.execute("SELECT id, start_utc, end_utc, local_date FROM sleep_sessions").fetchone()
            stages = conn.execute("SELECT stage, start_utc, end_utc FROM sleep_stages WHERE session_id = ? "
                                  "ORDER BY start_utc", (row["id"],)).fetchall()
        finally:
            conn.close()
        self.assertEqual(self.count("sleep_sessions"), 1)
        self.assertEqual(row["start_utc"], "2026-10-09T19:30:00.000000000Z")
        self.assertEqual(row["end_utc"], "2026-10-10T04:00:00.000000000Z")
        self.assertEqual(row["local_date"], "2026-10-10")                     # дата пробуждения
        self.assertEqual([s["stage"] for s in stages], ["light", "deep", "rem"])
        self.assertEqual(stages[1]["start_utc"], "2026-10-09T21:00:00.000000000Z")   # стадия через полночь цела
        self.assertEqual(r["data_range"]["local_date_from"], "2026-10-10")

    def test_12_sleep_stages_crossing_overlap_boundary(self):
        # Первый экспорт сделан ночью: сон ещё шёл, стадии до 01:30 местного. Второй экспорт (с нахлёстом) приносит
        # ту же сессию целиком с более поздним временем изменения: это обновление той же сессии, не новая.
        partial = night(stages=[{"stage": "light", "start": "2026-10-09T19:30:00Z", "end": "2026-10-09T21:00:00Z"},
                                {"stage": "deep", "start": "2026-10-09T21:00:00Z", "end": "2026-10-09T22:30:00Z"}],
                        end="2026-10-09T22:30:00Z")
        self.imported(hs.export(1, sleep_sessions=[partial]))
        r = self.imported(hs.export(2, sleep_sessions=[night(modified="2026-10-10T17:30:00Z")]))
        self.assertEqual(r["counts"]["sleep"]["updated"], 1)
        self.assertEqual(self.count("sleep_sessions"), 1)
        self.assertEqual(self.count("sleep_stages"), 3)

    def test_13_resent_sleep_session_unchanged(self):
        self.imported(hs.export(1, sleep_sessions=[night()]))
        r = self.imported(hs.export(2, sleep_sessions=[night()]))
        self.assertEqual(r["counts"]["sleep"]["unchanged"], 1)
        self.assertEqual(self.count("sleep_sessions"), 1)
        self.assertEqual(self.count("sleep_stages"), 3)

    def test_14_failure_during_import_leaves_nothing(self):
        real = health_import._write_record
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError("диск отключился")
            return real(*args, **kwargs)

        with mock.patch.object(health_import, "_write_record", flaky):
            is_error, r = self.send(hs.day_file(1))
        self.assertTrue(is_error)
        self.assertEqual(r["error"], "internal_error")
        self.assertEqual(sum(self.data_rows().values()), 0)
        self.assertEqual(self.count("data_sources"), 0)
        self.assertEqual(self.count("import_batches", "status = 'ok'"), 0)
        self.assertEqual(self.imported(hs.day_file(1))["status"], "imported")    # после сбоя файл принимается

    def test_14b_sqlite_failure_is_journaled(self):
        with mock.patch.object(health_import, "_apply_deletions", side_effect=__import__("sqlite3").OperationalError("x")):
            r = self.failed(hs.day_file(1))
        self.assertEqual(sum(self.data_rows().values()), 0)
        self.assertEqual(self.count("import_batches", "status = 'failed'"), 1)
        self.assertIn("ничего не сохранено", r["message"])

    def test_15_parallel_imports(self):
        payload = hs.encode(hs.day_file(1))
        results, errors = [], []
        barrier = threading.Barrier(4)

        def worker():
            try:
                barrier.wait()
                results.append(health_import.import_bytes(payload, "same.json", self.db_path))
            except Exception as exc:              # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["status"] for r in results), ["already_imported"] * 3 + ["imported"])
        self.assertEqual(self.count("step_intervals"), 3)
        self.assertEqual(self.count("import_batches"), 1)

    def test_15b_parallel_different_files(self):
        first, second = hs.day_file(1), hs.day_file(1, phone="phone-02")
        results, errors = [], []

        def worker(doc):
            try:
                results.append(health_import.import_bytes(hs.encode(doc), "f.json", self.db_path))
            except Exception as exc:              # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(d,)) for d in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(self.count("import_batches", "status = 'ok'"), 2)
        self.assertEqual(self.count("step_intervals"), 3)          # те же hc_id и источник: дубликатов нет

    def test_dry_run_writes_nothing(self):
        r = self.imported(hs.day_file(1), dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertIsNone(r["import_id"])
        self.assertEqual(r["totals"]["inserted"], 7)
        self.assertEqual(self.count("import_batches"), 0)

    def test_missing_database_not_created(self):
        os.remove(self.db_path)
        r = self.failed(hs.day_file(1))
        self.assertEqual(r["error"], "database_missing")
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.err("start_health_upload", {"total_bytes": 1, "sha256": "0" * 64,
                                                          "chunk_count": 1})["error"], "database_missing")

    def test_exactly_one_source_required(self):
        self.assertEqual(self.err("import_health_export", {})["error"], "validation_error")
        self.assertEqual(self.err("import_health_export", {"file": file_arg(), "upload_id": "a" * 32})["error"],
                         "validation_error")
        self.assertEqual(self.err("import_health_export", {"path": "C:/Windows/win.ini"})["error"], "validation_error")

    def test_journal_tool(self):
        self.imported(hs.day_file(1))
        self.failed(hs.export(3, [hs.step("x")]))
        r = self.ok("get_health_imports", {})
        self.assertEqual([b["status"] for b in r["imports"]], ["failed", "ok"])
        self.assertEqual(r["imports"][1]["counts"]["steps"]["inserted"], 3)
        self.assertEqual(r["phones"], [{"phone_id": "phone-01", "last_sequence": 1,
                                        "last_exported_at": "2026-10-10T17:20:00Z"}])


class DownloadUrlTests(McpImportTestCase):
    def setUp(self):
        super().setUp()
        # Настоящая проверка адресов (без сети: результат DNS подменяется в каждом тесте).
        patcher = mock.patch.object(health_transfer, "_public_host", REAL_PUBLIC_HOST)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve_to(self, address):
        return mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))])

    def test_rejects_unsafe_urls(self):
        bad = ["http://files.example.com/x", "ftp://files.example.com/x", "https://user:pw@files.example.com/x",
               "https://files.example.com:8443/x", "https://127.0.0.1/x", "https://[::1]/x", "file:///C:/x.json", ""]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(health_transfer.ValidationError):
                health_transfer.check_download_url(url)

    def test_rejects_private_resolution(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.1.1", "::1"):
            with self.subTest(address=address), self.resolve_to(address), \
                    self.assertRaises(health_transfer.TransferError):
                health_transfer.check_download_url("https://localhost.example.com/x")

    def test_accepts_public_https(self):
        with self.resolve_to("93.184.216.34"):
            self.assertEqual(health_transfer.check_download_url(URL), URL)

    def test_download_failure_reports_and_writes_nothing(self):
        with self.resolve_to("93.184.216.34"), \
                mock.patch.object(health_transfer, "_download", side_effect=health_transfer.TransferError("ссылка устарела")):
            is_error, r = self.call("import_health_export", {"file": file_arg()})
        self.assertTrue(is_error)
        self.assertEqual(r["error"], "transfer_failed")
        self.assertEqual(self.count("import_batches"), 0)


class ChunkedUploadTests(McpImportTestCase):
    def start(self, data, chunks, encoding="utf8"):
        return self.ok("start_health_upload", {"total_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                               "chunk_count": chunks, "encoding": encoding,
                                               "file_name": "phone.json"})["upload_id"]

    def pieces(self, data, n):
        size = -(-len(data) // n)
        return [data[i * size:(i + 1) * size] for i in range(n)]

    def test_full_transfer_out_of_order_then_import(self):
        data = hs.encode(hs.day_file(1))
        uid = self.start(data, 3)
        parts = self.pieces(data.decode("utf-8"), 3)
        for i in (2, 0):
            self.assertFalse(self.ok("upload_health_chunk", {"upload_id": uid, "index": i, "data": parts[i]})["complete"])
        r = self.ok("upload_health_chunk", {"upload_id": uid, "index": 1, "data": parts[1],
                                            "chunk_sha256": hashlib.sha256(parts[1].encode()).hexdigest()})
        self.assertTrue(r["complete"])
        self.assertTrue(self.ok("upload_health_chunk", {"upload_id": uid, "index": 1, "data": parts[1]})["duplicate"])
        r = self.ok("import_health_export", {"upload_id": uid})
        self.assertEqual(r["status"], "imported")
        self.assertEqual(r["file"], "phone.json")
        self.assertFalse((self.db_path.parent / "health_uploads" / uid).exists())    # части удалены после импорта

    def test_base64_transfer(self):
        data = hs.encode(hs.day_file(1))
        uid = self.start(data, 2, "base64")
        for i, piece in enumerate(self.pieces(data, 2)):
            self.ok("upload_health_chunk", {"upload_id": uid, "index": i, "data": base64.b64encode(piece).decode()})
        self.assertEqual(self.ok("import_health_export", {"upload_id": uid, "dry_run": True})["status"], "dry_run")
        self.assertTrue((self.db_path.parent / "health_uploads" / uid).exists())     # dry_run части не удаляет
        self.assertEqual(self.ok("import_health_export", {"upload_id": uid})["status"], "imported")

    def test_07_incomplete_transfer_does_not_import(self):
        data = hs.encode(hs.day_file(1))
        uid = self.start(data, 3)
        parts = self.pieces(data.decode("utf-8"), 3)
        self.ok("upload_health_chunk", {"upload_id": uid, "index": 0, "data": parts[0]})
        r = self.err("import_health_export", {"upload_id": uid})
        self.assertEqual(r["error"], "transfer_failed")
        self.assertIn("нет частей 1, 2", r["message"])
        self.assertEqual(self.count("import_batches"), 0)

    def test_07b_corrupted_transfer_does_not_import(self):
        data = hs.encode(hs.day_file(1))
        uid = self.start(data, 2)
        a, b = self.pieces(data.decode("utf-8"), 2)
        self.ok("upload_health_chunk", {"upload_id": uid, "index": 0, "data": b[:len(a)]})   # перепутаны местами
        self.ok("upload_health_chunk", {"upload_id": uid, "index": 1, "data": a[:len(b)]})
        r = self.err("import_health_export", {"upload_id": uid})
        self.assertEqual(r["error"], "transfer_failed")
        self.assertIn("SHA-256", r["message"])
        self.assertEqual(self.count("import_batches"), 0)

    def test_07c_bad_chunks_rejected(self):
        data = hs.encode(hs.day_file(1))
        uid = self.start(data, 2)
        a, _ = self.pieces(data.decode("utf-8"), 2)
        self.ok("upload_health_chunk", {"upload_id": uid, "index": 0, "data": a})
        self.assertEqual(self.err("upload_health_chunk", {"upload_id": uid, "index": 0, "data": a + "x"})["error"],
                         "transfer_failed")                                      # та же часть, другое содержимое
        self.assertEqual(self.err("upload_health_chunk", {"upload_id": uid, "index": 2, "data": a})["error"],
                         "validation_error")
        self.assertEqual(self.err("upload_health_chunk", {"upload_id": uid, "index": 1, "data": "x",
                                                          "chunk_sha256": "0" * 64})["error"], "transfer_failed")
        self.assertEqual(self.err("upload_health_chunk", {"upload_id": "../../etc", "index": 0, "data": a})["error"],
                         "validation_error")
        self.assertEqual(self.err("upload_health_chunk", {"upload_id": "f" * 32, "index": 0, "data": a})["error"],
                         "transfer_failed")
        b64 = self.start(data, 1, "base64")
        self.assertEqual(self.err("upload_health_chunk", {"upload_id": b64, "index": 0, "data": "@@@"})["error"],
                         "validation_error")

    def test_expired_uploads_are_removed(self):
        data = hs.encode(hs.day_file(1))
        uid = self.start(data, 1)
        base = self.db_path.parent / "health_uploads"
        self.assertEqual(health_transfer.cleanup_expired(base, now=__import__("time").time() + 25 * 3600), 1)
        self.assertEqual(self.err("import_health_export", {"upload_id": uid})["error"], "transfer_failed")


class RealServerProcessTests(unittest.TestCase):
    """16: настоящий процесс server.py, как его запускает tunnel-client (stdio, одна строка JSON на сообщение)."""

    def test_import_through_server_process(self):
        tmp = Path(tempfile.mkdtemp(prefix="fitness-mcp-proc-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db_path = tmp / "data" / "test.db"
        db.open_database(db_path).close()
        data = hs.encode(hs.export(1, sleep_sessions=[night()], steps=[hs.step()]))
        text = data.decode("utf-8")
        half = len(text) // 2
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "start_health_upload", "arguments": {
                "total_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "chunk_count": 2}}},
        ]
        env = dict(os.environ, FITNESS_DB_PATH=str(db_path), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(tmp), env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        def ask(msg):
            proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdin.flush()
            if "id" not in msg:
                return None
            line = proc.stdout.readline()
            line.decode("ascii")                       # stdout сервера — чистый ASCII
            return json.loads(line)

        responses = [ask(m) for m in messages]
        tool = {t["name"]: t for t in responses[2]["result"]["tools"]}["import_health_export"]
        self.assertEqual(tool["_meta"], {"openai/fileParams": ["file"]})
        uid = json.loads(responses[3]["result"]["content"][0]["text"])["upload_id"]
        for i, piece in enumerate((text[:half], text[half:])):
            ask({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {
                "name": "upload_health_chunk", "arguments": {"upload_id": uid, "index": i, "data": piece}}})
        results = []
        for n in range(2):
            res = ask({"jsonrpc": "2.0", "id": 20 + n, "method": "tools/call", "params": {
                "name": "import_health_export", "arguments": {"upload_id": uid}}})
            results.append(json.loads(res["result"]["content"][0]["text"]))
        proc.communicate(timeout=30)
        self.assertEqual(results[0]["status"], "imported")
        self.assertEqual(results[0]["counts"]["sleep"]["inserted"], 1)
        self.assertEqual(results[1]["error"], "transfer_failed")        # части уже удалены: повторного импорта нет
        conn = db.connect(db_path, create=False)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sleep_sessions").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
