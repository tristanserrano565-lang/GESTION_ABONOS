from __future__ import annotations

from datetime import datetime, timedelta
import unicodedata
from typing import Any, Dict, Optional, Tuple, Union

from . import config


def normalize_text(value: Optional[str]) -> str:
    return value.strip() if value else ""


def normalize_team_name(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFD", value)
    without_accents = "".join(
        ch for ch in normalized if unicodedata.category(ch) != "Mn"
    )
    return without_accents.lower().strip()


def normalize_datetime_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip().replace("T", " ").replace("Z", "")
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(cleaned)  + timedelta(hours=1) # Se le añade una hora para coger el horario español
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def combine_datetime(
    date_value: Optional[str], time_value: Optional[str]
) -> Optional[str]:
    if not date_value:
        return None
    time_component = time_value or "00:00:00"
    return normalize_datetime_value(f"{date_value} {time_component}")


def build_team_names(is_home: bool, rival: str) -> Tuple[str, str]:
    opponent = rival or "Pendiente"
    if is_home:
        return config.ATLETICO_TEAM_NAME, opponent
    return opponent, config.ATLETICO_TEAM_NAME


def format_abono(abono: Union[Dict[str, Any], Any, None]) -> str:
    if not abono:
        return "Abono"
    try:
        sector = abono["sector"]
        puerta = abono["puerta"]
        fila = abono["fila"]
        asiento = abono["asiento"]
    except (TypeError, KeyError):
        return "Abono"
    return f"Puerta {puerta} · Sector {sector} · Fila {fila} · Asiento {asiento}"


def format_parking(parking: Union[Dict[str, Any], Any, None]) -> str:
    if not parking:
        return "Parking"
    try:
        nombre = parking["nombre"]
    except (TypeError, KeyError):
        return "Parking"
    return f"Plaza {nombre}"


def human_datetime(value: Optional[str]) -> str:
    if not value:
        return "Sin confirmar"
    normalized = normalize_datetime_value(value)
    if not normalized:
        return value
    dt = datetime.fromisoformat(normalized)
    return dt.strftime("%d/%m/%Y %H:%M")


def simple_human_date(value: Optional[str]) -> str:
    if not value:
        return "--/--"
    normalized = normalize_datetime_value(value)
    if not normalized:
        return value
    dt = datetime.fromisoformat(normalized)
    return dt.strftime("%d/%m")


def competition_theme(
    competicion: Optional[str], is_home: Union[bool, int] = False
) -> Dict[str, str]:
    text = (competicion or "").lower()
    if "champ" in text:
        icon = "img/champions.svg"
        label = "Champions"
    elif "copa" in text:
        icon = "img/copa.svg"
        label = "Copa"
    else:
        icon = "img/laliga.svg"
        label = competicion or "Liga"

    return {
        "class": "match-card--home" if is_home else "match-card--away",
        "label": label,
        "icon": icon,
    }
