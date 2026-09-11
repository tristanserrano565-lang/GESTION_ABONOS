#!/bin/sh
set -eu

read_hex_secret() {
    secret_path="$1"
    secret_value="$(tr -d '\r\n' < "$secret_path")"
    case "$secret_value" in
        ''|*[!0-9a-f]*)
            echo "El secreto $secret_path debe ser hexadecimal." >&2
            exit 1
            ;;
    esac
    if [ "${#secret_value}" -lt 32 ]; then
        echo "El secreto $secret_path es demasiado corto." >&2
        exit 1
    fi
    printf '%s' "$secret_value"
}

app_password="$(read_hex_secret /run/secrets/app_db_password)"
n8n_password="$(read_hex_secret /run/secrets/n8n_db_password)"
mailer_password="$(read_hex_secret /run/secrets/n8n_mailer_password)"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    SET password_encryption = 'scram-sha-256';
    CREATE ROLE gestion_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '${app_password}';
    CREATE ROLE n8n_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '${n8n_password}';
    CREATE ROLE n8n_mailer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '${mailer_password}';
    CREATE DATABASE gestion_abonos OWNER gestion_app;
    CREATE DATABASE n8n OWNER n8n_owner;
    REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
    REVOKE CONNECT, TEMPORARY ON DATABASE gestion_abonos FROM PUBLIC;
    REVOKE CONNECT, TEMPORARY ON DATABASE n8n FROM PUBLIC;
    GRANT CONNECT, TEMPORARY ON DATABASE gestion_abonos TO gestion_app;
    GRANT CONNECT ON DATABASE gestion_abonos TO n8n_mailer;
    GRANT CONNECT, TEMPORARY ON DATABASE n8n TO n8n_owner;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname gestion_abonos <<-'SQL'
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    GRANT USAGE, CREATE ON SCHEMA public TO gestion_app;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname gestion_abonos \
    --file /opt/bootstrap/n8n_delivery_ledger.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname gestion_abonos <<-'SQL'
    REVOKE ALL ON SCHEMA n8n_delivery FROM PUBLIC;
    REVOKE ALL ON n8n_delivery.receipts FROM PUBLIC;
    GRANT USAGE ON SCHEMA n8n_delivery TO n8n_mailer;
    GRANT SELECT, INSERT, UPDATE ON n8n_delivery.receipts TO n8n_mailer;
    ALTER ROLE n8n_mailer SET search_path = pg_catalog, n8n_delivery;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname n8n <<-'SQL'
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    GRANT USAGE, CREATE ON SCHEMA public TO n8n_owner;
SQL
