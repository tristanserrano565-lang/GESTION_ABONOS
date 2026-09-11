from __future__ import annotations

import time

from flask import (
    Blueprint,
    abort,
    current_app,
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
from ..services.match_documents import store_match_documents, edit_match_document
from ..services.partido_locks import try_acquire_partido_operation_lock, release_partido_operation_lock
from ..services.pdf_documents import PdfValidationError

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
        flash("Ya existe un cliente con ese nombre.", "warning")
        return False


def _resource_has_pdf(conn, tipo: str, recurso_id: int, partido_id: int) -> bool:
    """Comprueba el PDF del partido para abonos y el PDF fijo para parkings."""
    return recurso_id in _assignable_resource_ids(conn, tipo, [recurso_id], partido_id)


def _assignable_resource_ids(conn, tipo: str, recurso_ids: list[int], partido_id: int) -> set[int]:
    """Devuelve recursos con documento válido para la asignación solicitada."""
    if not recurso_ids:
        return set()
    column = "abono_id" if tipo == "abono" else "parking_id"
    placeholders = ", ".join("?" for _ in recurso_ids)
    scope = " AND partido_id = ?"
    params = (*recurso_ids, partido_id)
    lock = " FOR SHARE" if conn.conn.dialect.name == "postgresql" else ""
    rows = conn.execute(
        f"SELECT {column} AS recurso_id FROM documentos_pdf WHERE {column} IN ({placeholders}){scope}{lock}",
        params,
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
        LEFT JOIN documentos_pdf d ON d.abono_id = aa.abono_id AND d.partido_id = aa.id_partido
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
        LEFT JOIN documentos_pdf d ON d.parking_id = ap.parking_id AND d.partido_id = ap.id_partido
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
        LEFT JOIN documentos_pdf d ON d.abono_id = a.id AND d.partido_id = ?
        WHERE a.id NOT IN (
            SELECT abono_id FROM asignaciones_abonos WHERE id_partido = ?
        )
        ORDER BY a.puerta, a.sector, a.fila, a.asiento
        """,
        (partido_id, partido_id),
    ).fetchall()

    parkings_disponibles = conn.execute(
        """
        SELECT p.id,
               p.nombre,
               d.id AS documento_pdf_id
        FROM parkings p
        LEFT JOIN documentos_pdf d ON d.parking_id = p.id AND d.partido_id = ?
        WHERE p.id NOT IN (
            SELECT parking_id FROM asignaciones_parkings WHERE id_partido = ?
        )
        ORDER BY p.nombre
        """,
        (partido_id, partido_id),
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
            LEFT JOIN documentos_pdf d ON d.abono_id = a.id AND d.partido_id = ?
            WHERE a.id = ?
            """,
            (partido_id, recurso_id),
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
            LEFT JOIN documentos_pdf d ON d.parking_id = p.id AND d.partido_id = ?
            WHERE p.id = ?
            """,
            (partido_id, recurso_id),
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

    conn = db.get_connection()
    try:
        entradas = conn.execute(
            """SELECT a.*, 'abono' AS tipo, c.nombre AS propietario, d.id AS documento_pdf_id,
                      d.filename, d.byte_size, d.uploaded_by, d.created_at,
                      EXISTS (SELECT 1 FROM envios_email ee WHERE ee.documento_pdf_id = d.id) AS tiene_envios,
                      EXISTS (SELECT 1 FROM asignaciones_abonos aa WHERE aa.abono_id = a.id AND aa.id_partido = ?) AS esta_asignado
               FROM abonos a
               LEFT JOIN clientes c ON c.id = a.id_propietario
               LEFT JOIN documentos_pdf d ON d.abono_id = a.id AND d.partido_id = ?
               ORDER BY a.puerta, a.sector, a.fila, a.asiento""",
            (partido_id, partido_id),
        ).fetchall()
        entradas += conn.execute(
            """SELECT p.*, 'parking' AS tipo, c.nombre AS propietario,
                      d.id AS documento_pdf_id, d.filename, d.byte_size,
                      EXISTS (SELECT 1 FROM envios_email ee WHERE ee.documento_pdf_id = d.id) AS tiene_envios,
                      EXISTS (SELECT 1 FROM asignaciones_parkings ap WHERE ap.parking_id = p.id AND ap.id_partido = ?) AS esta_asignado
               FROM parkings p
               LEFT JOIN clientes c ON c.id = p.id_propietario
               LEFT JOIN documentos_pdf d ON d.parking_id = p.id AND d.partido_id = ?
               ORDER BY p.id""", (partido_id, partido_id),
        ).fetchall()
    finally:
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
        envio_resumen=envio_resumen,
        delivery_snapshot_token=delivery_snapshot_token,
        entradas=entradas,
        max_pdf_batch_files=config.MAX_PDF_BATCH_FILES,
        max_pdf_batch_bytes=config.MAX_PDF_BATCH_BYTES,
        max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
    )


@home_bp.post("/partidos/<int:partido_id>/entradas")
def subir_entradas(partido_id: int):
    """Recibe un lote de PDFs vinculados a abonos o parkings del partido."""
    _partido_or_404(partido_id)
    conn = db.get_connection()
    try:
        created, unchanged = store_match_documents(
            conn, partido_id, request.files.getlist("pdf_files"),
            request.form.getlist("recurso_ids") or request.form.getlist("abono_ids"), g.current_user["username"],
        )
        conn.commit()
        flash(f"{created} entrada(s) guardadas.", "success")
    except PdfValidationError as exc:
        conn.conn.rollback()
        flash(f"No se ha guardado el lote. {exc}", "danger")
    except IntegrityError as exc:
        conn.conn.rollback()
        code = getattr(exc.orig, "pgcode", None)
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        current_app.logger.warning("Carga PDF rechazada: sqlstate=%s restriccion=%s", code, constraint)
        if constraint in {"ck_documentos_pdf_one_resource", "idx_documentos_pdf_parking_unique", "idx_documentos_pdf_abono_unique"}:
            detail = "La estructura de la base de datos no está adaptada a entradas por partido. Debe revisarla el administrador."
        elif code == "23505":
            detail = "Hay un conflicto de datos duplicados. Recarga el partido y comprueba los PDFs y sus vinculaciones."
        elif code == "23503":
            detail = "Un recurso, partido o usuario ya no está disponible. Recarga la página."
        else:
            detail = "Los datos incumplen una restricción de la base de datos. Debe revisarla el administrador."
        flash(f"No se ha guardado el lote. {detail}", "warning")
    finally:
        conn.close()
    return redirect(url_for("home.partido_detalle", partido_id=partido_id))


@home_bp.post("/partidos/<int:partido_id>/entradas/<int:document_id>/editar")
def editar_entrada(partido_id: int, document_id: int):
    """Modifica una entrada del partido autenticado sin interferir con sus envíos."""
    _partido_or_404(partido_id)
    action = request.form.get("accion")
    target_id = None
    if action == "vincular":
        raw_id = request.form.get("recurso_id", request.form.get("abono_id", ""))
        if not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 10 or int(raw_id) < 1:
            flash("Selecciona un recurso de destino válido.", "warning")
            return redirect(url_for("home.partido_detalle", partido_id=partido_id))
        target_id = int(raw_id)
    elif action != "borrar":
        abort(400)

    conn = db.get_connection()
    acquired = False
    try:
        acquired = try_acquire_partido_operation_lock(conn, partido_id)
        if not acquired:
            raise PdfValidationError("Hay un envío u otra modificación en curso. Inténtalo cuando termine.")
        edit_match_document(conn, partido_id, document_id, target_id)
        conn.commit()
        current_app.logger.info(
            "Entrada modificada: partido=%s documento=%s accion=%s recurso_destino=%s gestor=%s",
            partido_id, document_id, action, target_id, g.current_user["username"],
        )
        flash("PDF borrado correctamente." if action == "borrar" else "Vinculación del PDF actualizada.", "success")
    except PdfValidationError as exc:
        conn.conn.rollback()
        flash(str(exc), "warning")
    except IntegrityError:
        conn.conn.rollback()
        flash("La entrada ha cambiado o el recurso ya tiene PDF. Recarga el partido.", "warning")
    finally:
        try:
            if acquired:
                conn.conn.rollback()
                release_partido_operation_lock(conn, partido_id)
        finally:
            conn.close()
    return redirect(url_for("home.partido_detalle", partido_id=partido_id))


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
        cancelled_delivery_rows = 0
        deleted = conn.execute(
            "DELETE FROM asignaciones_abonos WHERE id_partido = ? AND abono_id = ?",
            (partido_id, abono_id),
        )
        if deleted.rowcount:
            cancelled_delivery_rows = _cancel_pending_deliveries_for_resource(
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
                "Abono liberado correctamente. Se conserva su historial de envío. Reasignarlo al mismo destinatario no vuelve a enviar el mismo PDF.",
                "warning",
            )
        elif cancelled_delivery_rows:
            flash(
                "Abono liberado correctamente y envíos pendientes cancelados. Se conserva el historial.",
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
        cancelled_delivery_rows = 0
        deleted = conn.execute(
            "DELETE FROM asignaciones_parkings WHERE id_partido = ? AND parking_id = ?",
            (partido_id, parking_id),
        )
        if deleted.rowcount:
            cancelled_delivery_rows = _cancel_pending_deliveries_for_resource(
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
                "Parking liberado correctamente. Se conserva su historial de envío. Reasignarlo al mismo destinatario no vuelve a enviar el mismo PDF.",
                "warning",
            )
        elif cancelled_delivery_rows:
            flash(
                "Parking liberado correctamente y envíos pendientes cancelados. Se conserva el historial.",
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
            LEFT JOIN documentos_pdf d ON d.abono_id = aa.abono_id AND d.partido_id = aa.id_partido
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
            LEFT JOIN documentos_pdf d ON d.parking_id = ap.parking_id AND d.partido_id = ap.id_partido
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


def _cancel_pending_deliveries_for_resource(conn, tipo: str, partido_id: int, recurso_id: int) -> int:
    deleted = conn.execute(
        """
        UPDATE envios_email SET estado = 'cancelled', ultimo_error = 'Asignacion liberada antes del envio.'
        WHERE estado = 'pending' AND partido_id = ?
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
    if not _resource_has_pdf(conn, "abono", abono_id, partido_id):
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
    if not _resource_has_pdf(conn, "parking", parking_id, partido_id):
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
    assignable_abono_ids = _assignable_resource_ids(conn, "abono", abono_ids, partido_id)
    assignable_parking_ids = _assignable_resource_ids(conn, "parking", parking_ids, partido_id)
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
