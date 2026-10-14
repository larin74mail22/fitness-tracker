"""Health Connect в веб-панели: дневные сводки шагов, пульса и сна, обзор и страница «Здоровье».

Базы только временные, рабочая data/fitness.db не используется. Данные выдуманные.
"""

import unittest

from fitness import db, notes
from fitness.web import charts, health, pages
from fitness.web.readonly import readonly_connection
from tests.health_helpers import add_batch, add_sleep, add_source, add_step, canon
from tests.test_dashboard import DashboardTestCase, days_ago, file_state

HEALTH_PAGES = ["/health", "/health?period=30", "/health?period=365", "/health?period=all", "/health?period=bogus",
                "/", "/?period=all"]


def add_hr(conn, source, batch, hc_id, start, samples, local_date, offset=180, deleted_at=None):
    """Запись пульса с измерениями [(время, bpm)]; конец записи — время последнего измерения."""
    times = [t for t, _ in samples]
    record_id = conn.execute(
        "INSERT INTO heart_rate_records (source_id, hc_id, start_utc, end_utc, start_offset_min, end_offset_min,"
        " local_date, hc_last_modified, payload_hash, batch_id, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, hc_id, canon(start), canon(max(times + [start])), offset, offset, local_date,
         canon("2026-10-10T17:00:00Z"), "0" * 64, batch, deleted_at)).lastrowid
    for t, bpm in samples:
        conn.execute("INSERT INTO heart_rate_samples (record_id, time_utc, bpm) VALUES (?, ?, ?)",
                     (record_id, canon(t), bpm))
    return record_id


class HealthDbCase(DashboardTestCase):
    def open(self):
        conn = db.open_database(self.db_path)
        self.addCleanup(conn.close)
        self.source = add_source(conn)
        self.batch = add_batch(conn)
        return conn

    def fill_health(self, conn, day_offsets=(1, 2)):
        for i, n in enumerate(day_offsets):
            d = days_ago(n)
            add_step(conn, self.source, self.batch, hc_id=f"st-{i}-a", start=f"{d}T09:00:00Z", end=f"{d}T09:10:00Z",
                     count=1200, local_date=d)
            add_step(conn, self.source, self.batch, hc_id=f"st-{i}-b", start=f"{d}T10:00:00Z", end=f"{d}T10:10:00Z",
                     count=345, local_date=d)
            add_hr(conn, self.source, self.batch, f"hr-{i}", f"{d}T10:00:00Z",
                   [(f"{d}T10:00:00Z", 61), (f"{d}T10:05:00Z", 99)], d)
            add_sleep(conn, self.source, self.batch, hc_id=f"sl-{i}", start=f"{d}T00:00:00Z", end=f"{d}T07:30:00Z",
                      local_date=d, stages=[("light", f"{d}T00:00:00Z", f"{d}T05:00:00Z"),
                                            ("deep", f"{d}T05:00:00Z", f"{d}T07:00:00Z"),
                                            ("awake", f"{d}T07:00:00Z", f"{d}T07:30:00Z")])
        conn.commit()


# ---------- Агрегация ----------

class StepsTests(HealthDbCase):
    def test_daily_sum_and_deleted_excluded(self):
        conn = self.open()
        add_step(conn, self.source, self.batch, hc_id="a", start="2026-10-10T08:00:00Z", end="2026-10-10T08:10:00Z",
                 count=100)
        add_step(conn, self.source, self.batch, hc_id="b", start="2026-10-10T09:00:00Z", end="2026-10-10T09:10:00Z",
                 count=250)
        add_step(conn, self.source, self.batch, hc_id="del", start="2026-10-10T11:00:00Z",
                 end="2026-10-10T11:10:00Z", count=9999, deleted_at="2026-10-11T00:00:00Z")
        add_step(conn, self.source, self.batch, hc_id="c", start="2026-10-11T09:00:00Z", end="2026-10-11T09:10:00Z",
                 count=7, local_date="2026-10-11")
        days = health.daily_steps(conn, "2026-10-01", "2026-10-31")
        self.assertEqual([(d["date"], d["steps"], d["intervals"]) for d in days],
                         [("2026-10-10", 350, 2), ("2026-10-11", 7, 1)])
        self.assertFalse(any(d["overlap"] for d in days))

    def test_overlapping_intervals_not_doubled(self):
        conn = self.open()
        add_step(conn, self.source, self.batch, hc_id="a", start="2026-10-10T10:00:00Z", end="2026-10-10T10:10:00Z",
                 count=600)
        # Половина интервала уже покрыта предыдущим: учитывается только вторая половина (200 из 400).
        add_step(conn, self.source, self.batch, hc_id="b", start="2026-10-10T10:05:00Z", end="2026-10-10T10:15:00Z",
                 count=400)
        # Полностью внутри уже учтённого времени: не добавляет ничего.
        add_step(conn, self.source, self.batch, hc_id="c", start="2026-10-10T10:01:00Z", end="2026-10-10T10:04:00Z",
                 count=600)
        [day] = health.daily_steps(conn, "2026-10-10", "2026-10-10")
        self.assertEqual(day["steps"], 800)
        self.assertTrue(day["overlap"])

    def test_several_sources_take_largest(self):
        conn = self.open()
        other = add_source(conn, key="health_connect|app.example.phone||||", package="app.example.phone")
        add_step(conn, self.source, self.batch, hc_id="w", start="2026-10-10T10:00:00Z", end="2026-10-10T10:10:00Z",
                 count=1000)
        add_step(conn, other, self.batch, hc_id="p", start="2026-10-10T10:00:00Z", end="2026-10-10T10:10:00Z",
                 count=900)
        [day] = health.daily_steps(conn, "2026-10-10", "2026-10-10")
        self.assertEqual((day["steps"], day["sources"]), (1000, 2))


class HeartRateTests(HealthDbCase):
    def test_samples_not_records_and_duplicates_once(self):
        conn = self.open()
        add_hr(conn, self.source, self.batch, "r1", "2026-10-10T10:00:00Z",
               [("2026-10-10T10:00:00Z", 60), ("2026-10-10T10:01:00Z", 80)], "2026-10-10")
        # Та же точка измерения в другой записи того же источника — повтор, считается один раз.
        add_hr(conn, self.source, self.batch, "r2", "2026-10-10T10:01:00Z",
               [("2026-10-10T10:01:00Z", 80)], "2026-10-10")
        add_hr(conn, self.source, self.batch, "gone", "2026-10-10T11:00:00Z",
               [("2026-10-10T11:00:00Z", 200)], "2026-10-10", deleted_at="2026-10-11T00:00:00Z")
        [day] = health.daily_heart_rate(conn, "2026-10-10", "2026-10-10")
        self.assertEqual(day, {"date": "2026-10-10", "min_bpm": 60, "avg_bpm": 70.0, "max_bpm": 80, "samples": 2})
        self.assertEqual(health.counts(conn)["heart_rate"], 3)   # 4 строки измерений, одна в удалённой записи

    def test_sample_day_follows_local_time(self):
        conn = self.open()
        # Смещение +3 ч: 20:30Z — ещё 10.10, 21:30Z — уже 11.10 по местному времени.
        add_hr(conn, self.source, self.batch, "night", "2026-10-10T20:30:00Z",
               [("2026-10-10T20:30:00Z", 70), ("2026-10-10T21:30:00Z", 50)], "2026-10-10")
        days = health.daily_heart_rate(conn, "2026-10-01", "2026-10-31")
        self.assertEqual([(d["date"], d["min_bpm"], d["samples"]) for d in days],
                         [("2026-10-10", 70, 1), ("2026-10-11", 50, 1)])
        self.assertEqual(health.heart_rate_period(days)["avg_bpm"], 60.0)
        self.assertIsNone(health.heart_rate_period([]))


class SleepTests(HealthDbCase):
    def test_overnight_session_counts_on_wake_date(self):
        conn = self.open()
        # Местное время +3: 23:00 9 октября — 07:00 10 октября; дата сессии — дата пробуждения.
        add_sleep(conn, self.source, self.batch, hc_id="night", start="2026-10-09T20:00:00Z",
                  end="2026-10-10T04:00:00Z", local_date="2026-10-10",
                  stages=[("light", "2026-10-09T20:00:00Z", "2026-10-09T22:00:00Z"),
                          ("deep", "2026-10-09T22:00:00Z", "2026-10-09T23:00:00Z"),
                          ("awake", "2026-10-09T23:00:00Z", "2026-10-09T23:30:00Z"),
                          ("rem", "2026-10-09T23:30:00Z", "2026-10-10T04:30:00Z")])   # выходит за конец сессии
        add_sleep(conn, self.source, self.batch, hc_id="nap", start="2026-10-10T12:00:00Z",
                  end="2026-10-10T13:00:00Z", local_date="2026-10-10")
        # Дубликат части ночи от другого источника не увеличивает время сна.
        other = add_source(conn, key="health_connect|app.example.phone||||", package="app.example.phone")
        add_sleep(conn, other, self.batch, hc_id="dup", start="2026-10-09T21:00:00Z", end="2026-10-10T03:00:00Z",
                  local_date="2026-10-10")
        add_sleep(conn, self.source, self.batch, hc_id="gone", start="2026-10-10T14:00:00Z",
                  end="2026-10-10T18:00:00Z", local_date="2026-10-10", deleted_at="2026-10-11T00:00:00Z")
        self.assertEqual(health.daily_sleep(conn, "2026-10-09", "2026-10-09"), [])
        [day] = health.daily_sleep(conn, "2026-10-01", "2026-10-31")
        self.assertEqual(day["date"], "2026-10-10")
        self.assertEqual(day["sessions"], 3)
        self.assertEqual(day["staged_sessions"], 1)
        self.assertEqual(day["in_bed_min"], 540.0)                # 8 ч ночью + 1 ч днём
        self.assertEqual(day["asleep_min"], 450.0)                # 120 + 60 + 270 (REM обрезан концом сессии)
        self.assertEqual((day["deep_min"], day["light_min"], day["rem_min"], day["awake_min"]),
                         (60.0, 120.0, 270.0, 30.0))

    def test_session_without_stages(self):
        conn = self.open()
        add_sleep(conn, self.source, self.batch, hc_id="s", start="2026-10-10T00:00:00Z",
                  end="2026-10-10T06:15:00Z")
        [day] = health.daily_sleep(conn, "2026-10-10", "2026-10-10")
        self.assertEqual(day["in_bed_min"], 375.0)
        self.assertIsNone(day["asleep_min"])
        self.assertIsNone(day["deep_min"])


class PeriodTests(HealthDbCase):
    def test_functions_filter_by_dates(self):
        conn = self.open()
        self.fill_health(conn, day_offsets=(10, 100))
        recent, old = days_ago(10), days_ago(100)
        for fn in (health.daily_steps, health.daily_heart_rate, health.daily_sleep):
            with self.subTest(fn=fn.__name__):
                self.assertEqual([d["date"] for d in fn(conn, days_ago(29), days_ago(0))], [recent])
                self.assertEqual([d["date"] for d in fn(conn, days_ago(364), days_ago(0))], [old, recent])
                self.assertEqual(fn(conn, days_ago(9), days_ago(0)), [])

    def test_all_period_starts_at_earliest_health_record(self):
        conn = self.open()
        self.fill_health(conn, day_offsets=(400,))
        self.assertEqual(pages.period_bounds(conn, "all")[0], days_ago(400))
        self.assertEqual(health.earliest_date(conn), days_ago(400))

    def test_pages_follow_period(self):
        conn = self.open()
        self.fill_health(conn, day_offsets=(10, 100))
        conn.close()
        self.start()
        short = self.get_html("/health?period=30")
        self.assertIn(days_ago(10), short)
        self.assertNotIn(days_ago(100), short)
        year = self.get_html("/health?period=365")
        self.assertIn(days_ago(100), year)
        self.assertIn("3 090", year)     # 1200 + 345 за каждый из двух дней


# ---------- Страницы ----------

class HealthPageTests(HealthDbCase):
    def test_overview_with_only_health_data(self):
        conn = self.open()
        self.fill_health(conn)
        conn.close()
        self.start()
        html = self.get_html("/")
        self.assertNotIn("База пуста", html)
        self.assertIn("Health Connect", html)
        for title in ("<h2>Шаги</h2>", "<h2>Пульс, уд/мин</h2>", "<h2>Сон</h2>"):
            self.assertIn(title, html)
        self.assertIn("1 545", html)      # шаги последнего дня
        self.assertIn("61–99", html)           # пульс: минимум–максимум
        self.assertIn("7 ч 30 мин", html)      # сон последних суток
        self.assertIn("7 ч 00 мин", html)      # из них во сне по стадиям
        self.assertEqual(html.count("<svg"), 3)
        self.assertIn('href="/health?period=90"', html)

    def test_overview_with_regular_and_health_data(self):
        self.fill()
        conn = self.open()
        self.fill_health(conn)
        conn.close()
        self.start()
        html = self.get_html("/")
        self.assertIn("81.2 кг", html)
        self.assertIn("Health Connect", html)
        self.assertEqual(html.count("<svg"), 4)
        # Счётчики обычных записей не изменились.
        self.assertIn('<span class="muted">Замеры</span><b>2</b>', html)
        self.assertIn('<span class="muted">Заметки</span><b>1</b>', html)

    def test_regular_data_without_health(self):
        self.fill()
        self.start()
        html = self.get_html("/")
        self.assertNotIn("Health Connect", html)
        self.assertNotIn("<h2>Шаги</h2>", html)
        health_html = self.get_html("/health")
        self.assertEqual(health_html.count("Пока нет записей"), 3)

    def test_completely_empty_database(self):
        db.open_database(self.db_path).close()
        self.start()
        overview = self.get_html("/")
        self.assertIn("База пуста", overview)
        self.assertNotIn("Health Connect", overview)
        html = self.get_html("/health?period=all")
        self.assertEqual(html.count("Пока нет записей"), 3)
        self.assertNotIn("<svg", html)

    def test_only_some_health_types(self):
        conn = self.open()
        add_sleep(conn, self.source, self.batch, hc_id="s", start=f"{days_ago(1)}T00:00:00Z",
                  end=f"{days_ago(1)}T06:00:00Z", local_date=days_ago(1))
        conn.commit()
        conn.close()
        self.start()
        html = self.get_html("/health")
        self.assertEqual(html.count("Пока нет записей"), 2)
        self.assertIn("6 ч 00 мин", html)
        self.assertEqual(html.count("<svg"), 1)
        overview = self.get_html("/")
        self.assertNotIn("База пуста", overview)
        self.assertEqual(overview.count("Пока нет записей"), 2)

    def test_route_and_navigation(self):
        conn = self.open()
        self.fill_health(conn)
        conn.close()
        self.start()
        html = self.get_html("/health")
        self.assertIn('<a href="/health" class="active">Здоровье</a>', html)
        self.assertIn("<title>Здоровье — FITNESS Tracker</title>", html)
        self.assertIn('<a href="/health">Здоровье</a>', self.get_html("/workouts"))
        status, headers, body = self.request("/health", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(self.request("/health", method="POST")[0], 405)
        self.assertEqual(self.request("/health/x")[0], 404)
        for path in HEALTH_PAGES:
            page = self.get_html(path)
            self.assertNotIn("<script", page, path)
            self.assertNotIn("https://", page, path)

    def test_health_pages_do_not_change_database(self):
        self.fill()
        conn = self.open()
        self.fill_health(conn, day_offsets=(1, 2, 40))
        notes.add_note(conn, {"note_date": days_ago(1), "category": "тест", "text": "x"})
        conn.close()
        before = file_state(self.db_path)
        self.start()
        for path in HEALTH_PAGES:
            self.request(path)
            self.request(path, method="HEAD")
        self.request("/health", method="POST")
        self.server.shutdown()
        self.assertEqual(file_state(self.db_path), before)
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_queries_work_under_readonly_authorizer(self):
        conn = self.open()
        self.fill_health(conn)
        conn.close()
        with readonly_connection(self.db_path) as ro:
            self.assertEqual(health.counts(ro), {"steps": 4, "heart_rate": 4, "sleep": 2})
            self.assertEqual(len(health.daily_steps(ro, days_ago(30), days_ago(0))), 2)
            self.assertEqual(len(health.daily_heart_rate(ro, days_ago(30), days_ago(0))), 2)
            self.assertEqual(len(health.daily_sleep(ro, days_ago(30), days_ago(0))), 2)


class RangeChartTests(unittest.TestCase):
    def test_range_chart(self):
        self.assertEqual(charts.range_chart([], "t", "u"), "")
        svg = charts.range_chart([("2026-10-10", 50, 70, 120), ("2026-10-11", 55, 72.5, 110)], "Пульс", "уд/мин")
        self.assertEqual(svg.count('class="range"'), 2)
        self.assertIn("среднее 72.5 уд/мин, мин. 55, макс. 110", svg)


if __name__ == "__main__":
    unittest.main()
