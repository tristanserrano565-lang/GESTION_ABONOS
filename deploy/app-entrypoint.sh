#!/bin/sh
set -eu

read_secret() {
    secret_path="$1"
    if [ ! -s "$secret_path" ]; then
        echo "Falta el secreto requerido: $secret_path" >&2
        exit 1
    fi
    tr -d '\r\n' < "$secret_path"
}

database_password="$(read_secret /run/secrets/app_db_password)"
export DATABASE_URL="postgresql+psycopg2://${DATABASE_USER}:${database_password}@${DATABASE_HOST}:5432/${DATABASE_NAME}"
export SECRET_KEY="$(read_secret /run/secrets/flask_secret_key)"
export N8N_WEBHOOK_SECRET="$(read_secret /run/secrets/n8n_webhook_secret)"
export DEFAULT_ADMIN_HASH="$(read_secret /run/secrets/default_admin_hash)"
export DEFAULT_ADMIN_SALT="$(read_secret /run/secrets/default_admin_salt)"

if [ -n "${API_FOOTBALL_KEY_FILE:-}" ] && [ -s "$API_FOOTBALL_KEY_FILE" ]; then
    export API_FOOTBALL_KEY="$(read_secret "$API_FOOTBALL_KEY_FILE")"
fi

ca_wait=0
while [ ! -s "${N8N_CA_CERT_FILE}" ] && [ "$ca_wait" -lt 60 ]; do
    ca_wait=$((ca_wait + 1))
    sleep 1
done
if [ ! -s "${N8N_CA_CERT_FILE}" ]; then
    echo "Caddy no ha generado la CA interna en ${N8N_CA_CERT_FILE}." >&2
    exit 1
fi

exec gunicorn app:app \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 2 \
    --timeout 120 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile -
