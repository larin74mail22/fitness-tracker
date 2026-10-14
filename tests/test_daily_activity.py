"""MCP-инструмент get_daily_activity: шаги, сон и пульс по каждому дню периода.

Базы только временные, рабочая data/fitness.db не используется. Данные выдуманные.
"""

import os
from unittest import mock

from fitness import health_status, protocol, tools
from tests.health_helpers import add_sleep, add_source, add_step
from tests.test_dashboard import file_state
from tests.test_dashboard_health import HealthDbCase, add_hr


class DailyActivityTests(HealthDbCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"FITNESS_DB_PATH": str(self.db_path)})
        env.start()
        self.addCleanup(env.stop)

    def call(self, args):
        result = tools.call_tool("get_daily_activity", args)
        return result["isError"], result["structuredContent"]

    def fill(self):
        """Данные 10, 11 и 13 октября; 12-го записей нет, после 13-го ещё не было импорта."""
        conn = self.open()
        for i, d in enumerate(("2026-10-10", "2026-10-11", "2026-10-13")):
            add_step(conn, self.source, self.batch, hc_id=f"st-{i}", start=f"{d}T09:00:00Z", end=f"{d}T09:10:00Z",
                     count=1000 + i, local_date=d)
            add_hr(conn, self.source, self.batch, f"hr-{i}", f"{d}T10:00:00Z",
                   [(f"{d}T10:00:00Z", 60), (f"{d}T10:05:00Z", 90)], d)
            add_sleep(conn, self.source, self.batch, hc_id=f"sl-{i}", start=f"{d}T00:00:00Z", end=f"{d}T07:00:00Z",
                      local_date=d, stages=[("light", f"{d}T00:00:00Z", f"{d}T05:00:00Z"),
                                            ("deep", f"{d}T05:00:00Z", f"{d}T07:00:00Z")])
        conn.commit()
        return conn

    def test_every_day_present_and_missing_days_are_null(self):
        self.fill()
        error, body = self.call({"date_from": "2026-10-09", "date_to": "2026-10-14"})
        self.assertFalse(error, body)
        days = {d["date"]: d for d in body["days"]}
        self.assertEqual(list(days), ["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13",
                                      "2026-10-14"])
        self.assertEqual(days["2026-10-10"]["steps"], 1000)
        self.assertEqual(days["2026-10-10"]["sleep"]["asleep_min"], 420.0)
        self.assertEqual(days["2026-10-10"]["sleep"]["deep_min"], 120.0)
        self.assertEqual(days["2026-10-10"]["heart_rate"], {"min_bpm": 60, "avg_bpm": 75.0, "max_bpm": 90, "samples": 2})
        for day, status in (("2026-10-09", "before_first_record"), ("2026-10-12", "no_records"),
                            ("2026-10-14", "not_imported_yet")):
            d = days[day]
            self.assertIsNone(d["steps"])          # не ноль
            self.assertIsNone(d["sleep"])
            self.assertIsNone(d["heart_rate"])
            self.assertEqual((d["steps_status"], d["sleep_status"], d["heart_rate_status"]), (status,) * 3)
        self.assertEqual(body["totals"], {"days_with_steps": 3, "days_without_steps": 3, "steps_total": 3003,
                                          "steps_avg_per_day_with_data": 1001})

    def test_overlaps_deleted_and_second_source_not_double_counted(self):
        conn = self.open()
        d = "2026-10-10"
        add_step(conn, self.source, self.batch, hc_id="a", start=f"{d}T09:00:00Z", end=f"{d}T09:10:00Z",
                 count=600, local_date=d)
        # Перекрывает первую половину: учитывается только непокрытая часть (5 из 10 минут).
        add_step(conn, self.source, self.batch, hc_id="b", start=f"{d}T09:05:00Z", end=f"{d}T09:15:00Z",
                 count=400, local_date=d)
        add_step(conn, self.source, self.batch, hc_id="gone", start=f"{d}T11:00:00Z", end=f"{d}T11:10:00Z",
                 count=5000, local_date=d, deleted_at="2026-10-11T00:00:00Z")
        other = add_source(conn, key="health_connect|app.example.fit||||", package="app.example.fit")
        add_step(conn, other, self.batch, hc_id="c", start=f"{d}T09:00:00Z", end=f"{d}T09:10:00Z",
                 count=700, local_date=d)
        conn.commit()
        error, body = self.call({"date_from": d, "date_to": d})
        self.assertFalse(error, body)
        self.assertEqual(body["days"][0]["steps"], 800)   # max(600 + 200, 700), а не 1700 и не 6700

    def test_local_date_decides_day_boundary(self):
        conn = self.open()
        # 21:30 UTC 10 октября — это 00:30 11 октября по Москве (+180): шаги относятся к 11-му.
        add_step(conn, self.source, self.batch, hc_id="late", start="2026-10-10T21:30:00Z",
                 end="2026-10-10T21:40:00Z", count=321, local_date="2026-10-11",
                 start_offset_min=180, end_offset_min=180)
        add_step(conn, self.source, self.batch, hc_id="early", start="2026-10-10T20:50:00Z",
                 end="2026-10-10T20:59:00Z", count=123, local_date="2026-10-10",
                 start_offset_min=180, end_offset_min=180)
        conn.commit()
        error, body = self.call({"date_from": "2026-10-10", "date_to": "2026-10-11"})
        self.assertFalse(error, body)
        self.assertEqual([(d["date"], d["steps"]) for d in body["days"]], [("2026-10-10", 123), ("2026-10-11", 321)])

    def test_long_period_and_limits(self):
        self.fill()
        error, body = self.call({"date_from": "2026-07-13", "date_to": "2026-10-13"})   # ровно 93 дня
        self.assertFalse(error, body)
        self.assertEqual(len(body["days"]), health_status.MAX_ACTIVITY_DAYS)
        self.assertEqual(body["totals"]["days_with_steps"], 3)
        for args in ({"date_from": "2026-07-12", "date_to": "2026-10-13"},       # 94 дня
                     {"date_from": "2026-10-13"},
                     {"date_from": "2026-10-13", "date_to": "2026-10-01"},
                     {"date_from": "13.10.2026", "date_to": "2026-10-13"},
                     {"date_from": "2026-10-01", "date_to": "2026-10-13", "days": 3}):
            error, body = self.call(args)
            self.assertTrue(error, args)
            self.assertEqual(body["error"], "validation_error", args)

    def test_does_not_modify_database(self):
        conn = self.fill()
        conn.close()
        before = file_state(self.db_path)
        self.call({"date_from": "2026-10-01", "date_to": "2026-10-31"})
        self.assertEqual(file_state(self.db_path), before)

    def test_empty_health_tables(self):
        self.open().commit()
        error, body = self.call({"date_from": "2026-10-01", "date_to": "2026-10-02"})
        self.assertFalse(error, body)
        self.assertEqual({d["steps_status"] for d in body["days"]}, {"not_imported_yet"})
        self.assertIsNone(body["totals"]["steps_total"])

    def test_registered_for_mcp_client(self):
        tool = {t["name"]: t for t in tools.list_tools()}["get_daily_activity"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(tool["inputSchema"]["required"], ["date_from", "date_to"])
        self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertIn("get_daily_activity", protocol.INSTRUCTIONS)
        listed = protocol.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("get_daily_activity", [t["name"] for t in listed["result"]["tools"]])
