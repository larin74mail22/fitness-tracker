-- Миграция 001: исходная схема FITNESS Tracker.
-- Даты: TEXT 'YYYY-MM-DD'; время: TEXT 'HH:MM' (местное); служебные метки: UTC ISO 8601.
-- Неизвестные значения хранятся как NULL, а не 0.

-- ---------- Замеры тела ----------

CREATE TABLE measurement_types (
    code        TEXT PRIMARY KEY,
    name_ru     TEXT NOT NULL,
    unit        TEXT NOT NULL CHECK (unit IN ('cm', '%')),
    min_value   REAL NOT NULL,
    max_value   REAL NOT NULL
);

INSERT INTO measurement_types (code, name_ru, unit, min_value, max_value) VALUES
    ('waist',            'Талия',                  'cm', 30, 250),
    ('chest',            'Грудь',                  'cm', 40, 250),
    ('hips',             'Бёдра (ягодицы)',        'cm', 40, 250),
    ('neck',             'Шея',                    'cm', 20, 80),
    ('shoulders',        'Плечи (обхват)',         'cm', 60, 200),
    ('biceps_left',      'Бицепс левый',           'cm', 15, 70),
    ('biceps_right',     'Бицепс правый',          'cm', 15, 70),
    ('forearm_left',     'Предплечье левое',       'cm', 15, 60),
    ('forearm_right',    'Предплечье правое',      'cm', 15, 60),
    ('thigh_left',       'Бедро левое',            'cm', 25, 110),
    ('thigh_right',      'Бедро правое',           'cm', 25, 110),
    ('calf_left',        'Голень левая',           'cm', 20, 70),
    ('calf_right',       'Голень правая',          'cm', 20, 70),
    ('body_fat_percent', 'Процент жира',           '%',  2, 70);

CREATE TABLE measurement_sessions (
    id                INTEGER PRIMARY KEY,
    measured_on       TEXT NOT NULL CHECK (measured_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    measured_time     TEXT CHECK (measured_time IS NULL OR measured_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    weight_kg         REAL CHECK (weight_kg IS NULL OR (weight_kg > 0 AND weight_kg < 500)),
    notes             TEXT,
    client_request_id TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_measurement_sessions_date ON measurement_sessions (measured_on);

CREATE TABLE body_measurements (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES measurement_sessions (id) ON DELETE CASCADE,
    type_code   TEXT NOT NULL REFERENCES measurement_types (code),
    value       REAL NOT NULL CHECK (value > 0),
    UNIQUE (session_id, type_code)
);

CREATE TABLE photos (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES measurement_sessions (id) ON DELETE SET NULL,
    taken_on    TEXT NOT NULL CHECK (taken_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    file_path   TEXT NOT NULL UNIQUE,   -- относительно data/photos
    view        TEXT CHECK (view IS NULL OR view IN ('front', 'side', 'back', 'other')),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_photos_date ON photos (taken_on);

-- ---------- Тренировки ----------

CREATE TABLE workouts (
    id                INTEGER PRIMARY KEY,
    workout_date      TEXT NOT NULL CHECK (workout_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    started_time      TEXT CHECK (started_time IS NULL OR started_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    title             TEXT NOT NULL CHECK (length(title) > 0),
    workout_type      TEXT NOT NULL DEFAULT 'other'
                      CHECK (workout_type IN ('strength', 'cardio', 'mobility', 'sport', 'other')),
    duration_min      REAL CHECK (duration_min IS NULL OR (duration_min > 0 AND duration_min <= 1440)),
    notes             TEXT,
    client_request_id TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_workouts_date ON workouts (workout_date);

CREATE TABLE exercises (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL UNIQUE,   -- нормализованное имя: нижний регистр, одиночные пробелы
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE workout_exercises (
    id           INTEGER PRIMARY KEY,
    workout_id   INTEGER NOT NULL REFERENCES workouts (id) ON DELETE CASCADE,
    exercise_id  INTEGER NOT NULL REFERENCES exercises (id),
    position     INTEGER NOT NULL CHECK (position >= 1),
    notes        TEXT,
    discomfort   TEXT,
    UNIQUE (workout_id, position)
);
CREATE INDEX idx_workout_exercises_exercise ON workout_exercises (exercise_id);

CREATE TABLE exercise_sets (
    id                   INTEGER PRIMARY KEY,
    workout_exercise_id  INTEGER NOT NULL REFERENCES workout_exercises (id) ON DELETE CASCADE,
    set_number           INTEGER NOT NULL CHECK (set_number >= 1),
    weight_kg            REAL CHECK (weight_kg IS NULL OR (weight_kg >= 0 AND weight_kg <= 1000)),
    reps                 INTEGER CHECK (reps IS NULL OR (reps >= 0 AND reps <= 1000)),
    rir                  REAL CHECK (rir IS NULL OR (rir >= 0 AND rir <= 10)),
    duration_sec         REAL CHECK (duration_sec IS NULL OR (duration_sec > 0 AND duration_sec <= 86400)),
    distance_m           REAL CHECK (distance_m IS NULL OR (distance_m > 0 AND distance_m <= 500000)),
    is_warmup            INTEGER NOT NULL DEFAULT 0 CHECK (is_warmup IN (0, 1)),
    notes                TEXT,
    UNIQUE (workout_exercise_id, set_number),
    CHECK (reps IS NOT NULL OR duration_sec IS NOT NULL OR distance_m IS NOT NULL)
);

-- ---------- Питание ----------

CREATE TABLE meals (
    id                INTEGER PRIMARY KEY,
    eaten_on          TEXT NOT NULL CHECK (eaten_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    eaten_time        TEXT CHECK (eaten_time IS NULL OR eaten_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    meal_type         TEXT CHECK (meal_type IS NULL OR meal_type IN ('breakfast', 'lunch', 'dinner', 'snack', 'other')),
    notes             TEXT,
    client_request_id TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_meals_date ON meals (eaten_on);

CREATE TABLE meal_items (
    id                INTEGER PRIMARY KEY,
    meal_id           INTEGER NOT NULL REFERENCES meals (id) ON DELETE CASCADE,
    position          INTEGER NOT NULL CHECK (position >= 1),
    name              TEXT NOT NULL CHECK (length(name) > 0),
    quantity          REAL CHECK (quantity IS NULL OR quantity > 0),
    unit              TEXT CHECK (unit IS NULL OR unit IN ('g', 'ml', 'pcs', 'serving')),
    kcal              REAL CHECK (kcal IS NULL OR kcal >= 0),
    protein_g         REAL CHECK (protein_g IS NULL OR protein_g >= 0),
    fat_g             REAL CHECK (fat_g IS NULL OR fat_g >= 0),
    carbs_g           REAL CHECK (carbs_g IS NULL OR carbs_g >= 0),
    nutrition_source  TEXT CHECK (nutrition_source IS NULL OR
                                  nutrition_source IN ('label', 'database', 'recipe', 'user', 'estimate')),
    is_estimate       INTEGER NOT NULL DEFAULT 1 CHECK (is_estimate IN (0, 1)),
    UNIQUE (meal_id, position),
    CHECK ((quantity IS NULL) = (unit IS NULL))
);

-- ---------- Алкоголь ----------

CREATE TABLE alcohol_entries (
    id                INTEGER PRIMARY KEY,
    consumed_on       TEXT NOT NULL CHECK (consumed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    consumed_time     TEXT CHECK (consumed_time IS NULL OR consumed_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    drink             TEXT NOT NULL CHECK (length(drink) > 0),
    volume_ml         REAL CHECK (volume_ml IS NULL OR (volume_ml > 0 AND volume_ml <= 10000)),
    abv_percent       REAL CHECK (abv_percent IS NULL OR (abv_percent > 0 AND abv_percent <= 100)),
    ethanol_g         REAL CHECK (ethanol_g IS NULL OR ethanol_g >= 0),   -- только если известны объём и крепость
    kcal              REAL CHECK (kcal IS NULL OR kcal >= 0),
    kcal_source       TEXT CHECK (kcal_source IS NULL OR kcal_source IN ('user', 'label', 'ethanol_only_estimate')),
    kcal_is_estimate  INTEGER CHECK (kcal_is_estimate IS NULL OR kcal_is_estimate IN (0, 1)),
    notes             TEXT,
    client_request_id TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((kcal IS NULL) = (kcal_source IS NULL))
);
CREATE INDEX idx_alcohol_date ON alcohol_entries (consumed_on);

-- ---------- Заметки ----------

CREATE TABLE notes (
    id                INTEGER PRIMARY KEY,
    note_date         TEXT NOT NULL CHECK (note_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    category          TEXT NOT NULL CHECK (length(category) > 0),
    text              TEXT NOT NULL CHECK (length(text) > 0),
    client_request_id TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_notes_date ON notes (note_date);
CREATE INDEX idx_notes_category ON notes (category);

CREATE TABLE note_fields (
    id          INTEGER PRIMARY KEY,
    note_id     INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    key         TEXT NOT NULL CHECK (length(key) > 0),
    value_text  TEXT,
    value_num   REAL,
    unit        TEXT,
    UNIQUE (note_id, key),
    CHECK ((value_text IS NULL) <> (value_num IS NULL))
);

-- ---------- Автообновление updated_at ----------

CREATE TRIGGER trg_measurement_sessions_updated AFTER UPDATE ON measurement_sessions
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE measurement_sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_photos_updated AFTER UPDATE ON photos
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE photos SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_workouts_updated AFTER UPDATE ON workouts
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE workouts SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_meals_updated AFTER UPDATE ON meals
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE meals SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_alcohol_entries_updated AFTER UPDATE ON alcohol_entries
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE alcohol_entries SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_notes_updated AFTER UPDATE ON notes
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE notes SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;
