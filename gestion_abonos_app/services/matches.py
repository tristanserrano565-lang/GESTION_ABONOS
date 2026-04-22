from __future__ import annotations

import json
import time
from typing import Dict, Optional

import requests
from flask import current_app

from .. import config, db, utils

SYNC_STATE_NAME = "matches_api_football"
POSTGRES_SYNC_LOCK_KEY = 530001


def _fetch_fixtures(**params) -> list[Dict]:
    headers = {"accept": "application/json"}
    if config.API_FOOTBALL_KEY:
        headers.update(
            {
                "x-apisports-key": config.API_FOOTBALL_KEY,
                "x-rapidapi-host": config.API_FOOTBALL_HOST,
            }
        )
    else:
        current_app.logger.error(
            "API_FOOTBALL_KEY no está configurada; no se puede sincronizar fixtures."
        )
        return []

    try:
        response = requests.get(
            f"{config.API_FOOTBALL_BASE}/fixtures",
            params=params,
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning("No se pudo sincronizar con fixtures: %s", exc)
        return []

    payload = response.json() or {}
    try:
        dump_path = config.BASE_DIR / "api_events_dump.json"
        with dump_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        current_app.logger.warning("No se pudo volcar el JSON de fixtures: %s", exc)

    fixtures = payload.get("response") or []
    if not isinstance(fixtures, list):
        current_app.logger.warning("Respuesta inesperada de fixtures: %r", payload)
        return []
    return fixtures


def _sync_interval_seconds() -> int:
    return max(config.SYNC_INTERVAL_MINUTES, 1) * 60


def _get_sync_state(conn) -> Dict[str, Optional[int]]:
    row = conn.execute(
        """
        SELECT last_checked_at, last_synced_at
        FROM service_sync_state
        WHERE name = ?
        """,
        (SYNC_STATE_NAME,),
    ).fetchone()
    if not row:
        return {"last_checked_at": None, "last_synced_at": None}
    return {
        "last_checked_at": (
            int(row["last_checked_at"]) if row["last_checked_at"] is not None else None
        ),
        "last_synced_at": (
            int(row["last_synced_at"]) if row["last_synced_at"] is not None else None
        ),
    }


def _recently_checked(state: Dict[str, Optional[int]], now_ts: int) -> bool:
    last_checked_at = state.get("last_checked_at")
    if last_checked_at is None:
        return False
    return now_ts - last_checked_at < _sync_interval_seconds()


def _update_sync_state(
    conn,
    *,
    checked_at: int,
    synced_at: Optional[int] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO service_sync_state (name, last_checked_at, last_synced_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_checked_at = excluded.last_checked_at,
            last_synced_at = COALESCE(excluded.last_synced_at, service_sync_state.last_synced_at)
        """,
        (SYNC_STATE_NAME, checked_at, synced_at),
    )


def _using_postgres() -> bool:
    return db.engine.dialect.name == "postgresql"


def _acquire_sync_lock(conn) -> bool:
    if not _using_postgres():
        return True
    row = conn.execute(
        "SELECT pg_try_advisory_lock(?) AS locked",
        (POSTGRES_SYNC_LOCK_KEY,),
    ).fetchone()
    return bool(row and row["locked"])


def _release_sync_lock(conn) -> None:
    if not _using_postgres():
        return
    conn.execute(
        "SELECT pg_advisory_unlock(?)",
        (POSTGRES_SYNC_LOCK_KEY,),
    )


def sync_upcoming_matches(force: bool = False) -> bool:
    now_ts = int(time.time())
    conn = db.get_connection()
    lock_acquired = False
    try:
        state = _get_sync_state(conn)
        if not force and _recently_checked(state, now_ts):
            return False

        if not _acquire_sync_lock(conn):
            current_app.logger.debug("[sync] otro worker ya está sincronizando partidos.")
            return False
        lock_acquired = True

        state = _get_sync_state(conn)
        if not force and _recently_checked(state, now_ts):
            return False

        fixtures = _fetch_fixtures(
            team=config.API_FOOTBALL_TEAM_ID,
            next=config.API_FOOTBALL_NEXT,
        )
        current_app.logger.info(
            "[sync] fixtures fetched %s items for team=%s",
            len(fixtures),
            config.API_FOOTBALL_TEAM_ID,
        )
        if not fixtures:
            _update_sync_state(conn, checked_at=now_ts)
            conn.commit()
            return False

        updated = False

        for fixture in fixtures:
            fixture_info = fixture.get("fixture") or {}
            league_info = fixture.get("league") or {}
            teams_info = fixture.get("teams") or {}
            home_info = teams_info.get("home") or {}
            away_info = teams_info.get("away") or {}

            api_id = str(fixture_info.get("id") or "")
            if not api_id:
                continue

            id_home = str(home_info.get("id") or "")
            id_away = str(away_info.get("id") or "")
            home_team = home_info.get("name") or ""
            away_team = away_info.get("name") or ""
            logo_home = home_info.get("logo")
            logo_away = away_info.get("logo")

            is_home = id_home == str(config.API_FOOTBALL_TEAM_ID)
            if not is_home and id_away != str(config.API_FOOTBALL_TEAM_ID):
                normalized_team = utils.normalize_team_name(config.ATLETICO_TEAM_NAME)
                if (
                    utils.normalize_team_name(home_team) != normalized_team
                    and utils.normalize_team_name(away_team) != normalized_team
                ):
                    current_app.logger.debug("Fixture descartado por nombres: %r", fixture)
                    continue
                is_home = utils.normalize_team_name(home_team) == normalized_team

            rival = away_team if is_home else home_team

            fecha_raw = fixture_info.get("date") or fixture_info.get("timestamp")
            fecha = utils.normalize_datetime_value(str(fecha_raw)) if fecha_raw else None

            estadio = None
            venue_info = fixture_info.get("venue") or {}
            if isinstance(venue_info, dict):
                estadio = venue_info.get("name")

            competicion = league_info.get("name")
            jornada = league_info.get("round")
            try:
                if isinstance(jornada, str):
                    parts = [int(p) for p in jornada.split() if p.isdigit()]
                    jornada = parts[0] if parts else None
                else:
                    jornada = int(jornada) if jornada is not None else None
            except (TypeError, ValueError):
                jornada = None

            equipo_local, equipo_visitante = (
                (config.ATLETICO_TEAM_NAME, rival)
                if is_home
                else (rival, config.ATLETICO_TEAM_NAME)
            )

            conn.execute(
                """
                INSERT INTO partidos (
                    jornada,
                    rival,
                    fecha,
                    localia,
                    competicion,
                    api_id,
                    estadio,
                    equipo_local,
                    equipo_visitante,
                    logo_local,
                    logo_visitante
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(api_id) DO UPDATE SET
                    jornada=excluded.jornada,
                    rival=excluded.rival,
                    fecha=excluded.fecha,
                    localia=excluded.localia,
                    competicion=excluded.competicion,
                    estadio=excluded.estadio,
                    equipo_local=excluded.equipo_local,
                    equipo_visitante=excluded.equipo_visitante,
                    logo_local=excluded.logo_local,
                    logo_visitante=excluded.logo_visitante
                """,
                (
                    jornada,
                    rival,
                    fecha,
                    1 if is_home else 0,
                    competicion,
                    api_id,
                    estadio,
                    equipo_local,
                    equipo_visitante,
                    logo_home,
                    logo_away,
                ),
            )
            updated = True

        _update_sync_state(
            conn,
            checked_at=now_ts,
            synced_at=now_ts,
        )
        conn.commit()
        return updated
    finally:
        if lock_acquired:
            try:
                _release_sync_lock(conn)
            except Exception:
                current_app.logger.warning(
                    "[sync] no se pudo liberar el lock de sincronización.",
                    exc_info=True,
                )
        conn.close()
