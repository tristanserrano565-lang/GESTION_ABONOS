from __future__ import annotations

from .. import db

PARTIDO_OPERATION_LOCK_SCOPE = 91021


def _using_postgres() -> bool:
    return db.engine.dialect.name == "postgresql"


def try_acquire_partido_operation_lock(conn, partido_id: int) -> bool:
    if not _using_postgres():
        return True
    row = conn.execute(
        "SELECT pg_try_advisory_lock(?, ?) AS locked",
        (PARTIDO_OPERATION_LOCK_SCOPE, int(partido_id)),
    ).fetchone()
    return bool(row and row["locked"])


def release_partido_operation_lock(conn, partido_id: int) -> None:
    if not _using_postgres():
        return
    conn.execute(
        "SELECT pg_advisory_unlock(?, ?)",
        (PARTIDO_OPERATION_LOCK_SCOPE, int(partido_id)),
    )
