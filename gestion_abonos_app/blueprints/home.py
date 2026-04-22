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
from ..services.email_delivery import (
    build_delivery_snapshot_token,
    DeliveryBusyError,
    DeliverySendError,
    get_partido_delivery_overview,
    send_assigned_resources_for_partido,
)
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
from ..utils import (
    format_abono,
    format_parking,
    is_valid_email,
    normalize_email,
    normalize_text,
)

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
            "SELECT id, nombre, email FROM clientes ORDER BY nombre"
        ).fetchall()
        conn.close()
        _CLIENTES_OPTIONS_CACHE["rows"] = clientes
        _CLIENTES_OPTIONS_CACHE["ts"] = now_ts
        _CLIENTES_OPTIONS_CACHE["version"] = cache.cache_version("clientes")
    return _CLIENTES_OPTIONS_CACHE["rows"]


def _validate_new_cliente_payload(nombre: str, email: str) -> str | None:
    if not nombre:
        return "El nombre del cliente es obligatorio."
    if len(nombre) > 128:
        return "El nombre del cliente no puede exceder los 128 caracteres."
    if email and len(email) > 255:
        return "El email no puede exceder los 255 caracteres."
    if email and not is_valid_email(email):
        return "Introduce un email válido."
    return None


def _create_cliente_from_request(conn) -> bool:
    nuevo_nombre = normalize_text(request.form.get("nuevo_nombre"))
    nuevo_email = normalize_email(request.form.get("nuevo_email"))
    validation_error = _validate_new_cliente_payload(nuevo_nombre, nuevo_email)
    if validation_error:
        flash(validation_error, "warning")
        return False

    existe = conn.execute(
        "SELECT 1 FROM clientes WHERE lower(nombre) = lower(?)",
        (nuevo_nombre,),
    ).fetchone()
    if existe:
        flash("Ya existe un cliente con ese nombre.", "warning")
        return False

    try:
        conn.execute(
            "INSERT INTO clientes (nombre, email) VALUES (?, ?)",
            (nuevo_nombre, nuevo_email or None),
        )
        conn.commit()
        flash("Cliente creado correctamente.", "success")
        return True
    except IntegrityError:
        conn.conn.rollback()
        flash("Ya existe un cliente con ese nombre o con ese email.", "warning")
        return False


def _resource_has_pdf(conn, tipo: str, recurso_id: int) -> bool:
    column = "abono_id" if tipo == "abono" else "parking_id"
    row = conn.execute(
        f"SELECT 1 FROM documentos_pdf WHERE {column} = ?",
        (recurso_id,),
    ).fetchone()
    return bool(row)


def _assignable_resource_ids(conn, tipo: str, recurso_ids: list[int]) -> set[int]:
    if not recurso_ids:
        return set()
    column = "abono_id" if tipo == "abono" else "parking_id"
    placeholders = ", ".join("?" for _ in recurso_ids)
    rows = conn.execute(
        f"SELECT {column} AS recurso_id FROM documentos_pdf WHERE {column} IN ({placeholders})",
        tuple(recurso_ids),
    ).fetchall()
    return {int(row["recurso_id"]) for row in rows}


def _partido_detalle_data(partido_id: int):
    now_ts = time.time()
    cache_version = cache.cache_version(
        "partidos",
        "asignaciones_abonos",
        "asignaciones_parkings",
        "abonos",
        "parkings",
        "clientes",
        "documentos_pdf",
        "envios_email",
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
               c.email AS cliente_email,
               a.sector, a.puerta, a.fila, a.asiento,
               aa.asignador,
               ee.estado AS estado_envio,
               ee.enviado_en AS enviado_en
        FROM asignaciones_abonos aa
        JOIN abonos a ON a.id = aa.abono_id
        JOIN clientes c ON c.id = aa.id_cliente
        LEFT JOIN documentos_pdf d ON d.abono_id = aa.abono_id
        LEFT JOIN envios_email ee
            ON ee.partido_id = aa.id_partido
           AND ee.cliente_id = aa.id_cliente
           AND ee.documento_pdf_id = d.id
           AND ee.tipo_recurso = 'abono'
           AND ee.recurso_id = aa.abono_id
           AND lower(ee.destino_email) = lower(TRIM(COALESCE(c.email, '')))
        WHERE aa.id_partido = ?
        ORDER BY a.puerta, a.sector, a.fila, a.asiento
        """,
        (partido_id,),
    ).fetchall()

    parkings_asignados = conn.execute(
        """
        SELECT ap.parking_id, ap.id_cliente, c.nombre AS cliente,
               c.email AS cliente_email,
               p.nombre, ap.asignador,
               ee.estado AS estado_envio,
               ee.enviado_en AS enviado_en
        FROM asignaciones_parkings ap
        JOIN parkings p ON p.id = ap.parking_id
        JOIN clientes c ON c.id = ap.id_cliente
        LEFT JOIN documentos_pdf d ON d.parking_id = ap.parking_id
        LEFT JOIN envios_email ee
            ON ee.partido_id = ap.id_partido
           AND ee.cliente_id = ap.id_cliente
           AND ee.documento_pdf_id = d.id
           AND ee.tipo_recurso = 'parking'
           AND ee.recurso_id = ap.parking_id
           AND lower(ee.destino_email) = lower(TRIM(COALESCE(c.email, '')))
        WHERE ap.id_partido = ?
        ORDER BY c.nombre
        """,
        (partido_id,),
    ).fetchall()

    abonos_disponibles = conn.execute(
        """
        SELECT a.id,
               a.sector,
               a.puerta,
               a.fila,
               a.asiento,
               d.id AS documento_pdf_id
        FROM abonos a
        LEFT JOIN documentos_pdf d ON d.abono_id = a.id
        WHERE a.id NOT IN (
            SELECT abono_id FROM asignaciones_abonos WHERE id_partido = ?
        )
        ORDER BY a.puerta, a.sector, a.fila, a.asiento
        """,
        (partido_id,),
    ).fetchall()

    parkings_disponibles = conn.execute(
        """
        SELECT p.id,
               p.nombre,
               d.id AS documento_pdf_id
        FROM parkings p
        LEFT JOIN documentos_pdf d ON d.parking_id = p.id
        WHERE p.id NOT IN (
            SELECT parking_id FROM asignaciones_parkings WHERE id_partido = ?
        )
        ORDER BY p.nombre
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
        "envio_resumen": get_partido_delivery_overview(partido_id),
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
        "documentos_pdf",
        "envios_email",
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
            """
            SELECT a.*, d.id AS documento_pdf_id
            FROM abonos a
            LEFT JOIN documentos_pdf d ON d.abono_id = a.id
            WHERE a.id = ?
            """,
            (recurso_id,),
        ).fetchone()
    else:
        already_assigned = conn.execute(
            "SELECT 1 FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
            (partido_id, recurso_id),
        ).fetchone()
        recurso = conn.execute(
            """
            SELECT p.*, d.id AS documento_pdf_id
            FROM parkings p
            LEFT JOIN documentos_pdf d ON d.parking_id = p.id
            WHERE p.id = ?
            """,
            (recurso_id,),
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
            "documentos_pdf",
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
            "documentos_pdf",
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
    envio_resumen = data["envio_resumen"]
    delivery_snapshot_token = build_delivery_snapshot_token(partido_id)

    puede_reservar = bool(partido["localia"])

    return render_template(
        "partido_detalle.html",
        partido=partido,
        abonos_asignados=abonos_asignados,
        abonos_disponibles=abonos_disponibles,
        parkings_asignados=parkings_asignados,
        parkings_disponibles=parkings_disponibles,
        puede_reservar=puede_reservar,
        envio_resumen=envio_resumen,
        delivery_snapshot_token=delivery_snapshot_token,
    )


@home_bp.post("/partidos/<int:partido_id>/enviar-asignados")
def enviar_asignados(partido_id: int):
    _partido_or_404(partido_id)
    if not config.N8N_WEBHOOK_URL:
        flash(
            "Configura N8N_WEBHOOK_URL antes de lanzar el envío de asignados.",
            "warning",
        )
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    try:
        summary = send_assigned_resources_for_partido(
            partido_id,
            solicitado_por=g.current_user["username"],
            snapshot_token=(request.form.get("delivery_snapshot_token") or "").strip(),
        )
    except DeliveryBusyError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))
    except DeliverySendError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    overview_before = summary.overview_before
    overview_after = summary.overview_after

    if overview_before.total_assigned == 0:
        flash("No hay abonos ni parkings asignados para este partido.", "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    if summary.cancelled_obsolete:
        flash(
            f"Se han descartado {summary.cancelled_obsolete} envío(s) obsoletos por cambios en las asignaciones o en sus PDFs/emails.",
            "info",
        )
    if summary.requeued_stale:
        flash(
            f"Se han recuperado {summary.requeued_stale} envío(s) que quedaron a medias y se han vuelto a poner en cola.",
            "info",
        )
    if summary.reactivated_exhausted_errors:
        flash(
            f"Se han reactivado {summary.reactivated_exhausted_errors} envío(s) con error que habían agotado intentos previos.",
            "info",
        )
    if summary.snapshot_skipped_changed:
        flash(
            f"Se han omitido {summary.snapshot_skipped_changed} recurso(s) porque cambiaron o se liberaron después de que cargases esta pantalla. Refresca el partido para ver el estado actual.",
            "warning",
        )
    if overview_before.missing_email_count:
        flash(
            f"{overview_before.missing_email_count} recurso(s) asignados no se pueden enviar porque el cliente no tiene email.",
            "warning",
        )
    if overview_before.missing_pdf_count:
        flash(
            f"{overview_before.missing_pdf_count} recurso(s) asignados no se pueden enviar porque no tienen PDF asociado.",
            "warning",
        )
    if summary.sent_now:
        flash(
            f"Se han enviado correctamente {summary.sent_now} documento(s) de este partido.",
            "success",
        )
    if summary.failed_now:
        flash(
            f"{summary.failed_now} envío(s) han fallado y quedan registrados para reintento posterior.",
            "warning",
        )

    if (
        summary.sent_now == 0
        and summary.failed_now == 0
        and overview_after.ready_count > 0
        and overview_after.ready_count == overview_after.sent_count
        and overview_after.pending_count == 0
        and overview_after.processing_count == 0
        and overview_after.error_count == 0
    ):
        flash(
            "Todos los recursos listos para este partido ya estaban enviados.",
            "info",
        )
    elif (
        summary.sent_now == 0
        and summary.failed_now == 0
        and overview_after.ready_count == 0
    ):
        flash(
            "No hay recursos listos para enviar en este partido.",
            "warning",
        )

    return redirect(url_for("home.partido_detalle", partido_id=partido_id))


@home_bp.post("/partidos/<int:partido_id>/abonos/<int:abono_id>/liberar")
def liberar_abono(partido_id: int, abono_id: int):
    partido = _partido_or_404(partido_id)
    conn = db.get_connection()
    try:
        previous_status = _current_delivery_status(conn, "abono", partido_id, abono_id)
        purged_delivery_rows = 0
        deleted = conn.execute(
            "DELETE FROM asignaciones_abonos WHERE id_partido = ? AND abono_id = ?",
            (partido_id, abono_id),
        )
        if deleted.rowcount:
            purged_delivery_rows = _purge_delivery_rows_for_resource(
                conn,
                "abono",
                partido_id,
                abono_id,
            )
        conn.commit()
    finally:
        conn.close()
    if deleted.rowcount:
        if previous_status == "sent":
            flash(
                "Abono liberado correctamente. Se ha limpiado su registro de envío para permitir un futuro reenvío si vuelve a asignarse.",
                "warning",
            )
        elif purged_delivery_rows:
            flash(
                "Abono liberado correctamente y trazas de envío anteriores eliminadas.",
                "success",
            )
        else:
            flash("Abono liberado correctamente.", "success")
    else:
        flash("El abono ya estaba libre.", "info")
    return redirect(url_for("home.partido_detalle", partido_id=partido["id"]))


@home_bp.post("/partidos/<int:partido_id>/parkings/<int:parking_id>/liberar")
def liberar_parking(partido_id: int, parking_id: int):
    partido = _partido_or_404(partido_id)
    conn = db.get_connection()
    try:
        previous_status = _current_delivery_status(conn, "parking", partido_id, parking_id)
        purged_delivery_rows = 0
        deleted = conn.execute(
            "DELETE FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
            (partido_id, parking_id),
        )
        if deleted.rowcount:
            purged_delivery_rows = _purge_delivery_rows_for_resource(
                conn,
                "parking",
                partido_id,
                parking_id,
            )
        conn.commit()
    finally:
        conn.close()
    if deleted.rowcount:
        if previous_status == "sent":
            flash(
                "Parking liberado correctamente. Se ha limpiado su registro de envío para permitir un futuro reenvío si vuelve a asignarse.",
                "warning",
            )
        elif purged_delivery_rows:
            flash(
                "Parking liberado correctamente y trazas de envío anteriores eliminadas.",
                "success",
            )
        else:
            flash("Parking liberado correctamente.", "success")
    else:
        flash("El parking ya estaba libre.", "info")
    return redirect(url_for("home.partido_detalle", partido_id=partido["id"]))


def _validar_partido_local(partido) -> bool:
    if not partido["localia"]:
        flash("Solo se pueden asignar recursos en partidos disputados en casa.", "warning")
        return False
    return True


def _current_delivery_status(conn, tipo: str, partido_id: int, recurso_id: int) -> str | None:
    if tipo == "abono":
        row = conn.execute(
            """
            SELECT ee.estado
            FROM asignaciones_abonos aa
            JOIN clientes c ON c.id = aa.id_cliente
            LEFT JOIN documentos_pdf d ON d.abono_id = aa.abono_id
            LEFT JOIN envios_email ee
                ON ee.partido_id = aa.id_partido
               AND ee.cliente_id = aa.id_cliente
               AND ee.documento_pdf_id = d.id
               AND ee.tipo_recurso = 'abono'
               AND ee.recurso_id = aa.abono_id
               AND lower(ee.destino_email) = lower(TRIM(COALESCE(c.email, '')))
            WHERE aa.id_partido = ?
              AND aa.abono_id = ?
            ORDER BY ee.enviado_en DESC, ee.id DESC
            LIMIT 1
            """,
            (partido_id, recurso_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT ee.estado
            FROM asignaciones_parkings ap
            JOIN clientes c ON c.id = ap.id_cliente
            LEFT JOIN documentos_pdf d ON d.parking_id = ap.parking_id
            LEFT JOIN envios_email ee
                ON ee.partido_id = ap.id_partido
               AND ee.cliente_id = ap.id_cliente
               AND ee.documento_pdf_id = d.id
               AND ee.tipo_recurso = 'parking'
               AND ee.recurso_id = ap.parking_id
               AND lower(ee.destino_email) = lower(TRIM(COALESCE(c.email, '')))
            WHERE ap.id_partido = ?
              AND ap.parking_id = ?
            ORDER BY ee.enviado_en DESC, ee.id DESC
            LIMIT 1
            """,
            (partido_id, recurso_id),
        ).fetchone()
    return row["estado"] if row and row["estado"] else None


def _purge_delivery_rows_for_resource(conn, tipo: str, partido_id: int, recurso_id: int) -> int:
    deleted = conn.execute(
        """
        DELETE FROM envios_email
        WHERE partido_id = ?
          AND tipo_recurso = ?
          AND recurso_id = ?
        """,
        (partido_id, tipo, recurso_id),
    )
    return int(deleted.rowcount or 0)


def _validate_resource_pdf_or_redirect(
    recurso,
    *,
    tipo: str,
    partido_id: int,
):
    if recurso and recurso.get("documento_pdf_id"):
        return None
    label = "abono" if tipo == "abono" else "parking"
    flash(
        f"No se puede asignar este {label} porque no tiene PDF asociado.",
        "warning",
    )
    return redirect(url_for("home.partido_detalle", partido_id=partido_id))


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
        no_pdf_redirect = _validate_resource_pdf_or_redirect(
            data["recurso"],
            tipo="abono",
            partido_id=partido_id,
        )
        if no_pdf_redirect:
            return no_pdf_redirect
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
    if not _resource_has_pdf(conn, "abono", abono_id):
        conn.close()
        flash("No se puede asignar este abono porque no tiene PDF asociado.", "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    clientes = _clientes_options()

    if request.method == "POST":
        if request.form.get("crear_cliente"):
            if _create_cliente_from_request(conn):
                conn.close()
                return redirect(
                    url_for(
                        "home.asignar_abono",
                        partido_id=partido_id,
                        abono_id=abono_id,
                    )
                )
            conn.close()
            response = make_response(
                render_template(
                    "seleccionar_cliente.html",
                    partido=partido,
                    recurso=abono,
                    clientes=_clientes_options(),
                    tipo="abono",
                )
            )
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            return response
        cliente_id = request.form.get("cliente_id")
        if not cliente_id:
            flash("Selecciona un cliente válido.", "warning")
        else:
            try:
                already_assigned = conn.execute(
                    "SELECT 1 FROM asignaciones_abonos WHERE id_partido = ? AND abono_id = ?",
                    (partido_id, abono_id),
                ).fetchone()
                if already_assigned:
                    flash("Ese abono ya está asignado para este partido.", "warning")
                    return redirect(url_for("home.partido_detalle", partido_id=partido_id))
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

    if conn is not None:
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
        no_pdf_redirect = _validate_resource_pdf_or_redirect(
            data["recurso"],
            tipo="parking",
            partido_id=partido_id,
        )
        if no_pdf_redirect:
            return no_pdf_redirect
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
    if not _resource_has_pdf(conn, "parking", parking_id):
        conn.close()
        flash("No se puede asignar este parking porque no tiene PDF asociado.", "warning")
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    clientes = _clientes_options()

    if request.method == "POST":
        if request.form.get("crear_cliente"):
            if _create_cliente_from_request(conn):
                conn.close()
                return redirect(
                    url_for(
                        "home.asignar_parking",
                        partido_id=partido_id,
                        parking_id=parking_id,
                    )
                )
            conn.close()
            response = make_response(
                render_template(
                    "seleccionar_cliente.html",
                    partido=partido,
                    recurso=parking,
                    clientes=_clientes_options(),
                    tipo="parking",
                )
            )
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            return response
        cliente_id = request.form.get("cliente_id")
        if not cliente_id:
            flash("Selecciona un cliente válido.", "warning")
        else:
            try:
                already_assigned = conn.execute(
                    "SELECT 1 FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
                    (partido_id, parking_id),
                ).fetchone()
                if already_assigned:
                    flash("Ese parking ya está asignado para este partido.", "warning")
                    return redirect(url_for("home.partido_detalle", partido_id=partido_id))
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

    if conn is not None:
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
    assignable_abono_ids = _assignable_resource_ids(conn, "abono", abono_ids)
    assignable_parking_ids = _assignable_resource_ids(conn, "parking", parking_ids)
    blocked_abono_ids = [abono_id for abono_id in abono_ids if abono_id not in assignable_abono_ids]
    blocked_parking_ids = [
        parking_id for parking_id in parking_ids if parking_id not in assignable_parking_ids
    ]
    abono_ids = [abono_id for abono_id in abono_ids if abono_id in assignable_abono_ids]
    parking_ids = [
        parking_id for parking_id in parking_ids if parking_id in assignable_parking_ids
    ]

    if blocked_abono_ids:
        flash(
            f"{len(blocked_abono_ids)} abono(s) sin PDF no se han podido asignar.",
            "warning",
        )
    if blocked_parking_ids:
        flash(
            f"{len(blocked_parking_ids)} parking(s) sin PDF no se han podido asignar.",
            "warning",
        )
    if not abono_ids and not parking_ids:
        conn.close()
        flash(
            "Los recursos seleccionados no tienen PDF asociado y no se pueden asignar.",
            "warning",
        )
        return redirect(url_for("home.partido_detalle", partido_id=partido_id))

    if request.form.get("crear_cliente"):
        _create_cliente_from_request(conn)
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
                    inserted = conn.execute(
                        """
                        INSERT INTO asignaciones_abonos (id_cliente, id_partido, abono_id, asignador)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (id_partido, abono_id) DO NOTHING
                        """,
                        (cliente_id, partido_id, abono_id, g.current_user["username"]),
                    )
                    if inserted.rowcount:
                        asignados += 1
                    else:
                        repetidos += 1
                for parking_id in parking_ids:
                    inserted = conn.execute(
                        """
                        INSERT INTO asignaciones_parkings (id_cliente, id_partido, parking_id, asignador)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (id_partido, parking_id) DO NOTHING
                        """,
                        (cliente_id, partido_id, parking_id, g.current_user["username"]),
                    )
                    if inserted.rowcount:
                        asignados += 1
                    else:
                        repetidos += 1
                conn.commit()
                if asignados:
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

    if conn is not None:
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
