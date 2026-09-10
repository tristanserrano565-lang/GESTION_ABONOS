import os
from pathlib import Path
import regex

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _env_csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    values = tuple(
        item.strip().rstrip("/")
        for item in raw_value.split(",")
        if item.strip()
    )
    return values or default


BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv("DATABASE_URL")

#LOGIN RATE-LIMITING
MAX_LOGIN_ATTEMPTS = _env_int("MAX_LOGIN_ATTEMPTS", 5)
LOGIN_WINDOW_SECONDS = _env_int("LOGIN_WINDOW_SECONDS", 120)

SECRET_KEY = (os.getenv("SECRET_KEY") or "").strip() or None
COOKIE_SECURE = _env_bool("COOKIE_SECURE", default=False)
FORCE_HTTPS = _env_bool("FORCE_HTTPS", default=False)
SESSION_COOKIE_SAMESITE = (os.getenv("SESSION_COOKIE_SAMESITE") or "Lax").strip() or "Lax"
ENABLE_SECURITY_HEADERS = _env_bool("ENABLE_SECURITY_HEADERS", default=True)
CSP_IMG_ALLOWLIST = _env_csv(
    "CSP_IMG_ALLOWLIST",
    default=("https://media.api-sports.io",),
)
PROXY_FIX_X_FOR = _env_int("PROXY_FIX_X_FOR", 0)
PROXY_FIX_X_PROTO = _env_int("PROXY_FIX_X_PROTO", 0)
PROXY_FIX_X_HOST = _env_int("PROXY_FIX_X_HOST", 0)
PROXY_FIX_X_PORT = _env_int("PROXY_FIX_X_PORT", 0)
PROXY_FIX_X_PREFIX = _env_int("PROXY_FIX_X_PREFIX", 0)

ATLETICO_TEAM_NAME = "Atleti"

# API-Football settings
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_HOST = "v3.football.api-sports.io"
API_FOOTBALL_TEAM_ID = 530  # Atlético de Madrid en API-Football
API_FOOTBALL_NEXT = _env_int("API_FOOTBALL_NEXT", 10)  # número de próximos partidos a traer
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

SYNC_INTERVAL_MINUTES = _env_int("SYNC_INTERVAL_MINUTES", 180) #Intervalo de tiempo para llamar a la api

DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME")
DEFAULT_ADMIN_HASH = os.getenv("DEFAULT_ADMIN_HASH")
DEFAULT_ADMIN_SALT = os.getenv("DEFAULT_ADMIN_SALT")

SESSION_MAX_AGE_SECONDS = _env_int("SESSION_MAX_AGE_SECONDS", 43200)
MAX_PDF_UPLOAD_BYTES = _env_int("MAX_PDF_UPLOAD_BYTES", 5 * 1024 * 1024)
MAX_PDF_BATCH_FILES = max(1, _env_int("MAX_PDF_BATCH_FILES", 20))
MAX_PDF_BATCH_BYTES = max(1, _env_int("MAX_PDF_BATCH_BYTES", 25 * 1024 * 1024))
POST_RATE_LIMIT_COUNT = _env_int("POST_RATE_LIMIT_COUNT", 120)
POST_RATE_LIMIT_WINDOW_SECONDS = _env_int("POST_RATE_LIMIT_WINDOW_SECONDS", 60)

N8N_WEBHOOK_URL = (os.getenv("N8N_WEBHOOK_URL") or "").strip()
N8N_WEBHOOK_TIMEOUT_SECONDS = _env_int("N8N_WEBHOOK_TIMEOUT_SECONDS", 20)
N8N_WEBHOOK_MAX_RETRIES = _env_int("N8N_WEBHOOK_MAX_RETRIES", 2)
N8N_WEBHOOK_RETRY_DELAY_SECONDS = _env_int("N8N_WEBHOOK_RETRY_DELAY_SECONDS", 3)
N8N_WEBHOOK_BEARER_TOKEN = (os.getenv("N8N_WEBHOOK_BEARER_TOKEN") or "").strip()
N8N_WEBHOOK_SECRET_HEADER = (
    os.getenv("N8N_WEBHOOK_SECRET_HEADER") or "X-Workflow-Token"
).strip()
N8N_WEBHOOK_SECRET = (os.getenv("N8N_WEBHOOK_SECRET") or "").strip()
EMAIL_DELIVERY_MAX_ATTEMPTS = _env_int("EMAIL_DELIVERY_MAX_ATTEMPTS", 3)
EMAIL_DELIVERY_PROCESSING_STALE_SECONDS = _env_int(
    "EMAIL_DELIVERY_PROCESSING_STALE_SECONDS",
    600,
)
EMAIL_DELIVERY_INTER_SEND_DELAY_SECONDS = _env_int(
    "EMAIL_DELIVERY_INTER_SEND_DELAY_SECONDS",
    2,
)

DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 10)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 1800)

LOG_SLOW_QUERIES = _env_bool("LOG_SLOW_QUERIES", default=True)
SLOW_QUERY_THRESHOLD_MS = _env_int("SLOW_QUERY_THRESHOLD_MS", 200)
