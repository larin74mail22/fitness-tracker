"""Язык интерфейса: словари EN/RU, выбор и сохранение языка, обе версии страниц панели.

Панель поднимается на временной базе, как в test_dashboard.py; рабочая data/fitness.db и data/launcher.json
не используются. Данные выдуманные.
"""

import json
import os
import re
import string
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from fitness import alcohol, db, health_status, i18n, measurements, notes, nutrition, photo_store, workouts
from fitness.launcher import components, config
from fitness.launcher import supervisor as sv
from fitness.locales import en, ru
from fitness.web import pages, photo_pages, sync_page
from tests.test_dashboard import days_ago, file_state
from tests.test_dashboard_health import HealthDbCase
from tests.test_dashboard_photos import PhotoTestCase, jpeg

ROOT = Path(__file__).resolve().parent.parent
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LANG_FORM = re.compile(r'<form class="lang".*?</form>', re.S)


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class CatalogTests(unittest.TestCase):
    def test_same_keys_in_both_languages(self):
        self.assertEqual(sorted(set(en.MESSAGES) - set(ru.MESSAGES)), [], "нет перевода на русский")
        self.assertEqual(sorted(set(ru.MESSAGES) - set(en.MESSAGES)), [], "нет перевода на английский")

    def test_same_placeholders(self):
        for key, text in en.MESSAGES.items():
            with self.subTest(key=key):
                self.assertEqual(placeholders(text), placeholders(ru.MESSAGES[key]))

    def test_texts_are_plain_and_not_empty(self):
        # Часть текстов вставляется в HTML без экранирования, поэтому разметки в словарях быть не должно.
        for lang, messages in i18n.MESSAGES.items():
            for key, text in messages.items():
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(text.strip())
                    self.assertNotRegex(text, r"[<>&]")

    def test_english_texts_have_no_cyrillic(self):
        self.assertEqual([k for k, v in en.MESSAGES.items() if CYRILLIC.search(v)], [])

    def test_every_key_used_in_code_exists(self):
        used = set()
        for path in (ROOT / "fitness").rglob("*.py"):
            used |= set(re.findall(r"""\bt\(\s*["']([a-z0-9_.]+[a-z0-9_])["']""", path.read_text(encoding="utf-8")))
        used |= {key for _, key in pages.NAV} | set(pages.PERIODS.values())
        used |= {"photos.size." + k for k in photo_pages.SIZES} | {"photos.notice." + k for k in photo_pages.NOTICES}
        used |= {"view." + v for v in photo_pages.SECTIONS} | {"sync.outcome." + k for k in sync_page.OUTCOMES}
        used |= {"view." + v for v in photo_store.PHOTO_VIEWS}
        used |= {"state." + s for s in components.STATE_TEXT}
        used |= {f"status.component.{c}" for c in ("tunnel", "import", "dashboard")}
        self.assertGreater(len(used), 250)
        self.assertEqual(sorted(used - set(en.MESSAGES)), [])

    def test_database_codes_translated(self):
        sql = (ROOT / "fitness" / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
        codes = re.findall(r"^\s*\('([a-z_]+)',\s*'", sql, re.M)
        self.assertIn("waist", codes)
        self.assertEqual([c for c in codes if "measure." + c not in en.MESSAGES], [])
        for group, values in (("workout_type", re.search(r"workout_type\s+TEXT.*?IN \(([^)]*)\)", sql, re.S)),
                              ("meal_type", re.search(r"meal_type\s+TEXT.*?IN \(([^)]*)\)", sql, re.S)),
                              ("unit", re.search(r"\bunit\s+TEXT.*?IN \(([^)]*'g'[^)]*)\)", sql, re.S))):
            self.assertIsNotNone(values, group)
            for code in re.findall(r"'([a-z]+)'", values.group(1)):
                with self.subTest(group=group, code=code):
                    self.assertIn(f"{group}.{code}", en.MESSAGES)

    @unittest.skipUnless(os.name == "nt", "значок в трее есть только в Windows")
    def test_tray_menu_translated(self):
        from fitness.launcher import tray
        for item in filter(None, tray.MENU):
            self.assertIn(item[1], en.MESSAGES)
            self.assertNotEqual(i18n.t(item[1], lang="en"), i18n.t(item[1], lang="ru"))


class TranslateTests(unittest.TestCase):
    def test_default_is_english(self):
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "en")
        self.assertEqual(i18n.get_language(), "en")
        self.assertEqual(i18n.t("nav.workouts"), "Workouts")

    def test_explicit_and_context_language(self):
        self.assertEqual(i18n.t("nav.workouts", lang="ru"), "Тренировки")
        with i18n.use_language("ru"):
            self.assertEqual(i18n.t("nav.workouts"), "Тренировки")
            self.assertEqual(i18n.t("photos.compare_gap", n=3), "Между снимками 3 дн.")
        self.assertEqual(i18n.t("nav.workouts"), "Workouts")

    def test_unknown_language_falls_back_to_english(self):
        for value in ("de", "", None, "RU ", "<script>"):
            with self.subTest(value=value):
                self.assertEqual(i18n.normalize(value), "ru" if value == "RU " else "en")

    def test_missing_translation(self):
        with mock.patch.dict(ru.MESSAGES, clear=False):
            del ru.MESSAGES["nav.workouts"]
            self.assertEqual(i18n.t("nav.workouts", lang="ru"), "Workouts")   # нет перевода — английский текст
        self.assertEqual(i18n.t("no.such.key", lang="ru"), "no.such.key")      # нет ключа — сам ключ виден

    def test_number_and_duration_formats(self):
        self.assertEqual(i18n.group_thousands(12345.4, "en"), "12,345")
        self.assertEqual(i18n.group_thousands(12345.4, "ru"), "12\u00a0345")
        with i18n.use_language("en"):
            self.assertEqual(pages.fmt_duration(425), "7 h 05 min")
            self.assertEqual(pages.fmt(81.2, unit=pages.u("kg")), "81.2 kg")
        with i18n.use_language("ru"):
            self.assertEqual(pages.fmt_duration(425), "7 ч 05 мин")
            self.assertEqual(pages.fmt(81.2, unit=pages.u("kg")), "81.2 кг")

    def test_status_text_in_both_languages(self):
        status = {"version": "x", "started_at": "t", "pid": 1, "database": "d", "logs": "l", "mcp_check": None,
                  "components": {name: {"state": "running", "state_text": "работает", "message": ""}
                                 for name in ("tunnel", "import", "dashboard")}}
        self.assertIn("Auto-import: running", "\n".join(sv.describe_status(status, "en")))
        self.assertIn("Автоимпорт: работает", "\n".join(sv.describe_status(status, "ru")))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-i18n-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "data" / "fitness.db"
        self.config_file = config.paths(self.db_path)["config"]

    def test_fresh_install_is_english_and_nothing_created(self):
        self.assertEqual(config.read_language(self.db_path), "en")
        self.assertFalse(self.config_file.exists())
        self.assertFalse(self.db_path.parent.exists())
        self.assertEqual(config.DEFAULTS["language"], "en")

    def test_save_keeps_other_settings(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text(json.dumps({"dashboard_port": 9000, "custom": "keep"}), encoding="utf-8")
        config.save_language("ru", self.db_path)
        self.assertEqual(config.read_language(self.db_path), "ru")
        stored = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(stored, {"dashboard_port": 9000, "custom": "keep", "language": "ru"})
        self.assertEqual(config.load(self.db_path, write_defaults=False)["dashboard_port"], 9000)
        config.save_language("en", self.db_path)
        self.assertEqual(config.read_language(self.db_path), "en")
        self.assertFalse(self.config_file.with_name("launcher.json.tmp").exists())

    def test_save_without_file_writes_defaults(self):
        config.save_language("ru", self.db_path)
        self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")),
                         dict(config.DEFAULTS, language="ru"))

    def test_corrupt_file_not_overwritten(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("{broken", encoding="utf-8")
        self.assertEqual(config.read_language(self.db_path), "en")
        with self.assertRaises(ValueError):
            config.save_language("ru", self.db_path)
        self.assertEqual(self.config_file.read_text(encoding="utf-8"), "{broken")

    def test_existing_install_keeps_russian(self):
        # launcher.json старой версии (без ключа language): установка работала по-русски и остаётся на русском.
        old = {key: value for key, value in config.DEFAULTS.items() if key != "language"}
        old["dashboard_port"] = 8800
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(config.read_language(self.db_path), "ru")
        self.assertEqual(config.load(self.db_path)["dashboard_port"], 8800)
        self.assertNotIn("language", json.loads(self.config_file.read_text(encoding="utf-8")), "файл не меняется")
        config.save_language("en", self.db_path)
        self.assertEqual(config.read_language(self.db_path), "en")
        self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")), dict(old, language="en"))

    def test_launcher_first_start_writes_english(self):
        config.load(self.db_path)              # первый запуск launcher создаёт файл настроек
        self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8"))["language"], "en")
        self.assertEqual(config.read_language(self.db_path), "en")

    def test_bom_and_unicode_settings_preserved(self):
        self.config_file.parent.mkdir(parents=True)
        original = {"tunnel_client": "C:/Программы/tunnel-client.exe", "import_interval_sec": 45, "language": "en"}
        self.config_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8-sig")
        config.save_language("ru", self.db_path)
        self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")), dict(original, language="ru"))

    def test_unknown_language_rejected_and_bad_value_ignored(self):
        with self.assertRaises(ValueError):
            config.save_language("de", self.db_path)
        self.assertFalse(self.config_file.exists())
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text(json.dumps({"language": "de"}), encoding="utf-8")
        self.assertEqual(config.read_language(self.db_path), "en")


class DashboardLanguageTests(PhotoTestCase, HealthDbCase):
    ui_language = None          # как у пользователя: язык из data/launcher.json

    def fill_all(self):
        conn = db.open_database(self.db_path)
        try:
            session = measurements.log_measurement(conn, {"measured_on": days_ago(5), "weight_kg": 81.2,
                                                          "measurements": [{"type": "waist", "value": 86.5}],
                                                          "notes": "morning"})
            measurements.log_measurement(conn, {"measured_on": days_ago(20), "weight_kg": 82.0})
            workouts.log_workout(conn, {"workout_date": days_ago(3), "title": "Upper body", "workout_type": "strength",
                                        "duration_min": 55, "exercises": [
                                            {"name": "Bench press", "discomfort": "shoulder", "sets": [
                                                {"weight_kg": 40, "reps": 10, "is_warmup": True},
                                                {"weight_kg": 60, "reps": 8, "rir": 2}]}]})
            nutrition.log_meal(conn, {"eaten_on": days_ago(2), "meal_type": "breakfast", "items": [
                {"name": "Oatmeal", "quantity": 60, "unit": "g", "kcal": 220, "protein_g": 8,
                 "nutrition_source": "label", "is_estimate": False},
                {"name": "Apple", "quantity": 1, "unit": "pcs", "kcal": 80, "nutrition_source": "estimate",
                 "is_estimate": True}]})
            alcohol.log_alcohol(conn, {"consumed_on": days_ago(1), "drink": "Beer", "volume_ml": 500,
                                       "abv_percent": 5})
            alcohol.log_alcohol(conn, {"consumed_on": days_ago(1), "drink": "Wine"})
            notes.add_note(conn, {"note_date": days_ago(1), "category": "test", "text": "note"})
        finally:
            conn.close()
        self.fill_health(self.open())
        self.front = self.add_existing("front.jpg", days_ago(5), "front", jpeg(b"1"), notes="first")
        self.side = self.add_existing("side.jpg", days_ago(4), "side", jpeg(b"2"))
        self.trashed = self.add_existing("back.jpg", days_ago(3), "back", jpeg(b"3"))
        return session

    def switch(self, lang, next_path="/", **extra):
        fields = {"csrf": self.server.csrf_token, "lang": lang, "next": next_path, **extra}
        return self.post("/settings/language", urlencode(fields).encode())

    def page_paths(self):
        conn = db.open_database(self.db_path)
        try:
            trash_id = conn.execute("SELECT id FROM photo_trash").fetchone()[0]
        finally:
            conn.close()
        return ["/", "/?period=all", "/measurements", "/measurements?type=waist", "/workouts",
                "/workouts?exercise=Bench+press", "/nutrition", "/alcohol", "/health", "/sync", "/photos",
                "/photos/upload", f"/photos/{self.front}", f"/photos/{self.front}/delete",
                f"/photos/compare?id={self.front}&id={self.side}", "/photos/trash",
                f"/photos/trash/{trash_id}/purge", "/no-such-page"]

    def trash_one(self):
        conn = db.open_database(self.db_path)
        try:
            from fitness import records
            code = records.prepare_delete(conn, "photo", self.trashed)["confirmation_code"]
        finally:
            conn.close()
        status, _, body = self.form(f"/photos/{self.trashed}/delete", code=code, period="90")
        self.assertEqual(status, 303, body)

    def get_any(self, path):
        status, _, body = self.request(path)
        self.assertIn(status, (200, 404), path)
        return body.decode("utf-8")

    def test_fresh_install_english_everywhere(self):
        self.fill_all()
        self.start()
        self.trash_one()
        self.assertFalse(config.paths(self.db_path)["config"].exists(), "настройки не созданы без переключения")
        for path in self.page_paths():
            with self.subTest(path=path):
                html = self.get_any(path)
                self.assertIn('<html lang="en">', html)
                self.assertIn('value="en" lang="en" aria-pressed="true"', html)
                ui = LANG_FORM.sub("", html)
                self.assertEqual(CYRILLIC.findall(ui), [], f"непереведённый текст на {path}")
        html = self.get_html("/")
        self.assertIn(">Workouts</a>", html)
        self.assertIn("81.2 kg", html)
        self.assertIn("3,090", self.get_html("/health"))           # разделитель разрядов в английском
        self.assertIn("7 h 30 min", self.get_html("/health"))

    def test_russian_after_switch_and_after_restart(self):
        self.fill_all()
        self.start()
        self.trash_one()
        status, headers, _ = self.switch("ru", "/workouts?period=30")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/workouts?period=30")
        self.assertEqual(config.read_language(self.db_path), "ru")
        for path in self.page_paths():
            with self.subTest(path=path):
                html = self.get_any(path)
                self.assertIn('<html lang="ru">', html)
                self.assertIn('value="ru" lang="ru" aria-pressed="true"', html)
        html = self.get_html("/")
        self.assertIn(">Тренировки</a>", html)
        self.assertIn("81.2 кг", html)
        self.assertIn("3\u00a0090", self.get_html("/health"))      # неразрывный пробел между разрядами
        # Перезапуск панели: язык читается из настроек.
        self.server.shutdown()
        self.server.server_close()
        self.start()
        self.assertIn('<html lang="ru">', self.get_html("/"))
        self.assertEqual(self.switch("en")[0], 303)
        self.assertIn('<html lang="en">', self.get_html("/"))

    def test_switching_does_not_touch_fitness_data(self):
        self.fill_all()
        self.start()
        self.trash_one()
        before = file_state(self.db_path)
        photos_before = sorted(p.name for p in self.photos.iterdir())
        for lang in ("ru", "en", "ru"):
            self.assertEqual(self.switch(lang, "/photos")[0], 303)
            self.get_html("/photos")
            self.get_html("/nutrition")
        self.assertEqual(file_state(self.db_path), before)
        self.assertEqual(sorted(p.name for p in self.photos.iterdir()), photos_before)
        settings = json.loads(config.paths(self.db_path)["config"].read_text(encoding="utf-8"))
        self.assertEqual(settings["language"], "ru")

    def test_switch_requires_form_secret_and_origin(self):
        self.start()
        cases = [
            ({"csrf": "wrong", "lang": "ru"}, True, 403),
            ({"lang": "ru"}, True, 403),
            ({"csrf": self.server.csrf_token, "lang": "ru"}, "http://evil.example", 403),
            ({"csrf": self.server.csrf_token, "lang": "ru"}, False, 403),
            ({"csrf": self.server.csrf_token, "lang": "de"}, True, 400),
        ]
        for fields, origin, expected in cases:
            with self.subTest(fields=fields, origin=origin):
                status, _, body = self.post("/settings/language", urlencode(fields).encode(), origin=origin)
                self.assertEqual(status, expected, body)
        self.assertFalse(config.paths(self.db_path)["config"].exists())
        self.assertEqual(self.request("/settings/language")[0], 404)     # GET язык не меняет

    def test_redirect_stays_inside_dashboard(self):
        self.start()
        for next_path in ("//evil.example/", "http://evil.example/", "/\\evil.example", "", "photos",
                          "/фото", "/x\ty"):
            with self.subTest(next_path=next_path):
                status, headers, _ = self.switch("ru", next_path)
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], "/")

    def test_corrupt_settings_reported_not_overwritten(self):
        self.start()
        config_file = config.paths(self.db_path)["config"]
        config_file.write_text("{broken", encoding="utf-8")
        self.assertIn('<html lang="en">', self.get_html("/"))           # панель работает на языке по умолчанию
        status, _, body = self.switch("ru")
        self.assertEqual(status, 500)
        self.assertIn("The language was not saved", body)
        self.assertEqual(config_file.read_text(encoding="utf-8"), "{broken")

    def test_mcp_error_texts_stay_russian(self):
        # Ошибки чтения базы попадают и в ответы MCP-инструментов: там они остаются прежними (по-русски),
        # а панель показывает их на языке интерфейса.
        missing = self.tmp / "missing" / "fitness.db"
        status = health_status.sync_status(missing)
        self.assertIn("База ещё не создана", status["database"]["error"])
        with self.assertRaises(Exception) as caught:
            health_status.daily_activity(missing, days_ago(3), days_ago(0))
        self.assertIn("База ещё не создана", str(caught.exception))
        server = self.start()
        server.db_path = missing
        html = self.get_html("/")
        self.assertIn("The database has not been created yet", html)

    def test_sync_status_page_both_languages(self):
        status = health_status.sync_status(self.db_path)
        with i18n.use_language("en"):
            self.assertIn("Inbox folder", sync_page.sync_page(status))
        with i18n.use_language("ru"):
            self.assertIn("Входящая папка", sync_page.sync_page(status))


if __name__ == "__main__":
    unittest.main()
