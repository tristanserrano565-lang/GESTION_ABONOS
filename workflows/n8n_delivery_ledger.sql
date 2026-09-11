-- Ejecutar en una BD PostgreSQL separada o en un esquema dedicado.
-- La credencial de n8n solo necesita USAGE en este esquema y SELECT/INSERT/UPDATE
-- en esta tabla. No conceder acceso a clientes, PDFs ni tablas de la aplicacion.
CREATE SCHEMA IF NOT EXISTS n8n_delivery;
CREATE TABLE IF NOT EXISTS n8n_delivery.receipts (
    idempotency_key text PRIMARY KEY CHECK (idempotency_key ~ '^[a-f0-9]{64}$'),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    owner_token text NOT NULL,
    state text NOT NULL DEFAULT 'processing' CHECK (state IN ('processing', 'sent')),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
-- Un registro processing NO caduca ni se elimina para reintentar automaticamente.
-- Puede significar que SMTP acepto el correo, pero se perdio la confirmacion.
