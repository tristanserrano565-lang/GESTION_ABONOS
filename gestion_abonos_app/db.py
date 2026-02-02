from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    event,
    func,
    text,
)

from . import cache, config


def _build_database_url() -> str:
    if config.DATABASE_URL:
        return config.DATABASE_URL
    path = Path(config.DATABASE_PATH).as_posix()
    return f"sqlite:///{path}"


_DATABASE_URL = _build_database_url()
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

Index("idx_partidos_fecha", partidos.c.fecha)
Index("idx_clientes_nombre", func.lower(clientes.c.nombre), unique=True)
Index(
    "idx_abonos_unique",
    abonos.c.sector,
    abonos.c.puerta,
    abonos.c.fila,
    abonos.c.asiento,
    unique=True,
)
Index("idx_parkings_id", parkings.c.id, unique=True)


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


def init_db() -> None:
    metadata.create_all(engine)
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
