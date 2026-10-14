"""Ошибки трекера. Тексты — на русском, чтобы ChatGPT мог пересказать их пользователю."""


class TrackerError(Exception):
    """Базовая ошибка трекера."""

    code = "tracker_error"

    def to_dict(self):
        return {"error": self.code, "message": str(self)}


class ValidationError(TrackerError):
    """Неверные или неоднозначные входные данные. Нужно уточнить у пользователя."""

    code = "validation_error"

    def __init__(self, field, message):
        super().__init__(f"{field}: {message}" if field else message)
        self.field = field

    def to_dict(self):
        data = super().to_dict()
        data["field"] = self.field
        return data


class NotFoundError(TrackerError):
    code = "not_found"


class DatabaseMissingError(TrackerError):
    code = "database_missing"


class ConfirmationError(TrackerError):
    """Опасная операция без правильного подтверждения."""

    code = "confirmation_required"


class MigrationPendingError(TrackerError):
    """Схема существующей базы устарела. Обычные вызовы её не обновляют: нужна явная команда migrate_db.py."""

    code = "migration_pending"


class MigrationError(TrackerError):
    """Обновление схемы не удалось или не прошло проверки. Сообщение содержит путь к резервной копии."""

    code = "migration_failed"


class BackupError(TrackerError):
    """Резервная копия перед удалением не создана или не прошла проверку. Удаление не выполнено."""

    code = "backup_failed"

    def __init__(self, message, backup=None):
        super().__init__(message)
        self.backup = backup

    def to_dict(self):
        data = super().to_dict()
        data["backup"] = str(self.backup) if self.backup is not None else None
        return data


class PhotoFileError(TrackerError):
    """Файл фото или корзины отсутствует, лежит вне разрешённой папки или не перенесён. Записи не изменены."""

    code = "photo_file_error"


class ImportFileError(TrackerError):
    """Файл выгрузки Health Connect не прошёл проверку. Ничего не импортировано.

    problems — список (путь в файле, сообщение), total — сколько всего найдено (в списке не больше предела),
    header — разобранный заголовок, если он корректен (нужен для записи неудачи в журнал), rejected — число
    плохих записей по типам.
    """

    code = "import_file_invalid"

    def __init__(self, problems, total=None, header=None, rejected=None):
        self.problems = list(problems)
        self.total = total if total is not None else len(self.problems)
        self.header = header
        self.rejected = rejected or {}
        lines = [f"{path}: {message}" for path, message in self.problems]
        if self.total > len(self.problems):
            lines.append(f"... и ещё проблем: {self.total - len(self.problems)}")
        super().__init__("Файл выгрузки не прошёл проверку, ничего не импортировано.\n  " + "\n  ".join(lines))

    def to_dict(self):
        data = super().to_dict()
        data["problems"] = [{"path": p, "message": m} for p, m in self.problems]
        return data


class ImportOrderError(TrackerError):
    """Нарушен порядок файлов от телефона (пропуск или более старый файл). Ничего не импортировано.

    kind: "gap" (пропущены номера), "older" (файл старше принятого), "taken" (номер уже принят с другим
    содержимым). details — сведения для автоматического импорта и ChatGPT (номера, непокрытый период)."""

    code = "import_order"

    def __init__(self, message, kind=None, **details):
        super().__init__(message)
        self.kind = kind
        self.details = details

    def to_dict(self):
        data = super().to_dict()
        if self.kind:
            data["order_problem"] = self.kind
            data.update(self.details)
        return data
