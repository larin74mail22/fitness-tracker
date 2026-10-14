"""Запись и чтение каждой категории, проверка входных данных, запросы за период."""

import unittest
from datetime import date, timedelta

from fitness import alcohol, measurements, notes, nutrition, workouts
from fitness.errors import NotFoundError, ValidationError
from tests.helpers import TempDbTestCase


class MeasurementTests(TempDbTestCase):
    def test_log_and_read(self):
        res = measurements.log_measurement(self.conn, {
            "measured_on": "2025-03-01", "measured_time": "07:45", "weight_kg": 81.4,
            "measurements": [{"type": "waist", "value": 86.5}, {"type": "chest", "value": 101}],
            "notes": "утром натощак",
        })
        self.assertFalse(res["duplicate"])
        rec = res["record"]
        self.assertEqual(rec["weight_kg"], 81.4)
        self.assertEqual({m["type"]: m["value"] for m in rec["measurements"]}, {"waist": 86.5, "chest": 101})
        self.assertNotIn("client_request_id", rec)
        self.assertTrue(rec["created_at"] and rec["updated_at"])

    def test_only_weight_and_only_measurements(self):
        measurements.log_measurement(self.conn, {"measured_on": "2025-03-01", "weight_kg": 80})
        measurements.log_measurement(self.conn, {"measured_on": "2025-03-02",
                                                 "measurements": [{"type": "hips", "value": 99}]})
        self.assertEqual(self.count("measurement_sessions"), 2)

    def test_requires_some_value(self):
        with self.assertRaises(ValidationError):
            measurements.log_measurement(self.conn, {"measured_on": "2025-03-01"})

    def test_requires_date(self):
        with self.assertRaises(ValidationError) as ctx:
            measurements.log_measurement(self.conn, {"weight_kg": 80})
        self.assertEqual(ctx.exception.field, "measured_on")

    def test_rejects_bad_values(self):
        bad = [
            {"measured_on": "2025-03-01", "weight_kg": "80"},             # строка вместо числа
            {"measured_on": "2025-03-01", "weight_kg": 5},                # нереальный вес
            {"measured_on": "2025-03-01", "weight_kg": True},
            {"measured_on": "01.03.2025", "weight_kg": 80},               # не тот формат даты
            {"measured_on": "2025-02-30", "weight_kg": 80},               # несуществующая дата
            {"measured_on": "2025-03-01", "measured_time": "25:00", "weight_kg": 80},
            {"measured_on": "2025-03-01", "measurements": [{"type": "nose", "value": 5}]},
            {"measured_on": "2025-03-01", "measurements": [{"type": "waist", "value": 86},
                                                           {"type": "waist", "value": 87}]},
            {"measured_on": "2025-03-01", "weight_kg": 80, "weight_lb": 176},   # неизвестное поле
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                measurements.log_measurement(self.conn, data)
        self.assertEqual(self.count("measurement_sessions"), 0)

    def test_future_date_rejected(self):
        future = (date.today() + timedelta(days=5)).isoformat()
        with self.assertRaises(ValidationError):
            measurements.log_measurement(self.conn, {"measured_on": future, "weight_kg": 80})

    def test_period_query(self):
        for day, weight in [("2025-01-10", 82), ("2025-01-20", 81), ("2025-02-05", 80)]:
            measurements.log_measurement(self.conn, {"measured_on": day, "weight_kg": weight})
        res = measurements.get_measurements(self.conn, "2025-01-01", "2025-01-31")
        self.assertEqual([r["weight_kg"] for r in res["records"]], [82, 81])
        res = measurements.get_measurements(self.conn, "2025-01-20", "2025-01-20")
        self.assertEqual(res["count"], 1)

    def test_period_validation(self):
        with self.assertRaises(ValidationError):
            measurements.get_measurements(self.conn, "2025-02-01", "2025-01-01")
        with self.assertRaises(ValidationError):
            measurements.get_measurements(self.conn, None, "2025-01-01")

    def test_filter_by_type(self):
        measurements.log_measurement(self.conn, {"measured_on": "2025-01-10", "weight_kg": 82})
        measurements.log_measurement(self.conn, {"measured_on": "2025-01-11",
                                                 "measurements": [{"type": "waist", "value": 85}]})
        self.assertEqual(measurements.get_measurements(self.conn, "2025-01-01", "2025-01-31", "waist")["count"], 1)
        self.assertEqual(measurements.get_measurements(self.conn, "2025-01-01", "2025-01-31", "weight")["count"], 1)

    def test_request_id_prevents_duplicates(self):
        data = {"measured_on": "2025-03-01", "weight_kg": 80, "client_request_id": "abc-1"}
        first = measurements.log_measurement(self.conn, data)
        second = measurements.log_measurement(self.conn, data)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.count("measurement_sessions"), 1)


class PhotoTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.photos = self.tmp / "photos"
        (self.photos / "2025").mkdir(parents=True)
        (self.photos / "2025" / "front.jpg").write_bytes(b"fake image")

    def test_add_photo_linked_to_measurement(self):
        session = measurements.log_measurement(self.conn, {"measured_on": "2025-03-01", "weight_kg": 80})
        res = measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "2025\\front.jpg",
                                                 "session_id": session["id"], "view": "спереди"},
                                     photos_dir=self.photos)
        self.assertEqual(res["record"]["file_path"], "2025/front.jpg")
        self.assertEqual(res["record"]["view"], "front")
        rec = measurements.get_measurement(self.conn, session["id"])
        self.assertEqual(len(rec["photos"]), 1)

    def test_photo_path_must_stay_inside_folder(self):
        for path in ["..\\secret.jpg", "C:\\Windows\\x.jpg", "/etc/passwd.jpg", "missing.jpg", "2025/front.exe"]:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": path},
                                       photos_dir=self.photos)

    def test_photo_with_unknown_session(self):
        with self.assertRaises(NotFoundError):
            measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "2025/front.jpg",
                                               "session_id": 999}, photos_dir=self.photos)
        self.assertEqual(self.count("photos"), 0)


WORKOUT = {
    "workout_date": "2025-04-02", "started_time": "18:30", "title": "Верх тела", "workout_type": "силовая",
    "exercises": [
        {"name": "Жим лёжа", "sets": [
            {"weight_kg": 40, "reps": 12, "is_warmup": True},
            {"weight_kg": 70, "reps": 8, "rir": 2},
            {"weight_kg": 70, "reps": 7, "rir": 1, "notes": "тяжело"},
        ], "discomfort": "лёгкий дискомфорт в левом плече"},
        {"name": "Планка", "sets": [{"duration_sec": 60}]},
    ],
}


class WorkoutTests(TempDbTestCase):
    def test_log_and_read_with_related_data(self):
        res = workouts.log_workout(self.conn, WORKOUT)
        rec = res["record"]
        self.assertEqual(rec["workout_type"], "strength")
        self.assertEqual([e["name"] for e in rec["exercises"]], ["Жим лёжа", "Планка"])
        bench = rec["exercises"][0]
        self.assertEqual([s["set_number"] for s in bench["sets"]], [1, 2, 3])
        self.assertTrue(bench["sets"][0]["is_warmup"])
        self.assertEqual(bench["sets"][1]["rir"], 2)
        self.assertEqual(bench["discomfort"], "лёгкий дискомфорт в левом плече")
        self.assertEqual(self.count("exercise_sets"), 4)

    def test_exercise_catalog_reused_case_insensitive(self):
        workouts.log_workout(self.conn, WORKOUT)
        second = dict(WORKOUT, workout_date="2025-04-05",
                      exercises=[{"name": "жим  ЛЕЖА", "sets": [{"weight_kg": 72, "reps": 6}]}])
        workouts.log_workout(self.conn, second)
        self.assertEqual(self.count("exercises"), 2)
        res = workouts.get_workouts(self.conn, "2025-04-01", "2025-04-30", exercise="Жим лежа")
        self.assertEqual(res["count"], 2)

    def test_invalid_set_saves_nothing(self):
        bad = dict(WORKOUT, exercises=[
            {"name": "Присед", "sets": [{"weight_kg": 100, "reps": 5}]},
            {"name": "Тяга", "sets": [{"weight_kg": 100}]},     # нет повторений/времени/дистанции
        ])
        with self.assertRaises(ValidationError):
            workouts.log_workout(self.conn, bad)
        for table in ("workouts", "workout_exercises", "exercise_sets", "exercises"):
            self.assertEqual(self.count(table), 0, table)

    def test_database_error_rolls_back_everything(self):
        # Ошибка SQLite посреди сохранения (третье упражнение) не должна оставить половину тренировки.
        self.conn.execute(
            "CREATE TRIGGER fail_third AFTER INSERT ON workout_exercises WHEN NEW.position = 3"
            " BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        data = dict(WORKOUT, exercises=WORKOUT["exercises"] + [{"name": "Подтягивания", "sets": [{"reps": 5}]}])
        import sqlite3
        with self.assertRaises(sqlite3.Error):
            workouts.log_workout(self.conn, data)
        for table in ("workouts", "workout_exercises", "exercise_sets"):
            self.assertEqual(self.count(table), 0, table)

    def test_validation(self):
        bad = [
            dict(WORKOUT, exercises=[]),
            dict(WORKOUT, title="  "),
            dict(WORKOUT, workout_type="йога-марафон"),
            dict(WORKOUT, exercises=[{"name": "Жим", "sets": [{"reps": 8.5}]}]),
            dict(WORKOUT, exercises=[{"name": "Жим", "sets": [{"reps": 8, "rir": 12}]}]),
            dict(WORKOUT, exercises=[{"name": "Жим", "sets": [{"reps": -1}]}]),
            {k: val for k, val in WORKOUT.items() if k != "workout_date"},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                workouts.log_workout(self.conn, data)

    def test_period_and_type_filter(self):
        workouts.log_workout(self.conn, WORKOUT)
        workouts.log_workout(self.conn, {"workout_date": "2025-05-01", "title": "Бег", "workout_type": "cardio",
                                         "exercises": [{"name": "Бег", "sets": [{"distance_m": 5000,
                                                                                   "duration_sec": 1800}]}]})
        self.assertEqual(workouts.get_workouts(self.conn, "2025-04-01", "2025-04-30")["count"], 1)
        self.assertEqual(workouts.get_workouts(self.conn, "2025-01-01", "2025-12-31",
                                               workout_type="cardio")["count"], 1)

    def test_request_id_prevents_duplicates(self):
        data = dict(WORKOUT, client_request_id="w-1")
        workouts.log_workout(self.conn, data)
        self.assertTrue(workouts.log_workout(self.conn, data)["duplicate"])
        self.assertEqual(self.count("workouts"), 1)
        self.assertEqual(self.count("exercise_sets"), 4)


class NutritionTests(TempDbTestCase):
    def test_log_and_summary(self):
        nutrition.log_meal(self.conn, {"eaten_on": "2025-06-01", "eaten_time": "08:00", "meal_type": "завтрак",
                                       "items": [
                                           {"name": "Овсянка", "quantity": 60, "unit": "г", "kcal": 220,
                                            "protein_g": 8, "fat_g": 4, "carbs_g": 36,
                                            "nutrition_source": "label", "is_estimate": False},
                                           {"name": "Банан", "quantity": 1, "unit": "шт", "kcal": 100,
                                            "nutrition_source": "estimate"},
                                       ]})
        nutrition.log_meal(self.conn, {"eaten_on": "2025-06-01", "items": [{"name": "Салат"}]})
        nutrition.log_meal(self.conn, {"eaten_on": "2025-06-02", "items": [
            {"name": "Творог", "quantity": 200, "unit": "g", "kcal": 240, "protein_g": 36,
             "nutrition_source": "label"}]})

        summary = nutrition.get_nutrition_summary(self.conn, "2025-06-01", "2025-06-02", include_meals=True)
        day1, day2 = summary["days"]
        self.assertEqual(day1["kcal"], 320)
        self.assertEqual(day1["protein_g"], 8)
        self.assertEqual(day1["items_without_kcal"], 1)
        self.assertFalse(day1["totals_complete"])
        self.assertEqual(day1["items_estimated"], 2)    # банан (оценка) и салат (по умолчанию приблизительно)
        self.assertTrue(day2["totals_complete"])
        self.assertEqual(summary["totals"]["kcal"], 560)
        self.assertEqual(len(summary["meals"]), 3)

    def test_unknown_values_stay_null(self):
        res = nutrition.log_meal(self.conn, {"eaten_on": "2025-06-01", "items": [{"name": "Суп"}]})
        item = res["record"]["items"][0]
        self.assertIsNone(item["kcal"])
        self.assertIsNone(item["quantity"])
        self.assertTrue(item["is_estimate"])

    def test_validation(self):
        bad = [
            {"eaten_on": "2025-06-01", "items": []},
            {"eaten_on": "2025-06-01", "items": [{"name": "Хлеб", "quantity": 50}]},             # нет единицы
            {"eaten_on": "2025-06-01", "items": [{"name": "Хлеб", "quantity": 1, "unit": "ломтик"}]},
            {"eaten_on": "2025-06-01", "items": [{"name": "Хлеб", "kcal": 120}]},              # нет источника
            {"eaten_on": "2025-06-01", "items": [{"name": "Хлеб", "kcal": -5, "nutrition_source": "user"}]},
            {"eaten_on": "2025-06-01", "items": [{"name": "Хлеб", "kcal": 100, "nutrition_source": "estimate",
                                                  "is_estimate": False}]},
            {"items": [{"name": "Хлеб"}]},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                nutrition.log_meal(self.conn, data)
        self.assertEqual(self.count("meals"), 0)

    def test_empty_period(self):
        summary = nutrition.get_nutrition_summary(self.conn, "2025-06-01", "2025-06-30")
        self.assertEqual(summary["days"], [])
        self.assertIsNone(summary["totals"]["kcal"])


class AlcoholTests(TempDbTestCase):
    def test_ethanol_and_kcal_estimate(self):
        res = alcohol.log_alcohol(self.conn, {"consumed_on": "2025-07-01", "drink": "Пиво",
                                              "volume_ml": 500, "abv_percent": 5})
        rec = res["record"]
        self.assertAlmostEqual(rec["ethanol_g"], 19.7)
        self.assertAlmostEqual(rec["kcal"], 137.9)
        self.assertEqual(rec["kcal_source"], "ethanol_only_estimate")
        self.assertTrue(rec["kcal_is_estimate"])

    def test_unknown_volume_keeps_values_empty(self):
        rec = alcohol.log_alcohol(self.conn, {"consumed_on": "2025-07-01", "drink": "Вино"})["record"]
        self.assertIsNone(rec["ethanol_g"])
        self.assertIsNone(rec["kcal"])
        self.assertIsNone(rec["kcal_is_estimate"])

    def test_user_kcal(self):
        rec = alcohol.log_alcohol(self.conn, {"consumed_on": "2025-07-01", "drink": "Сидр", "volume_ml": 330,
                                              "abv_percent": 4.5, "kcal": 160, "kcal_source": "label"})["record"]
        self.assertEqual(rec["kcal"], 160)
        self.assertFalse(rec["kcal_is_estimate"])

    def test_validation(self):
        bad = [
            {"consumed_on": "2025-07-01"},
            {"consumed_on": "2025-07-01", "drink": "Пиво", "abv_percent": 150},
            {"consumed_on": "2025-07-01", "drink": "Пиво", "volume_ml": 0},
            {"consumed_on": "2025-07-01", "drink": "Пиво", "kcal": 200},
            {"drink": "Пиво"},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                alcohol.log_alcohol(self.conn, data)

    def test_summary(self):
        alcohol.log_alcohol(self.conn, {"consumed_on": "2025-07-01", "drink": "Пиво", "volume_ml": 500,
                                        "abv_percent": 5})
        alcohol.log_alcohol(self.conn, {"consumed_on": "2025-07-01", "drink": "Вино"})
        alcohol.log_alcohol(self.conn, {"consumed_on": "2025-07-03", "drink": "Пиво", "volume_ml": 500,
                                        "abv_percent": 5})
        alcohol.log_alcohol(self.conn, {"consumed_on": "2025-08-01", "drink": "Пиво", "volume_ml": 500,
                                        "abv_percent": 5})
        s = alcohol.get_alcohol_summary(self.conn, "2025-07-01", "2025-07-31")
        self.assertEqual(s["entries_count"], 3)
        self.assertEqual(s["days_with_alcohol"], 2)
        self.assertAlmostEqual(s["ethanol_g_total"], 39.4)
        self.assertEqual(s["entries_without_ethanol"], 1)


class NoteTests(TempDbTestCase):
    def test_note_with_fields(self):
        res = notes.add_note(self.conn, {"note_date": "2025-08-01", "category": "Сон", "text": "Спал плохо",
                                         "fields": [{"key": "часы сна", "value": 5.5, "unit": "ч"},
                                                    {"key": "качество", "value": "плохое"}]})
        rec = res["record"]
        self.assertEqual(rec["category"], "сон")
        self.assertEqual(rec["fields"][0], {"key": "часы сна", "value": 5.5, "unit": "ч"})
        self.assertEqual(rec["fields"][1]["value"], "плохое")

    def test_validation(self):
        bad = [
            {"note_date": "2025-08-01", "category": "сон"},
            {"note_date": "2025-08-01", "text": "x"},
            {"note_date": "2025-08-01", "category": "сон", "text": "x", "fields": [{"key": "a"}]},
            {"note_date": "2025-08-01", "category": "сон", "text": "x",
             "fields": [{"key": "a", "value": 1}, {"key": "A", "value": 2}]},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                notes.add_note(self.conn, data)

    def test_missing_note(self):
        with self.assertRaises(NotFoundError):
            notes.get_note(self.conn, 42)


if __name__ == "__main__":
    unittest.main()
