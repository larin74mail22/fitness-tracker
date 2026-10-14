"""MCP: работа с историей — тренировка за дату с полной детализацией, питание за день, вес и замеры за период,
агрегированная статистика (get_period_stats) и ограничение объёма ответа для больших выборок.

Все вызовы — во временную базу через протокол (как у tunnel-client), данные синтетические.
"""

import json
from datetime import date, timedelta

from tests.test_mcp import McpTestCase


def workout(day, n=1, title="Верх тела", weight=80, request_id=None):
    return {"workout_date": day, "title": title, "workout_type": "strength", "duration_min": 60,
            "notes": "Плечо не беспокоило", "client_request_id": request_id or f"w-{day}-{n}",
            "exercises": [
                {"name": "Жим лёжа", "discomfort": "лёгкий дискомфорт в локте",
                 "sets": [{"weight_kg": 40, "reps": 12, "is_warmup": True},
                          {"weight_kg": weight, "reps": 8, "rir": 2},
                          {"weight_kg": weight, "reps": 7, "rir": 1, "notes": "тяжело"}]},
                {"name": "Подтягивания", "sets": [{"weight_kg": 0, "reps": 10, "rir": 3}]},
                {"name": "Бег", "sets": [{"duration_sec": 600, "distance_m": 2000}]},
            ]}


class HistoryToolsTest(McpTestCase):
    def test_workout_for_date_with_full_detail(self):
        self.call_ok("log_workout", workout("2026-03-10"))
        self.call_ok("log_workout", workout("2026-03-11", title="Низ тела"))
        got = self.call_ok("get_workouts", {"date_from": "2026-03-10", "date_to": "2026-03-10"})
        self.assertEqual(got["count"], 1)
        self.assertFalse(got["truncated"])
        w = got["records"][0]
        self.assertEqual((w["workout_date"], w["title"], w["duration_min"], w["notes"]),
                         ("2026-03-10", "Верх тела", 60, "Плечо не беспокоило"))
        bench = w["exercises"][0]
        self.assertEqual(bench["name"], "Жим лёжа")
        self.assertEqual(bench["discomfort"], "лёгкий дискомфорт в локте")
        self.assertEqual([(s["set_number"], s["weight_kg"], s["reps"], s["rir"], s["is_warmup"]) for s in bench["sets"]],
                         [(1, 40, 12, None, True), (2, 80, 8, 2, False), (3, 80, 7, 1, False)])
        self.assertEqual(bench["sets"][2]["notes"], "тяжело")
        run = w["exercises"][2]["sets"][0]
        self.assertEqual((run["duration_sec"], run["distance_m"]), (600, 2000))
        # Одна запись целиком по id.
        record = self.call_ok("get_record", {"record_type": "workout", "id": w["id"]})["record"]
        self.assertEqual(record["exercises"], w["exercises"])

    def test_large_period_is_limited_and_summary_is_compact(self):
        start = date(2026, 1, 1)
        for i in range(25):
            self.call_ok("log_workout", workout((start + timedelta(days=i)).isoformat(), weight=60 + i))
        full = self.call_ok("get_workouts", {"date_from": "2026-01-01", "date_to": "2026-02-28"})
        self.assertEqual((full["count"], full["total_count"], full["truncated"]), (20, 25, True))
        self.assertEqual(full["records"][0]["workout_date"], "2026-01-06", "оставлены последние тренировки")
        self.assertEqual(full["records"][-1]["workout_date"], "2026-01-25")
        self.assertIn("get_record", full["hint"])
        summary = self.call_ok("get_workouts", {"date_from": "2026-01-01", "date_to": "2026-02-28", "limit": 100,
                                                "detail": "summary"})
        self.assertEqual((summary["count"], summary["truncated"]), (25, False))
        brief = summary["records"][-1]["exercises"][0]
        self.assertEqual(brief, {"name": "Жим лёжа", "sets": 3, "work_sets": 2, "max_weight_kg": 84, "reps": [8, 7]})
        self.assertLess(len(json.dumps(summary, ensure_ascii=False)), len(json.dumps(full, ensure_ascii=False)) * 1.25)
        err = self.call_error("get_workouts", {"date_from": "2026-01-01", "date_to": "2026-01-31", "limit": 0})
        self.assertEqual(err["error"], "validation_error")
        err = self.call_error("get_workouts", {"date_from": "2026-01-01", "date_to": "2026-01-31", "detail": "все"})
        self.assertEqual(err["error"], "validation_error")

    def test_nutrition_for_a_day(self):
        self.call_ok("log_meal", {"eaten_on": "2026-03-10", "eaten_time": "08:00", "meal_type": "breakfast",
                                  "client_request_id": "m1",
                                  "items": [{"name": "Овсянка", "quantity": 60, "unit": "g", "kcal": 220,
                                             "protein_g": 8, "nutrition_source": "label", "is_estimate": False},
                                            {"name": "Банан", "quantity": 1, "unit": "pcs"}]})
        self.call_ok("log_meal", {"eaten_on": "2026-03-11", "client_request_id": "m2",
                                  "items": [{"name": "Суп", "kcal": 300, "nutrition_source": "estimate"}]})
        day = self.call_ok("get_nutrition_summary", {"date_from": "2026-03-10", "date_to": "2026-03-10",
                                                     "include_meals": True})
        self.assertEqual(day["days_with_records"], 1)
        self.assertEqual(day["totals"]["kcal"], 220)
        self.assertFalse(day["days"][0]["totals_complete"], "банан без калорий — сумма неполная")
        self.assertEqual([i["name"] for i in day["meals"][0]["items"]], ["Овсянка", "Банан"])
        self.assertIsNone(day["meals"][0]["items"][1]["kcal"], "неизвестное значение не превращается в 0")
        self.assertFalse(day["meals_truncated"])

    def test_weight_and_measurements_history(self):
        for i, (kg, waist) in enumerate([(82.4, 90), (81.9, 89.5), (81.1, 88)]):
            self.call_ok("log_measurement", {"measured_on": f"2026-03-0{i + 1}", "weight_kg": kg,
                                             "measurements": [{"type": "waist", "value": waist}],
                                             "client_request_id": f"meas-{i}"})
        got = self.call_ok("get_measurements", {"date_from": "2026-03-01", "date_to": "2026-03-31"})
        self.assertEqual([r["weight_kg"] for r in got["records"]], [82.4, 81.9, 81.1])
        limited = self.call_ok("get_measurements", {"date_from": "2026-03-01", "date_to": "2026-03-31", "limit": 2})
        self.assertEqual((limited["count"], limited["records_total"], limited["records_truncated"]), (2, 3, True))
        self.assertEqual(limited["records"][-1]["measured_on"], "2026-03-03")

    def test_period_stats_is_compact_and_correct(self):
        self.call_ok("log_workout", workout("2026-03-10", weight=80))
        self.call_ok("log_workout", workout("2026-03-12", weight=85))
        self.call_ok("log_measurement", {"measured_on": "2026-03-01", "weight_kg": 82.4, "client_request_id": "a",
                                         "measurements": [{"type": "waist", "value": 90}]})
        self.call_ok("log_measurement", {"measured_on": "2026-03-20", "weight_kg": 81.0, "client_request_id": "b",
                                         "measurements": [{"type": "waist", "value": 88.5}]})
        self.call_ok("log_meal", {"eaten_on": "2026-03-10", "client_request_id": "m",
                                  "items": [{"name": "Рис", "kcal": 500, "protein_g": 10, "nutrition_source": "label"}]})
        self.call_ok("log_alcohol", {"consumed_on": "2026-03-11", "drink": "Пиво", "volume_ml": 500, "abv_percent": 5,
                                     "client_request_id": "al"})
        self.call_ok("add_note", {"note_date": "2026-03-12", "category": "сон", "text": "Плохо спал",
                                  "client_request_id": "n"})
        stats = self.call_ok("get_period_stats", {"date_from": "2026-03-01", "date_to": "2026-03-31"})
        w = stats["workouts"]
        self.assertEqual((w["workouts"], w["days_with_workouts"], w["duration_min_total"]), (2, 2, 120))
        bench = next(e for e in w["exercises"] if e["name"] == "Жим лёжа")
        self.assertEqual((bench["sessions"], bench["work_sets"], bench["max_weight_kg"], bench["total_reps"]),
                         (2, 4, 85, 30))
        self.assertEqual(bench["volume_kg"], 80 * 15 + 85 * 15)
        self.assertEqual((bench["first_date"], bench["last_date"]), ("2026-03-10", "2026-03-12"))
        body = stats["body"]
        self.assertEqual(body["weight"]["change_kg"], -1.4)
        self.assertEqual(body["measurements"]["waist"]["change"], -1.5)
        self.assertEqual(stats["nutrition"]["avg_per_day_with_kcal"]["kcal"], 500)
        self.assertEqual(stats["alcohol"]["entries_count"], 1)
        self.assertEqual(stats["notes_by_category"], {"сон": 1})
        self.assertIn("steps", stats["health"])
        for key in ('"records": [', '"entries": [', '"meals": [', '"days": ['):
            self.assertNotIn(key, json.dumps(stats), "только агрегаты, без списков записей")
        self.assertLess(len(json.dumps(stats, ensure_ascii=False)), 4000)

    def test_period_stats_on_empty_database_and_validation(self):
        stats = self.call_ok("get_period_stats", {"date_from": "2026-03-01", "date_to": "2026-03-31"})
        self.assertEqual(stats["workouts"]["workouts"], 0)
        self.assertIsNone(stats["body"]["weight"])
        err = self.call_error("get_period_stats", {"date_from": "2026-03-31", "date_to": "2026-03-01"})
        self.assertEqual(err["error"], "validation_error")

    def test_verbatim_text_is_not_promised(self):
        tool = next(t for t in self.rpc("tools/list")["result"]["tools"] if t["name"] == "get_workouts")
        self.assertIn("Исходный текст сообщения пользователя о тренировке не хранится", tool["description"])
        stats_tool = next(t for t in self.rpc("tools/list")["result"]["tools"] if t["name"] == "get_period_stats")
        self.assertTrue(stats_tool["annotations"]["readOnlyHint"])
