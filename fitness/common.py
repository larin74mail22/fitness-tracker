"""Общие помощники для операций с базой."""

# Имена таблиц только из этого списка — никогда из аргументов пользователя.
REQUEST_ID_TABLES = {"measurement_sessions", "workouts", "meals", "alcohol_entries", "notes"}


def existing_by_request_id(conn, table, request_id):
    """id записи с тем же client_request_id или None."""
    if request_id is None:
        return None
    if table not in REQUEST_ID_TABLES:
        raise ValueError(f"unexpected table {table}")
    row = conn.execute(f"SELECT id FROM {table} WHERE client_request_id = ?", (request_id,)).fetchone()
    return row[0] if row else None


def row_to_dict(row, drop=("client_request_id",)):
    return {k: row[k] for k in row.keys() if k not in drop}


def round_or_none(value, digits=1):
    return None if value is None else round(value, digits)
