from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Optional, Sequence

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    create_engine,
    event,
    func,
    inspect,
    text,
)

from . import cache, config


if not config.DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está configurada.")


_DATABASE_URL = config.DATABASE_URL
engine = create_engine(
    _DATABASE_URL,
    future=True,
    pool_size=int(config.DB_POOL_SIZE),
    max_overflow=int(config.DB_MAX_OVERFLOW),
    pool_recycle=int(config.DB_POOL_RECYCLE),
    pool_pre_ping=True,
)

logger = logging.getLogger(__name__)


if _DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


metadata = MetaData()

usuarios = Table(
    "usuarios",
    metadata,
    Column("username", Text, primary_key=True),
    Column("password_hash", Text, nullable=False),
    Column("salt", Text, nullable=False),
    Column("role", Text, nullable=False, server_default="operador"),
)

clientes = Table(
    "clientes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nombre", Text, nullable=False),
    Column("email", Text),
)

partidos = Table(
    "partidos",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("jornada", Integer),
    Column("rival", Text),
    Column("fecha", Text),
    Column("localia", Integer, server_default="1"),
    Column("competicion", Text),
    Column("api_id", Text, unique=True),
    Column("estadio", Text),
    Column("equipo_local", Text),
    Column("equipo_visitante", Text),
    Column("logo_local", Text),
    Column("logo_visitante", Text),
)

abonos = Table(
    "abonos",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sector", Integer),
    Column("puerta", Integer),
    Column("fila", Integer),
    Column("asiento", Integer),
    Column("id_propietario", Integer, ForeignKey("clientes.id")),
)

parkings = Table(
    "parkings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("nombre", Text, nullable=False),
    Column("id_propietario", Integer, ForeignKey("clientes.id")),
)

asignaciones_abonos = Table(
    "asignaciones_abonos",
    metadata,
    Column("id_cliente", Integer, ForeignKey("clientes.id")),
    Column("id_partido", Integer, ForeignKey("partidos.id"), primary_key=True),
    Column("abono_id", Integer, ForeignKey("abonos.id"), primary_key=True),
    Column("asignador", Text, ForeignKey("usuarios.username")),
)

asignaciones_parkings = Table(
    "asignaciones_parkings",
    metadata,
    Column("id_cliente", Integer, ForeignKey("clientes.id")),
    Column("id_partido", Integer, ForeignKey("partidos.id"), primary_key=True),
    Column("parking_id", Integer, ForeignKey("parkings.id"), primary_key=True),
    Column("asignador", Text, ForeignKey("usuarios.username")),
)

documentos_pdf = Table(
    "documentos_pdf",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("abono_id", Integer, ForeignKey("abonos.id")),
    Column("parking_id", Integer, ForeignKey("parkings.id")),
    Column("partido_id", Integer, ForeignKey("partidos.id")),
    Column("filename", Text, nullable=False),
    Column("content_type", Text, nullable=False, server_default="application/pdf"),
    Column("byte_size", Integer, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("pdf_data", LargeBinary, nullable=False),
    Column("uploaded_by", Text, ForeignKey("usuarios.username")),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    CheckConstraint(
        "(abono_id IS NOT NULL AND parking_id IS NULL AND partido_id IS NOT NULL) OR "
        "(abono_id IS NULL AND parking_id IS NOT NULL AND partido_id IS NOT NULL)",
        name="ck_documentos_pdf_one_resource",
    ),
)

envios_email = Table(
    "envios_email",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("partido_id", Integer, ForeignKey("partidos.id"), nullable=False),
    Column("cliente_id", Integer, ForeignKey("clientes.id"), nullable=False),
    Column("documento_pdf_id", Integer, ForeignKey("documentos_pdf.id"), nullable=False),
    Column("tipo_recurso", Text, nullable=False),
    Column("recurso_id", Integer, nullable=False),
    Column("destino_email", Text, nullable=False),
    Column("documento_sha256", Text),
    Column("idempotency_key", Text, nullable=False),
    Column("estado", Text, nullable=False, server_default="pending"),
    Column("intentos", Integer, nullable=False, server_default="0"),
    Column("ultimo_error", Text),
    Column("solicitado_por", Text, ForeignKey("usuarios.username")),
    Column("solicitado_en", Integer, nullable=False),
    Column("ultimo_intento_en", Integer),
    Column("enviado_en", Integer),
)

service_sync_state = Table(
    "service_sync_state",
    metadata,
    Column("name", Text, primary_key=True),
    Column("last_checked_at", Integer),
    Column("last_synced_at", Integer),
)

rate_limit_events = Table(
    "rate_limit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope", Text, nullable=False),
    Column("bucket_key", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
)

Index("idx_partidos_fecha", partidos.c.fecha)
Index("idx_clientes_nombre", func.lower(clientes.c.nombre), unique=True)
# El correo es un destino compartido posible; no identifica de forma única a un cliente.
Index("idx_clientes_email", func.lower(clientes.c.email))
Index(
    "idx_abonos_unique",
    abonos.c.sector,
    abonos.c.puerta,
    abonos.c.fila,
    abonos.c.asiento,
    unique=True,
)
Index("idx_parkings_id", parkings.c.id, unique=True)
Index("idx_documentos_pdf_abono_partido_unique", documentos_pdf.c.abono_id, documentos_pdf.c.partido_id, unique=True)
Index("idx_documentos_pdf_parking_partido_unique", documentos_pdf.c.parking_id, documentos_pdf.c.partido_id, unique=True)
Index("idx_documentos_pdf_sha256", documentos_pdf.c.content_sha256)
Index("idx_documentos_pdf_partido_sha256_unique", documentos_pdf.c.partido_id, documentos_pdf.c.content_sha256, unique=True)
Index("idx_envios_email_idempotency", envios_email.c.idempotency_key, unique=True)
Index("idx_envios_email_estado", envios_email.c.estado, envios_email.c.solicitado_en)
Index(
    "idx_envios_email_partido_cliente",
    envios_email.c.partido_id,
    envios_email.c.cliente_id,
)
Index(
    "idx_rate_limit_events_lookup",
    rate_limit_events.c.scope,
    rate_limit_events.c.bucket_key,
    rate_limit_events.c.created_at,
)
Index(
    "idx_rate_limit_events_cleanup",
    rate_limit_events.c.scope,
    rate_limit_events.c.created_at,
)


@dataclass
class DBConnection:
    conn: Any

    def execute(self, statement: str, params: Optional[Sequence[Any]] = None):
        stmt, bound = _prepare_statement(statement, params)
        start = time.perf_counter()
        result = self.conn.execute(stmt, bound)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if config.LOG_SLOW_QUERIES and elapsed_ms >= config.SLOW_QUERY_THRESHOLD_MS:
            logger.warning(
                "Slow query %.1fms: %s",
                elapsed_ms,
                statement.strip().replace("\n", " "),
            )
        if _is_write_query(statement):
            tags = _write_tags(statement)
            if tags:
                cache.bump_cache_version(*tags)
        return ResultProxy(result)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def get_connection() -> DBConnection:
    return DBConnection(engine.connect())

class ResultProxy:
    def __init__(self, result):
        self._result = result
        self._mappings = None

    @property
    def rowcount(self):
        return self._result.rowcount

    def _map(self):
        if self._mappings is None:
            self._mappings = self._result.mappings()
        return self._mappings

    def fetchone(self):
        return self._map().fetchone()

    def fetchall(self):
        return self._map().fetchall()


def _prepare_statement(
    statement: str, params: Optional[Sequence[Any]]
):
    if not params:
        return text(statement), {}
    if "?" in statement:
        mapped = {}
        parts = statement.split("?")
        rebuilt = []
        for idx, part in enumerate(parts[:-1], start=1):
            key = f"p{idx}"
            mapped[key] = params[idx - 1]
            rebuilt.append(part + f":{key}")
        rebuilt.append(parts[-1])
        return text("".join(rebuilt)), mapped
    return text(statement), params


def _is_write_query(statement: str) -> bool:
    statement = statement.lstrip().upper()
    return statement.startswith("INSERT") or statement.startswith("UPDATE") or statement.startswith("DELETE")


def _write_tags(statement: str) -> Sequence[str]:
    statement = statement.strip()
    if not statement:
        return ()
    lowered = statement.lower()
    table = None
    if lowered.startswith("insert"):
        table = _extract_table_name(lowered, "insert into")
    elif lowered.startswith("update"):
        table = _extract_table_name(lowered, "update")
    elif lowered.startswith("delete"):
        table = _extract_table_name(lowered, "delete from")
    if not table:
        return ()
    return (table,)


def _extract_table_name(statement: str, keyword: str) -> Optional[str]:
    if keyword not in statement:
        return None
    after = statement.split(keyword, 1)[1].lstrip()
    if not after:
        return None
    token = after.split(None, 1)[0]
    token = token.strip().strip(",")
    token = token.strip('"')
    if "." in token:
        token = token.split(".")[-1]
    return token or None


SCHEMA_MIGRATIONS = ("20260422_01_add_clientes_email",)


def _migration_add_clientes_email(conn) -> None:
    inspector = inspect(conn)
    column_names = {column["name"] for column in inspector.get_columns("clientes")}
    if "email" not in column_names:
        conn.execute(text("ALTER TABLE clientes ADD COLUMN email TEXT"))
    conn.execute(
        text("DROP INDEX IF EXISTS idx_clientes_email_unique")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_clientes_email "
            "ON clientes (lower(email))"
        )
    )


def _apply_schema_migrations() -> None:
    migration_handlers = {
        "20260422_01_add_clientes_email": _migration_add_clientes_email,
    }
    now_ts = int(time.time())
    with engine.begin() as conn:
        applied_rows = conn.execute(
            text("SELECT version FROM schema_migrations")
        ).fetchall()
        applied_versions = {row[0] for row in applied_rows}
        for version in SCHEMA_MIGRATIONS:
            if version in applied_versions:
                continue
            handler = migration_handlers[version]
            handler(conn)
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, :applied_at)"
                ),
                {"version": version, "applied_at": now_ts},
            )


schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Text, primary_key=True),
    Column("applied_at", Integer, nullable=False),
)


def init_db() -> None:
    metadata.create_all(engine)
    _apply_schema_migrations()
    if not config.DEFAULT_ADMIN_USERNAME:
        return

    with engine.begin() as conn:
        stmt, bound = _prepare_statement(
            "SELECT username FROM usuarios WHERE username = ?",
            (config.DEFAULT_ADMIN_USERNAME,),
        )
        admin = conn.execute(stmt, bound).fetchone()
        if not admin:
            stmt, bound = _prepare_statement(
                """
                INSERT INTO usuarios (username, password_hash, salt, role)
                VALUES (?, ?, ?, ?)
                """,
                (
                    config.DEFAULT_ADMIN_USERNAME,
                    config.DEFAULT_ADMIN_HASH,
                    config.DEFAULT_ADMIN_SALT,
                    "admin",
                ),
            )
            conn.execute(stmt, bound)
