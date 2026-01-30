from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

from . import config


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _safe_unique_index(conn: sqlite3.Connection, statement: str) -> None:
    try:
        conn.execute(statement)
    except sqlite3.Error:
        # Si hay duplicados previos, el índice no se crea; lo ignoramos.
        pass


def _create_base_schema(conn: sqlite3.Connection) -> None:
    """Crea toda la estructura cuando la base está vacía."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operador'
        );

        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS partidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jornada INTEGER,
            rival TEXT,
            fecha TEXT,
            localia INTEGER DEFAULT 1,
            competicion TEXT,
            api_id TEXT UNIQUE,
            estadio TEXT,
            equipo_local TEXT,
            equipo_visitante TEXT,
            logo_local TEXT,
            logo_visitante TEXT
        );

        CREATE TABLE IF NOT EXISTS abonos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector INTEGER,
            puerta INTEGER,
            fila INTEGER,
            asiento INTEGER,
            id_propietario INTEGER,
            FOREIGN KEY (id_propietario) REFERENCES clientes(id)
        );

        CREATE TABLE IF NOT EXISTS parkings (
            id INTEGER PRIMARY KEY,
            nombre TEXT NOT NULL,
            id_propietario INTEGER,
            FOREIGN KEY (id_propietario) REFERENCES clientes(id)
        );

        CREATE TABLE IF NOT EXISTS asignaciones_abonos (
            id_cliente INTEGER,
            id_partido INTEGER,
            abono_id INTEGER,
            asignador TEXT,
            PRIMARY KEY (id_partido, abono_id),
            FOREIGN KEY (id_cliente) REFERENCES clientes(id),
            FOREIGN KEY (id_partido) REFERENCES partidos(id),
            FOREIGN KEY (abono_id) REFERENCES abonos(id),
            FOREIGN KEY (asignador) REFERENCES usuarios(username)
        );

        CREATE TABLE IF NOT EXISTS asignaciones_parkings (
            id_cliente INTEGER,
            id_partido INTEGER,
            parking_id INTEGER,
            asignador TEXT,
            PRIMARY KEY (id_partido, parking_id),
            FOREIGN KEY (id_cliente) REFERENCES clientes(id),
            FOREIGN KEY (id_partido) REFERENCES partidos(id),
            FOREIGN KEY (parking_id) REFERENCES parkings(id),
            FOREIGN KEY (asignador) REFERENCES usuarios(username)
        );
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_partidos_fecha ON partidos(fecha)")
    _safe_unique_index(
        conn,
        "CREATE UNIQUE INDEX idx_clientes_nombre ON clientes(lower(nombre))",
    )
    _safe_unique_index(
        conn,
        "CREATE UNIQUE INDEX idx_abonos_unique ON abonos(sector, puerta, fila, asiento)",
    )
    _safe_unique_index(
        conn,
        "CREATE UNIQUE INDEX idx_parkings_id ON parkings(id)",
    )



def init_db() -> None:
    conn = get_connection()

    _create_base_schema(conn)

    admin = conn.execute(
        "SELECT username FROM usuarios WHERE username = ?",
        (config.DEFAULT_ADMIN_USERNAME,),
    ).fetchone()
    if not admin:
        conn.execute(
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

    conn.commit()
    conn.close()
