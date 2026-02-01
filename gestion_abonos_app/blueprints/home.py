from __future__ import annotations

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
import sqlite3

from .. import config, db
from ..services.matches import sync_upcoming_matches
from ..utils import format_abono, format_parking, normalize_text

home_bp = Blueprint("home", __name__)


def _partido_or_404(partido_id: int) -> sqlite3.Row:
    conn = db.get_connection()
    partido = conn.execute(
        "SELECT * FROM partidos WHERE id = ?", (partido_id,)
    ).fetchone()
    conn.close()
    if partido is None:
        abort(404)
    return partido


@home_bp.route("/")
def home_page():
    sync_upcoming_matches()
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT *
        FROM partidos
        WHERE fecha IS NOT NULL
          AND datetime(fecha) >= datetime('now')
        ORDER BY datetime(fecha)
        """
    ).fetchall()

    total_abonos = conn.execute("SELECT COUNT(*) AS total FROM abonos").fetchone()[
        "total"
    ]
    total_parkings = conn.execute(
        "SELECT COUNT(*) AS total FROM parkings"
    ).fetchone()["total"]

    abono_counts = {
        row["id_partido"]: row["total"]
        for row in conn.execute(
            """
            SELECT id_partido, COUNT(*) AS total
            FROM asignaciones_abonos
            GROUP BY id_partido
            """
        ).fetchall()
    }
    parking_counts = {
        row["id_partido"]: row["total"]
        for row in conn.execute(
            """
            SELECT id_partido, COUNT(*) AS total
            FROM asignaciones_parkings
            GROUP BY id_partido
            """
        ).fetchall()
    }
    conn.close()

    partidos = []
    for row in rows:
        data = dict(row)
        asignados_abonos = abono_counts.get(row["id"], 0)
        asignados_parkings = parking_counts.get(row["id"], 0)
        data["abonos_disponibles"] = max(total_abonos - asignados_abonos, 0)
        data["parkings_disponibles"] = max(total_parkings - asignados_parkings, 0)
        partidos.append(data)

    return render_template("index.html", partidos=partidos)



@home_bp.route("/partidos/<int:partido_id>")
def partido_detalle(partido_id: int):
    partido = _partido_or_404(partido_id)
    conn = db.get_connection()

    abonos_asignados = conn.execute(
        """
        SELECT aa.abono_id, aa.id_cliente, c.nombre AS cliente,
               a.sector, a.puerta, a.fila, a.asiento,
               aa.asignador
        FROM asignaciones_abonos aa
        JOIN abonos a ON a.id = aa.abono_id
        JOIN clientes c ON c.id = aa.id_cliente
        WHERE aa.id_partido = ?
        ORDER BY a.sector, a.puerta, a.fila, a.asiento
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
        ORDER BY sector, puerta, fila, asiento
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


def _validar_partido_local(partido: sqlite3.Row) -> bool:
    if not partido["localia"]:
        flash("Solo se pueden asignar recursos en partidos disputados en casa.", "warning")
        return False
    return True


@home_bp.route(
    "/partidos/<int:partido_id>/abonos/<int:abono_id>/asignar",
    methods=["GET", "POST"],
)
def asignar_abono(partido_id: int, abono_id: int):
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

    clientes = conn.execute(
        "SELECT id, nombre FROM clientes ORDER BY nombre"
    ).fetchall()

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
            except sqlite3.IntegrityError:
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

    clientes = conn.execute(
        "SELECT id, nombre FROM clientes ORDER BY nombre"
    ).fetchall()

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
            except sqlite3.IntegrityError:
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
