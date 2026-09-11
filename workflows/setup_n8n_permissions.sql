-- Ejecutar con psql -X, como administrador, conectado a la BD de la app.
-- Alta unica: si el usuario ya existe, aborta sin cambiarlo ni rotar su clave.
\set ON_ERROR_STOP on
BEGIN;
CREATE ROLE n8n_mailer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;

\ir n8n_delivery_ledger.sql

SELECT format('GRANT CONNECT ON DATABASE %I TO n8n_mailer', current_database()) \gexec
GRANT USAGE ON SCHEMA n8n_delivery TO n8n_mailer;
GRANT SELECT, INSERT, UPDATE ON n8n_delivery.receipts TO n8n_mailer;
ALTER ROLE n8n_mailer SET search_path = pg_catalog, n8n_delivery;

-- PUBLIC tambien se aplica a roles NOINHERIT. Rechazar permisos amplios
-- sin revocarlos a otros usuarios ni modificar las tablas de la app.
DO $$
BEGIN
    IF has_database_privilege('n8n_mailer', current_database(), 'CREATE')
       OR EXISTS (
           SELECT 1 FROM pg_namespace
           WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
             AND has_schema_privilege('n8n_mailer', oid, 'CREATE')
       ) THEN
        RAISE EXCEPTION 'PUBLIC permite crear objetos. Revisar permisos antes del alta.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND c.oid <> 'n8n_delivery.receipts'::regclass
          AND (has_table_privilege('n8n_mailer', c.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR has_any_column_privilege('n8n_mailer', c.oid,
                   'SELECT,INSERT,UPDATE,REFERENCES'))
    ) OR has_table_privilege('n8n_mailer', 'n8n_delivery.receipts',
                             'DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
        RAISE EXCEPTION 'PUBLIC da permisos adicionales sobre tablas. Revisar antes del alta.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname !~ '^pg_' AND c.relkind = 'S'
          AND CASE WHEN c.relkind = 'S' THEN
              has_sequence_privilege('n8n_mailer', c.oid, 'USAGE,SELECT,UPDATE')
              ELSE false END
    ) OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
          AND p.prosecdef AND has_function_privilege('n8n_mailer', p.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Hay secuencias o funciones privilegiadas accesibles. Revisar antes del alta.';
    END IF;
END $$;

-- psql solicita la clave sin mostrarla ni enviarla en texto claro al servidor.
SET LOCAL password_encryption = 'scram-sha-256';
\password n8n_mailer
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_authid
                   WHERE rolname = 'n8n_mailer' AND rolpassword IS NOT NULL) THEN
        RAISE EXCEPTION 'No se ha configurado una clave. Se cancela el alta.';
    END IF;
END $$;
ALTER ROLE n8n_mailer LOGIN;
COMMIT;
\echo Usuario n8n_mailer y registro de envios preparados en la base seleccionada.
