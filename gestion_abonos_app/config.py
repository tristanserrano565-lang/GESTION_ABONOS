from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
#DATABASE_PATH = BASE_DIR / "gestion_abonos.db"
DATABASE_URL = os.getenv("DATABASE_URL")

#LOGIN RATE-LIMITING
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 120    

SECRET_KEY = os.getenv("SECRET_KEY")
COOKIE_SECURE = os.getenv("COOKIE_SECURE").lower() == "true"

ATLETICO_TEAM_NAME = "Atleti"

# API-Football settings
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_HOST = "v3.football.api-sports.io"
API_FOOTBALL_TEAM_ID = 530  # Atlético de Madrid en API-Football
API_FOOTBALL_NEXT = int(os.getenv("API_FOOTBALL_NEXT", "10"))  # número de próximos partidos a traer
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "180")) #Intervalo de tiempo para llamar a la api
ENABLE_BG_SYNC = os.getenv("ENABLE_BG_SYNC", "true").lower() == "true"

DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME")
DEFAULT_ADMIN_HASH = os.getenv("DEFAULT_ADMIN_HASH")
DEFAULT_ADMIN_SALT = os.getenv("DEFAULT_ADMIN_SALT")

SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", "604800"))
POST_RATE_LIMIT_COUNT = int(os.getenv("POST_RATE_LIMIT_COUNT", "120"))
POST_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("POST_RATE_LIMIT_WINDOW_SECONDS", "60"))

DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
DB_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))

LOG_SLOW_QUERIES = os.getenv("LOG_SLOW_QUERIES", "true").lower() == "true"
SLOW_QUERY_THRESHOLD_MS = int(os.getenv("SLOW_QUERY_THRESHOLD_MS", "200"))
