from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import requests
from flask import current_app

from .. import config, db, utils

_last_sync: Optional[datetime] = None


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


def sync_upcoming_matches(force: bool = False) -> bool:
    global _last_sync
    now = datetime.now(timezone.utc)
    if (
        not force
        and _last_sync is not None
        and now - _last_sync < timedelta(minutes=config.SYNC_INTERVAL_MINUTES)
    ):
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
        _last_sync = now
        return False

    conn = db.get_connection()
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

    conn.commit()
    conn.close()
    _last_sync = now
    return updated
