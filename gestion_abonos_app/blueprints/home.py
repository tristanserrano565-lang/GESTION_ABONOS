from __future__ import annotations

import time

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy.exc import IntegrityError

from .. import config, db
from .. import cache
from ..services.matches import sync_upcoming_matches

_HOME_MATCHES_CACHE = {"ts": 0.0, "rows": [], "version": -1}
_HOME_MATCHES_TTL = 30.0
_PARTIDO_DETALLE_CACHE = {"items": {}, "version": -1}
_PARTIDO_DETALLE_TTL = 15.0
_CLIENTES_OPTIONS_CACHE = {"ts": 0.0, "rows": [], "version": -1}
_CLIENTES_OPTIONS_TTL = 60.0
_PARTIDO_CACHE = {"items": {}, "version": -1}
_PARTIDO_TTL = 60.0
_ASIGNAR_CACHE = {"items": {}, "version": -1}
_ASIGNAR_TTL = 15.0
from ..utils import format_abono, format_parking, normalize_text

home_bp = Blueprint("home", __name__)


def _partido_or_404(partido_id: int):
    now_ts = time.time()
    cache_version = cache.cache_version("partidos")
    if _PARTIDO_CACHE["version"] != cache_version:
        _PARTIDO_CACHE["items"].clear()
        _PARTIDO_CACHE["version"] = cache_version
    entry = _PARTIDO_CACHE["items"].get(partido_id)
    if entry and now_ts - entry["ts"] <= _PARTIDO_TTL:
        return entry["row"]

    conn = db.get_connection()
    partido = conn.execute(
        "SELECT * FROM partidos WHERE id = ?", (partido_id,)
    ).fetchone()
    conn.close()
    if partido is None:
        abort(404)
    _PARTIDO_CACHE["items"][partido_id] = {"ts": now_ts, "row": partido}
    return partido


def _clientes_options():
    now_ts = time.time()
    if (
        cache.cache_version("clientes") != _CLIENTES_OPTIONS_CACHE["version"]
        or now_ts - _CLIENTES_OPTIONS_CACHE["ts"] > _CLIENTES_OPTIONS_TTL
    ):
        conn = db.get_connection()
        clientes = conn.execute(
            "SELECT id, nombre FROM clientes ORDER BY nombre"
        ).fetchall()
        conn.close()
        _CLIENTES_OPTIONS_CACHE["rows"] = clientes
        _CLIENTES_OPTIONS_CACHE["ts"] = now_ts
        _CLIENTES_OPTIONS_CACHE["version"] = cache.cache_version("clientes")
    return _CLIENTES_OPTIONS_CACHE["rows"]


def _partido_detalle_data(partido_id: int):
    now_ts = time.time()
    cache_version = cache.cache_version(
        "partidos",
        "asignaciones_abonos",
        "asignaciones_parkings",
        "abonos",
        "parkings",
        "clientes",
    )
    if _PARTIDO_DETALLE_CACHE["version"] != cache_version:
        _PARTIDO_DETALLE_CACHE["items"].clear()
        _PARTIDO_DETALLE_CACHE["version"] = cache_version
    entry = _PARTIDO_DETALLE_CACHE["items"].get(partido_id)
    if entry and now_ts - entry["ts"] <= _PARTIDO_DETALLE_TTL:
        return entry["data"]

    conn = db.get_connection()
    partido = conn.execute(
        "SELECT * FROM partidos WHERE id = ?", (partido_id,)
    ).fetchone()
    if partido is None:
        conn.close()
        abort(404)

    abonos_asignados = conn.execute(
        """
        SELECT aa.abono_id, aa.id_cliente, c.nombre AS cliente,
               a.sector, a.puerta, a.fila, a.asiento,
               aa.asignador
        FROM asignaciones_abonos aa
        JOIN abonos a ON a.id = aa.abono_id
        JOIN clientes c ON c.id = aa.id_cliente
        WHERE aa.id_partido = ?
        ORDER BY a.puerta, a.sector, a.fila, a.asiento
        """,
        (partido_id,),
    ).fetchall()

    parkings_asignados = conn.execute(
        """
        SELECT ap.parking_id, ap.id_cliente, c.nombre AS cliente, p.nombre
               , ap.asignador
        FROM asignaciones_parkings ap
        JOIN parkings p ON p.id = ap.parking_id
        JOIN clientes c ON c.id = ap.id_cliente
        WHERE ap.id_partido = ?
        ORDER BY c.nombre
        """,
        (partido_id,),
    ).fetchall()

    abonos_disponibles = conn.execute(
        """
        SELECT id, sector, puerta, fila, asiento
        FROM abonos
        WHERE id NOT IN (
            SELECT abono_id FROM asignaciones_abonos WHERE id_partido = ?
        )
        ORDER BY puerta, sector, fila, asiento
        """,
        (partido_id,),
    ).fetchall()

    parkings_disponibles = conn.execute(
        """
        SELECT id, nombre
        FROM parkings
        WHERE id NOT IN (
            SELECT parking_id FROM asignaciones_parkings WHERE id_partido = ?
        )
        ORDER BY nombre
        """,
        (partido_id,),
    ).fetchall()
    conn.close()

    data = {
        "partido": partido,
        "abonos_asignados": abonos_asignados,
        "abonos_disponibles": abonos_disponibles,
        "parkings_asignados": parkings_asignados,
        "parkings_disponibles": parkings_disponibles,
    }
    _PARTIDO_DETALLE_CACHE["items"][partido_id] = {"ts": now_ts, "data": data}
    return data


def _asignar_cache_key(tipo: str, partido_id: int, recurso_id: int) -> str:
    return f"{tipo}:{partido_id}:{recurso_id}"


def _asignar_context(tipo: str, partido_id: int, recurso_id: int):
    now_ts = time.time()
    cache_version = cache.cache_version(
        "partidos",
        "asignaciones_abonos",
        "asignaciones_parkings",
        "abonos",
        "parkings",
        "clientes",
    )
    if _ASIGNAR_CACHE["version"] != cache_version:
        _ASIGNAR_CACHE["items"].clear()
        _ASIGNAR_CACHE["version"] = cache_version
    key = _asignar_cache_key(tipo, partido_id, recurso_id)
    entry = _ASIGNAR_CACHE["items"].get(key)
    if entry and now_ts - entry["ts"] <= _ASIGNAR_TTL:
        return entry["data"]

    conn = db.get_connection()
    partido = conn.execute(
        "SELECT * FROM partidos WHERE id = ?", (partido_id,)
    ).fetchone()
    if partido is None:
        conn.close()
        abort(404)

    if tipo == "abono":
        already_assigned = conn.execute(
            "SELECT 1 FROM asignaciones_abonos WHERE id_partido = ? AND abono_id = ?",
            (partido_id, recurso_id),
        ).fetchone()
        recurso = conn.execute(
            "SELECT * FROM abonos WHERE id = ?", (recurso_id,)
        ).fetchone()
    else:
        already_assigned = conn.execute(
            "SELECT 1 FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
            (partido_id, recurso_id),
        ).fetchone()
        recurso = conn.execute(
            "SELECT * FROM parkings WHERE id = ?", (recurso_id,)
        ).fetchone()

    conn.close()
    if recurso is None:
        abort(404)

    data = {
        "partido": partido,
        "recurso": recurso,
        "already_assigned": bool(already_assigned),
        "clientes": _clientes_options(),
    }
    _ASIGNAR_CACHE["items"][key] = {"ts": now_ts, "data": data}
    return data


@home_bp.route("/")
def home_page():
    sync_upcoming_matches()
    conn = db.get_connection()
    now_ts = time.time()
    if (
        cache.cache_version(
            "partidos",
            "asignaciones_abonos",
            "asignaciones_parkings",
            "abonos",
            "parkings",
        ) != _HOME_MATCHES_CACHE["version"]
        or now_ts - _HOME_MATCHES_CACHE["ts"] > _HOME_MATCHES_TTL
    ):
        rows = conn.execute(
            """
            SELECT p.*,
                   COALESCE(abonos.total_abonos, 0) AS asignados_abonos,
                   COALESCE(parkings.total_parkings, 0) AS asignados_parkings,
                   (SELECT COUNT(*) FROM abonos) AS total_abonos,
                   (SELECT COUNT(*) FROM parkings) AS total_parkings
            FROM partidos p
            LEFT JOIN (
                SELECT id_partido, COUNT(*) AS total_abonos
                FROM asignaciones_abonos
                GROUP BY id_partido
            ) AS abonos ON abonos.id_partido = p.id
            LEFT JOIN (
                SELECT id_partido, COUNT(*) AS total_parkings
                FROM asignaciones_parkings
                GROUP BY id_partido
            ) AS parkings ON parkings.id_partido = p.id
            WHERE p.fecha IS NOT NULL
              AND p.fecha::timestamp >= now()
            ORDER BY p.fecha::timestamp
            """
        ).fetchall()
        _HOME_MATCHES_CACHE["rows"] = rows
        _HOME_MATCHES_CACHE["ts"] = now_ts
        _HOME_MATCHES_CACHE["version"] = cache.cache_version(
            "partidos",
            "asignaciones_abonos",
            "asignaciones_parkings",
            "abonos",
            "parkings",
        )
    rows = _HOME_MATCHES_CACHE["rows"]
    conn.close()

    partidos = []
    for row in rows:
        data = dict(row)
        total_abonos = row["total_abonos"]
        total_parkings = row["total_parkings"]
        asignados_abonos = row["asignados_abonos"]
        asignados_parkings = row["asignados_parkings"]
        data["abonos_disponibles"] = max(total_abonos - asignados_abonos, 0)
        data["parkings_disponibles"] = max(total_parkings - asignados_parkings, 0)
        partidos.append(data)

    return render_template("index.html", partidos=partidos)



@home_bp.route("/partidos/<int:partido_id>")
def partido_detalle(partido_id: int):
    data = _partido_detalle_data(partido_id)
    partido = data["partido"]
    abonos_asignados = data["abonos_asignados"]
    abonos_disponibles = data["abonos_disponibles"]
    parkings_asignados = data["parkings_asignados"]
    parkings_disponibles = data["parkings_disponibles"]

    puede_reservar = bool(partido["localia"])

    return render_template(
        "partido_detalle.html",
        partido=partido,
        abonos_asignados=abonos_asignados,
        abonos_disponibles=abonos_disponibles,
        parkings_asignados=parkings_asignados,
        parkings_disponibles=parkings_disponibles,
        puede_reservar=puede_reservar,
    )


@home_bp.post("/partidos/<int:partido_id>/abonos/<int:abono_id>/liberar")
def liberar_abono(partido_id: int, abono_id: int):
    partido = _partido_or_404(partido_id)
    conn = db.get_connection()
    deleted = conn.execute(
        "DELETE FROM asignaciones_abonos WHERE id_partido = ? AND abono_id = ?",
        (partido_id, abono_id),
    )
    conn.commit()
    conn.close()
    if deleted.rowcount:
        flash("Abono liberado correctamente.", "success")
    else:
        flash("El abono ya estaba libre.", "info")
    return redirect(url_for("home.partido_detalle", partido_id=partido["id"]))


@home_bp.post("/partidos/<int:partido_id>/parkings/<int:parking_id>/liberar")
def liberar_parking(partido_id: int, parking_id: int):
    partido = _partido_or_404(partido_id)
    conn = db.get_connection()
    deleted = conn.execute(
        "DELETE FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
        (partido_id, parking_id),
    )
    conn.commit()
    conn.close()
    if deleted.rowcount:
        flash("Parking liberado correctamente.", "success")
    else:
        flash("El parking ya estaba libre.", "info")
    return redirect(url_for("home.partido_detalle", partido_id=partido["id"]))


def _validar_partido_local(partido) -> bool:
    if not partido["localia"]:
        flash("Solo se pueden asignar recursos en partidos disputados en casa.", "warning")
        return False
    return True


@home_bp.route(
    "/partidos/<int:partido_id>/abonos/<int:abono_id>/asignar",
    methods=["GET", "POST"],
)
def asignar_abono(partido_id: int, abono_id: int):
    if request.method == "GET":
        data = _asignar_context("abono", partido_id, abono_id)
        partido = data["partido"]
        if not _validar_partido_local(partido):
            return redirect(url_for("home.partido_detalle", partido_id=partido_id))
        if data["already_assigned"]:
            flash("Ese abono ya estÃ¡ asignado para este partido.", "warning")
            return redirect(url_for("home.partido_detalle", partido_id=partido_id))
        response = make_response(
            render_template(
                "seleccionar_cliente.html",
                partido=partido,
                recurso=data["recurso"],
                clientes=data["clientes"],
                tipo="abono",
            )
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    partido = _partido_or_404(partido_id)
    if not _validar_partido_local(partido):
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    conn = db.get_connection()
    already_assigned = conn.execute(
        "SELECT 1 FROM asignaciones_abonos WHERE id_partido = ? AND abono_id = ?",
        (partido_id, abono_id),
    ).fetchone()
    if already_assigned:
        conn.close()
        flash("Ese abono ya está asignado para este partido.", "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))
    abono = conn.execute("SELECT * FROM abonos WHERE id = ?", (abono_id,)).fetchone()
    if abono is None:
        conn.close()
        abort(404)

    clientes = _clientes_options()

    if request.method == "POST":
        if request.form.get("crear_cliente"):
            nuevo_nombre = normalize_text(request.form.get("nuevo_nombre"))
            if not nuevo_nombre:
                flash("El nombre del cliente es obligatorio.", "warning")
            else:
                existe = conn.execute(
                    "SELECT 1 FROM clientes WHERE lower(nombre) = lower(?)",
                    (nuevo_nombre,),
                ).fetchone()
                if existe:
                    flash("Ya existe un cliente con ese nombre.", "warning")
                else:
                    conn.execute(
                        "INSERT INTO clientes (nombre) VALUES (?)",
                        (nuevo_nombre,),
                    )
                    conn.commit()
                    flash("Cliente creado correctamente.", "success")
                    conn.close()
                    return redirect(
                        url_for(
                            "home.asignar_abono",
                            partido_id=partido_id,
                            abono_id=abono_id,
                        )
                    )
        cliente_id = request.form.get("cliente_id")
        if not cliente_id:
            flash("Selecciona un cliente válido.", "warning")
        else:
            try:
                cliente = conn.execute(
                    "SELECT nombre FROM clientes WHERE id = ?", (cliente_id,)
                ).fetchone()
                if not cliente:
                    flash("El cliente indicado no existe.", "danger")
                else:
                    conn.execute(
                        """
                        INSERT INTO asignaciones_abonos (id_cliente, id_partido, abono_id, asignador)
                        VALUES (?, ?, ?, ?)
                        """,
                        (cliente_id, partido_id, abono_id, g.current_user["username"]),
                    )
                    conn.commit()
                    flash(
                        f"{format_abono(abono)} asignado a {cliente['nombre']}.",
                        "success",
                    )
                    conn.close()
                    return redirect(url_for("home.home_page"))
            except IntegrityError:
                flash("El abono ya está reservado para este partido.", "danger")

    conn.close()
    response = make_response(
        render_template(
            "seleccionar_cliente.html",
            partido=partido,
            recurso=abono,
            clientes=clientes,
            tipo="abono",
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@home_bp.route(
    "/partidos/<int:partido_id>/parkings/<int:parking_id>/asignar",
    methods=["GET", "POST"],
)
def asignar_parking(partido_id: int, parking_id: int):
    if request.method == "GET":
        data = _asignar_context("parking", partido_id, parking_id)
        partido = data["partido"]
        if not _validar_partido_local(partido):
            return redirect(url_for("home.partido_detalle", partido_id=partido_id))
        if data["already_assigned"]:
            flash("Ese parking ya estÃ¡ asignado para este partido.", "warning")
            return redirect(url_for("home.partido_detalle", partido_id=partido_id))
        response = make_response(
            render_template(
                "seleccionar_cliente.html",
                partido=partido,
                recurso=data["recurso"],
                clientes=data["clientes"],
                tipo="parking",
            )
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    partido = _partido_or_404(partido_id)
    if not _validar_partido_local(partido):
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    conn = db.get_connection()
    already_assigned = conn.execute(
        "SELECT 1 FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
        (partido_id, parking_id),
    ).fetchone()
    if already_assigned:
        conn.close()
        flash("Ese parking ya está asignado para este partido.", "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))
    parking = conn.execute(
        "SELECT * FROM parkings WHERE id = ?", (parking_id,)
    ).fetchone()
    if parking is None:
        conn.close()
        abort(404)

    clientes = _clientes_options()

    if request.method == "POST":
        if request.form.get("crear_cliente"):
            nuevo_nombre = normalize_text(request.form.get("nuevo_nombre"))
            if not nuevo_nombre:
                flash("El nombre del cliente es obligatorio.", "warning")
            else:
                existe = conn.execute(
                    "SELECT 1 FROM clientes WHERE lower(nombre) = lower(?)",
                    (nuevo_nombre,),
                ).fetchone()
                if existe:
                    flash("Ya existe un cliente con ese nombre.", "warning")
                else:
                    conn.execute(
                        "INSERT INTO clientes (nombre) VALUES (?)",
                        (nuevo_nombre,),
                    )
                    conn.commit()
                    flash("Cliente creado correctamente.", "success")
                    conn.close()
                    return redirect(
                        url_for(
                            "home.asignar_parking",
                            partido_id=partido_id,
                            parking_id=parking_id,
                        )
                    )
        cliente_id = request.form.get("cliente_id")
        if not cliente_id:
            flash("Selecciona un cliente válido.", "warning")
        else:
            try:
                cliente = conn.execute(
                    "SELECT nombre FROM clientes WHERE id = ?", (cliente_id,)
                ).fetchone()
                if not cliente:
                    flash("El cliente indicado no existe.", "danger")
                else:
                    conn.execute(
                        """
                        INSERT INTO asignaciones_parkings (id_cliente, id_partido, parking_id, asignador)
                        VALUES (?, ?, ?, ?)
                        """,
                        (cliente_id, partido_id, parking_id, g.current_user["username"]),
                    )
                    conn.commit()
                    flash(
                        f"{format_parking(parking)} asignado a {cliente['nombre']}.",
                        "success",
                    )
                    conn.close()
                    return redirect(url_for("home.home_page"))
            except IntegrityError:
                flash("El parking ya está reservado para este partido.", "danger")

    conn.close()
    response = make_response(
        render_template(
            "seleccionar_cliente.html",
            partido=partido,
            recurso=parking,
            clientes=clientes,
            tipo="parking",
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@home_bp.route("/partidos/<int:partido_id>/asignar", methods=["POST"])
def asignar_multiples(partido_id: int):
    partido = _partido_or_404(partido_id)
    if not _validar_partido_local(partido):
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    abono_ids = [int(value) for value in request.form.getlist("abono_ids") if value.isdigit()]
    parking_ids = [int(value) for value in request.form.getlist("parking_ids") if value.isdigit()]
    abono_ids = sorted(set(abono_ids))
    parking_ids = sorted(set(parking_ids))

    if not abono_ids and not parking_ids:
        flash("Selecciona al menos un abono o parking para asignar.", "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    conn = db.get_connection()

    if request.form.get("crear_cliente"):
        nuevo_nombre = normalize_text(request.form.get("nuevo_nombre"))
        if not nuevo_nombre:
            flash("El nombre del cliente es obligatorio.", "warning")
        else:
            existe = conn.execute(
                "SELECT 1 FROM clientes WHERE lower(nombre) = lower(?)",
                (nuevo_nombre,),
            ).fetchone()
            if existe:
                flash("Ya existe un cliente con ese nombre.", "warning")
            else:
                conn.execute(
                    "INSERT INTO clientes (nombre) VALUES (?)",
                    (nuevo_nombre,),
                )
                conn.commit()
                flash("Cliente creado correctamente.", "success")

    cliente_id = request.form.get("cliente_id")
    if cliente_id:
        try:
            cliente = conn.execute(
                "SELECT nombre FROM clientes WHERE id = ?", (cliente_id,)
            ).fetchone()
            if not cliente:
                flash("El cliente indicado no existe.", "danger")
            else:
                asignados = 0
                repetidos = 0
                for abono_id in abono_ids:
                    try:
                        conn.execute(
                            """
                            INSERT INTO asignaciones_abonos (id_cliente, id_partido, abono_id, asignador)
                            VALUES (?, ?, ?, ?)
                            """,
                            (cliente_id, partido_id, abono_id, g.current_user["username"]),
                        )
                        asignados += 1
                    except IntegrityError:
                        repetidos += 1
                for parking_id in parking_ids:
                    try:
                        conn.execute(
                            """
                            INSERT INTO asignaciones_parkings (id_cliente, id_partido, parking_id, asignador)
                            VALUES (?, ?, ?, ?)
                            """,
                            (cliente_id, partido_id, parking_id, g.current_user["username"]),
                        )
                        asignados += 1
                    except IntegrityError:
                        repetidos += 1
                conn.commit()
                flash(
                    f"Asignados {asignados} recursos a {cliente['nombre']}.",
                    "success",
                )
                if repetidos:
                    flash(
                        f"{repetidos} recursos ya estaban asignados para este partido.",
                        "warning",
                    )
                conn.close()
                return redirect(url_for("home.home_page"))
        except IntegrityError:
            flash("Algunos recursos ya estaban asignados.", "warning")

    conn.close()
    response = make_response(
        render_template(
            "seleccionar_cliente.html",
            partido=partido,
            clientes=_clientes_options(),
            modo_multiple=True,
            seleccion_abonos=abono_ids,
            seleccion_parkings=parking_ids,
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response
