from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = BASE_DIR / "gestion_abonos.db"

#LOGIN RATE-LIMITING
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 120    

SECRET_KEY = os.getenv("API_FOOTBALL_KEY")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

ATLETICO_TEAM_NAME = "Atleti"

# API-Football settings
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_HOST = "v3.football.api-sports.io"
API_FOOTBALL_TEAM_ID = 530  # Atlético de Madrid en API-Football
API_FOOTBALL_NEXT = 10  # número de próximos partidos a traer
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

SYNC_INTERVAL_MINUTES = 180 #Intervalo de tiempo para llamar a la api

DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME")
DEFAULT_ADMIN_HASH = os.getenv("DEFAULT_ADMIN_HASH")
DEFAULT_ADMIN_SALT = os.getenv("DEFAULT_ADMIN_SALT")

SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS"))
POST_RATE_LIMIT_COUNT = int(os.getenv("POST_RATE_LIMIT_COUNT"))
POST_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("POST_RATE_LIMIT_WINDOW_SECONDS"))
