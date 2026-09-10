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


def _count_events(scope: str, bucket_key: str) -> Tuple[int, Optional[int]]:
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total, MIN(created_at) AS oldest
            FROM rate_limit_events
            WHERE scope = ? AND bucket_key = ?
            """,
            (scope, bucket_key),
        ).fetchone()
        conn.close()
    except Exception:
        conn.close()
        raise

    total = int(row["total"] or 0) if row else 0
    oldest = int(row["oldest"]) if row and row["oldest"] is not None else None
    return total, oldest


def prune_expired(scope: str, window_seconds: int, now_ts: Optional[int] = None) -> None:
    current_ts = _now_ts(now_ts)
    cutoff = _window_cutoff(current_ts, window_seconds)
    conn = db.get_connection()
    try:
        conn.execute(
            """
            DELETE FROM rate_limit_events
            WHERE scope = ? AND created_at < ?
            """,
            (scope, cutoff),
        )
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
    prune_expired(scope, window_seconds, now_ts=current_ts)
    total, oldest = _count_events(scope, bucket_key)
    if total < max_attempts or oldest is None:
        return False, None

    wait_seconds = max(1, window_seconds - max(0, current_ts - oldest))
    return True, wait_seconds


def record_event(scope: str, bucket_key: str, now_ts: Optional[int] = None) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO rate_limit_events (scope, bucket_key, created_at)
            VALUES (?, ?, ?)
            """,
            (scope, bucket_key, _now_ts(now_ts)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_events(scope: str, bucket_key: str) -> None:
    conn = db.get_connection()
    try:
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
    prune_expired(scope, window_seconds, now_ts=current_ts)
    total, oldest = _count_events(scope, bucket_key)
    if total >= max_attempts and oldest is not None:
        wait_seconds = max(1, window_seconds - max(0, current_ts - oldest))
        return True, wait_seconds

    record_event(scope, bucket_key, now_ts=current_ts)
    return False, None
