from __future__ import annotations

import time
from typing import Optional, Tuple

from .. import db

LOGIN_FAILURE_SCOPE = "login_failure"
LOGIN_FAILURE_IP_SCOPE = "login_failure_ip"
POST_REQUEST_SCOPE = "post_request"


def _now_ts(now_ts: Optional[int] = None) -> int:
    if now_ts is not None:
        return int(now_ts)
    return int(time.time())


def _window_cutoff(now_ts: int, window_seconds: int) -> int:
    return max(0, now_ts - max(window_seconds, 1))


def _lock_bucket(conn, scope: str, bucket_key: str) -> None:
    """Serializa el contador de un origen en PostgreSQL durante la transacción."""
    if db.engine.dialect.name == "postgresql":
        lock_key = f"{len(scope)}:{scope}{len(bucket_key)}:{bucket_key}"
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (lock_key,),
        )


def _count_events(conn, scope: str, bucket_key: str) -> Tuple[int, Optional[int]]:
    row = conn.execute(
            """
            SELECT COUNT(*) AS total, MIN(created_at) AS oldest
            FROM rate_limit_events
            WHERE scope = ? AND bucket_key = ?
            """,
            (scope, bucket_key),
        ).fetchone()
    total = int(row["total"] or 0) if row else 0
    oldest = int(row["oldest"]) if row and row["oldest"] is not None else None
    return total, oldest


def _prune_expired(conn, scope: str, cutoff: int) -> None:
    conn.execute(
        """
        DELETE FROM rate_limit_events
        WHERE scope = ? AND created_at < ?
        """,
        (scope, cutoff),
    )


def _prune_bucket_expired(conn, scope: str, bucket_key: str, cutoff: int) -> None:
    conn.execute(
        """
        DELETE FROM rate_limit_events
        WHERE scope = ? AND bucket_key = ? AND created_at < ?
        """,
        (scope, bucket_key, cutoff),
    )


def prune_expired(scope: str, window_seconds: int, now_ts: Optional[int] = None) -> None:
    current_ts = _now_ts(now_ts)
    cutoff = _window_cutoff(current_ts, window_seconds)
    conn = db.get_connection()
    try:
        _prune_expired(conn, scope, cutoff)
        conn.commit()
    finally:
        conn.close()


def check_limit(
    scope: str,
    bucket_key: str,
    max_attempts: int,
    window_seconds: int,
    now_ts: Optional[int] = None,
) -> Tuple[bool, Optional[int]]:
    if max_attempts <= 0 or window_seconds <= 0:
        return False, None

    current_ts = _now_ts(now_ts)
    conn = db.get_connection()
    try:
        _lock_bucket(conn, scope, bucket_key)
        _prune_bucket_expired(
            conn, scope, bucket_key, _window_cutoff(current_ts, window_seconds)
        )
        total, oldest = _count_events(conn, scope, bucket_key)
        conn.commit()
    finally:
        conn.close()
    if total >= max_attempts and oldest is not None:
        return True, max(1, window_seconds - max(0, current_ts - oldest))
    return False, None


def clear_events(scope: str, bucket_key: str) -> None:
    conn = db.get_connection()
    try:
        _lock_bucket(conn, scope, bucket_key)
        conn.execute(
            """
            DELETE FROM rate_limit_events
            WHERE scope = ? AND bucket_key = ?
            """,
            (scope, bucket_key),
        )
        conn.commit()
    finally:
        conn.close()


def consume_limit(
    scope: str,
    bucket_key: str,
    max_attempts: int,
    window_seconds: int,
    now_ts: Optional[int] = None,
) -> Tuple[bool, Optional[int]]:
    if max_attempts <= 0 or window_seconds <= 0:
        return False, None

    current_ts = _now_ts(now_ts)
    conn = db.get_connection()
    try:
        _lock_bucket(conn, scope, bucket_key)
        _prune_bucket_expired(
            conn, scope, bucket_key, _window_cutoff(current_ts, window_seconds)
        )
        total, oldest = _count_events(conn, scope, bucket_key)
        if total >= max_attempts and oldest is not None:
            conn.commit()
            wait_seconds = max(1, window_seconds - max(0, current_ts - oldest))
            return True, wait_seconds
        conn.execute(
            """
            INSERT INTO rate_limit_events (scope, bucket_key, created_at)
            VALUES (?, ?, ?)
            """,
            (scope, bucket_key, current_ts),
        )
        conn.commit()
        return False, None
    finally:
        conn.close()
