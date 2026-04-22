from __future__ import annotations

from collections import defaultdict
import time
from typing import Optional

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy.exc import IntegrityError

from .. import cache, db
from .. import config
from ..services.pdf_documents import (
    PdfValidationError,
    build_abono_pdf_filename,
    build_parking_pdf_filename,
    validate_pdf_upload,
)
from ..utils import (
    format_abono,
    format_parking,
    is_valid_email,
    normalize_email,
    normalize_text,
)

resources_bp = Blueprint("resources", __name__)

_CLIENTES_CACHE = {"ts": 0.0, "rows": [], "version": -1}
_CLIENTES_TTL = 60.0


def _clientes_options():
    now_ts = time.time()
    if (
        cache.cache_version("clientes") != _CLIENTES_CACHE["version"]
        or now_ts - _CLIENTES_CACHE["ts"] > _CLIENTES_TTL
    ):
        conn = db.get_connection()
        clientes = conn.execute(
            "SELECT id, nombre FROM clientes ORDER BY nombre"
        ).fetchall()
        conn.close()
        _CLIENTES_CACHE["rows"] = clientes
        _CLIENTES_CACHE["ts"] = now_ts
        _CLIENTES_CACHE["version"] = cache.cache_version("clientes")
    return _CLIENTES_CACHE["rows"]


@resources_bp.route("/abonos")
def listar_abonos():
    conn = db.get_connection()
    abonos = conn.execute(
        """
        SELECT a.*,
               c.nombre AS propietario,
               d.id AS documento_pdf_id,
               d.filename AS pdf_filename,
               d.byte_size AS pdf_byte_size
        FROM abonos a
        LEFT JOIN clientes c ON c.id = a.id_propietario
        LEFT JOIN documentos_pdf d ON d.abono_id = a.id
        ORDER BY puerta, sector, fila, asiento
        """
    ).fetchall()
    home_matches = conn.execute(
        """
        SELECT id, fecha, competicion, equipo_local, equipo_visitante, logo_local, logo_visitante
        FROM partidos
        WHERE localia = 1
          AND fecha IS NOT NULL
          AND fecha::timestamp >= now()
        ORDER BY fecha::timestamp
        """
    ).fetchall()
    asignaciones = conn.execute(
        """
        SELECT aa.abono_id,
               p.id AS partido_id,
               p.fecha,
               p.competicion,
               p.equipo_local,
               p.equipo_visitante,
               p.logo_local,
               p.logo_visitante,
               aa.asignador,
               c.nombre AS cliente
        FROM asignaciones_abonos aa
        JOIN partidos p ON p.id = aa.id_partido
        JOIN clientes c ON c.id = aa.id_cliente
        WHERE p.fecha::timestamp >= now()
        ORDER BY p.fecha::timestamp
        """
    ).fetchall()
    conn.close()

    agrupadas = defaultdict(dict)
    for asignacion in asignaciones:
        agrupadas[asignacion["abono_id"]][asignacion["partido_id"]] = asignacion

    return render_template(
        "abonos.html",
        abonos=abonos,
        asignaciones=agrupadas,
        home_matches=home_matches,
    )


@resources_bp.post("/abonos/<int:abono_id>/eliminar")
def eliminar_abono(abono_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM asignaciones_abonos WHERE abono_id = ?", (abono_id,))
    deleted = conn.execute("DELETE FROM abonos WHERE id = ?", (abono_id,))
    conn.commit()
    conn.close()
    if deleted.rowcount:
        flash("Abono eliminado junto con sus asignaciones.", "success")
    else:
        flash("El abono indicado no existe.", "warning")
    return redirect(url_for("resources.listar_abonos"))


@resources_bp.route("/parkings")
def listar_parkings():
    conn = db.get_connection()
    parkings = conn.execute(
        """
        SELECT p.*,
               c.nombre AS propietario,
               d.id AS documento_pdf_id,
               d.filename AS pdf_filename,
               d.byte_size AS pdf_byte_size
        FROM parkings p
        LEFT JOIN clientes c ON c.id = p.id_propietario
        LEFT JOIN documentos_pdf d ON d.parking_id = p.id
        ORDER BY nombre
        """
    ).fetchall()
    home_matches = conn.execute(
        """
        SELECT id, fecha, competicion, equipo_local, equipo_visitante, logo_local, logo_visitante
        FROM partidos
        WHERE localia = 1
          AND fecha IS NOT NULL
          AND fecha::timestamp >= now()
        ORDER BY fecha::timestamp
        """
    ).fetchall()
    asignaciones = conn.execute(
        """
        SELECT ap.parking_id,
               p.id AS partido_id,
               p.fecha,
               p.competicion,
               p.equipo_local,
               p.equipo_visitante,
               p.logo_local,
               p.logo_visitante,
               ap.asignador,
               c.nombre AS cliente
        FROM asignaciones_parkings ap
        JOIN partidos p ON p.id = ap.id_partido
        JOIN clientes c ON c.id = ap.id_cliente
        WHERE p.fecha::timestamp >= now()
        ORDER BY p.fecha::timestamp
        """
    ).fetchall()
    conn.close()

    agrupadas = defaultdict(dict)
    for asignacion in asignaciones:
        agrupadas[asignacion["parking_id"]][asignacion["partido_id"]] = asignacion

    return render_template(
        "parkings.html",
        parkings=parkings,
        asignaciones=agrupadas,
        home_matches=home_matches,
    )


@resources_bp.post("/parkings/<int:parking_id>/eliminar")
def eliminar_parking(parking_id: int):
    conn = db.get_connection()
    conn.execute(
        "DELETE FROM asignaciones_parkings WHERE parking_id = ?", (parking_id,)
    )
    deleted = conn.execute("DELETE FROM parkings WHERE id = ?", (parking_id,))
    conn.commit()
    conn.close()
    if deleted.rowcount:
        flash("Parking eliminado junto con sus asignaciones.", "success")
    else:
        flash("El parking indicado no existe.", "warning")
    return redirect(url_for("resources.listar_parkings"))


@resources_bp.route("/clientes")
def listar_clientes():
    conn = db.get_connection()
    clientes = conn.execute(
        "SELECT id, nombre, email FROM clientes ORDER BY nombre"
    ).fetchall()

    abonos_cliente = conn.execute(
        """
        SELECT aa.id_cliente,
               aa.abono_id,
               a.sector,
               a.puerta,
               a.fila,
               a.asiento,
               p.id AS partido_id,
               p.fecha,
               p.competicion,
               p.equipo_local,
               p.equipo_visitante,
               p.logo_local,
               p.logo_visitante
        FROM asignaciones_abonos aa
        JOIN abonos a ON a.id = aa.abono_id
        JOIN partidos p ON p.id = aa.id_partido
        WHERE p.fecha::timestamp >= now()
        """
    ).fetchall()

    parkings_cliente = conn.execute(
        """
        SELECT ap.id_cliente,
               ap.parking_id,
               pk.nombre,
               p.id AS partido_id,
               p.fecha,
               p.competicion,
               p.equipo_local,
               p.equipo_visitante,
               p.logo_local,
               p.logo_visitante
        FROM asignaciones_parkings ap
        JOIN parkings pk ON pk.id = ap.parking_id
        JOIN partidos p ON p.id = ap.id_partido
        WHERE p.fecha::timestamp >= now()
        """
    ).fetchall()
    conn.close()

    abonos_por_cliente = defaultdict(list)
    for registro in abonos_cliente:
        abonos_por_cliente[registro["id_cliente"]].append(registro)

    parkings_por_cliente = defaultdict(list)
    for registro in parkings_cliente:
        parkings_por_cliente[registro["id_cliente"]].append(registro)

    clientes_detalle = []
    for cliente in clientes:
        partidos_map: dict[int, dict] = {}
        for abono in abonos_por_cliente.get(cliente["id"], []):
            partido = partidos_map.setdefault(
                abono["partido_id"],
                {
                    "fecha": abono["fecha"],
                    "competicion": abono["competicion"],
                    "equipo_local": abono["equipo_local"],
                    "equipo_visitante": abono["equipo_visitante"],
                    "logo_local": abono["logo_local"],
                    "logo_visitante": abono["logo_visitante"],
                    "abonos": [],
                    "parkings": [],
                },
            )
            partido["abonos"].append(abono)
        for parking in parkings_por_cliente.get(cliente["id"], []):
            partido = partidos_map.setdefault(
                parking["partido_id"],
                {
                    "fecha": parking["fecha"],
                    "competicion": parking["competicion"],
                    "equipo_local": parking["equipo_local"],
                    "equipo_visitante": parking["equipo_visitante"],
                    "logo_local": parking["logo_local"],
                    "logo_visitante": parking["logo_visitante"],
                    "abonos": [],
                    "parkings": [],
                },
            )
            partido["parkings"].append(parking)

        partidos_ordenados = sorted(
            partidos_map.values(),
            key=lambda item: item["fecha"] or "",
        )
        clientes_detalle.append(
            {
                "cliente": cliente,
                "partidos": partidos_ordenados,
            }
        )

    return render_template("clientes.html", clientes=clientes_detalle)


@resources_bp.route("/partidos")
def listar_partidos():
    conn = db.get_connection()
    partidos = conn.execute(
        """
        SELECT * FROM partidos
        WHERE fecha IS NOT NULL
          AND fecha::timestamp >= now() - interval '1 day'
        ORDER BY fecha::timestamp
        """
    ).fetchall()
    conn.close()
    return render_template("partidos.html", partidos=partidos)


@resources_bp.post("/partidos/<int:partido_id>/eliminar")
def eliminar_partido(partido_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM asignaciones_abonos WHERE id_partido = ?", (partido_id,))
    conn.execute(
        "DELETE FROM asignaciones_parkings WHERE id_partido = ?", (partido_id,)
    )
    deleted = conn.execute("DELETE FROM partidos WHERE id = ?", (partido_id,))
    conn.commit()
    conn.close()
    if deleted.rowcount:
        flash("Partido eliminado.", "success")
    else:
        flash("El partido no existe.", "warning")
    return redirect(url_for("resources.listar_partidos"))


@resources_bp.post("/clientes/<int:cliente_id>/eliminar")
def eliminar_cliente(cliente_id: int):
    conn = db.get_connection()
    conn.execute(
        "DELETE FROM asignaciones_abonos WHERE id_cliente = ?",
        (cliente_id,),
    )
    conn.execute(
        "DELETE FROM asignaciones_parkings WHERE id_cliente = ?",
        (cliente_id,),
    )
    conn.execute(
        "UPDATE abonos SET id_propietario = NULL WHERE id_propietario = ?",
        (cliente_id,),
    )
    conn.execute(
        "UPDATE parkings SET id_propietario = NULL WHERE id_propietario = ?",
        (cliente_id,),
    )
    deleted = conn.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))
    conn.commit()
    conn.close()
    if deleted.rowcount:
        flash("Cliente eliminado y asignaciones liberadas.", "success")
    else:
        flash("El cliente indicado no existe.", "warning")
    return redirect(url_for("resources.listar_clientes"))


def _validate_cliente_payload(nombre: str, email: str) -> Optional[str]:
    if not nombre:
        return "El nombre del cliente es obligatorio."
    if len(nombre) > 128:
        return "El nombre del cliente no puede exceder los 128 caracteres."
    if email and len(email) > 255:
        return "El email no puede exceder los 255 caracteres."
    if email and not is_valid_email(email):
        return "Introduce un email válido."
    return None


def _parse_optional_propietario_id(
    propietario_raw: str,
    clientes,
) -> tuple[Optional[int], Optional[str]]:
    if not propietario_raw:
        return None, None
    if not propietario_raw.isdigit():
        return None, "El propietario seleccionado no es válido."
    propietario_id = int(propietario_raw)
    client_ids = {int(cliente["id"]) for cliente in clientes}
    if propietario_id not in client_ids:
        return None, "El propietario seleccionado no existe."
    return propietario_id, None


@resources_bp.route("/insertar/cliente", methods=["GET", "POST"])
def insertar_cliente():
    if request.method == "POST":
        nombre = normalize_text(request.form.get("nombre"))
        email = normalize_email(request.form.get("email"))
        validation_error = _validate_cliente_payload(nombre, email)
        if validation_error:
            flash(validation_error, "danger")
        else:
            conn = db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO clientes (nombre, email) VALUES (?, ?)",
                    (nombre, email or None),
                )
                conn.commit()
                flash("Cliente creado correctamente.", "success")
                return redirect(url_for("resources.listar_clientes"))
            except IntegrityError:
                conn.conn.rollback()
                flash("Ya existe un cliente con ese nombre o con ese email.", "warning")
            finally:
                conn.close()
    return render_template("insertar_cliente.html")


@resources_bp.route("/clientes/<int:cliente_id>/editar", methods=["GET", "POST"])
def editar_cliente(cliente_id: int):
    conn = db.get_connection()
    cliente = conn.execute(
        "SELECT id, nombre, email FROM clientes WHERE id = ?",
        (cliente_id,),
    ).fetchone()
    if cliente is None:
        conn.close()
        flash("El cliente indicado no existe.", "warning")
        return redirect(url_for("resources.listar_clientes"))

    if request.method == "POST":
        nombre = normalize_text(request.form.get("nombre"))
        email = normalize_email(request.form.get("email"))
        validation_error = _validate_cliente_payload(nombre, email)
        if validation_error:
            flash(validation_error, "danger")
        else:
            try:
                conn.execute(
                    "UPDATE clientes SET nombre = ?, email = ? WHERE id = ?",
                    (nombre, email or None, cliente_id),
                )
                conn.commit()
                flash("Cliente actualizado correctamente.", "success")
                conn.close()
                return redirect(url_for("resources.listar_clientes"))
            except IntegrityError:
                conn.conn.rollback()
                flash("Ya existe un cliente con ese nombre o con ese email.", "warning")
        cliente = {"id": cliente_id, "nombre": nombre, "email": email}
        conn.close()
        return render_template(
            "insertar_cliente.html",
            modo_edicion=True,
            cliente=cliente,
        )

    conn.close()
    return render_template(
        "insertar_cliente.html",
        modo_edicion=True,
        cliente=cliente,
    )


@resources_bp.route("/insertar/abono", methods=["GET", "POST"])
def insertar_abono():
    clientes = _clientes_options()
    if request.method == "POST":
        sector = request.form.get("sector")
        puerta = request.form.get("puerta")
        fila = request.form.get("fila")
        asiento = request.form.get("asiento")
        propietario_raw = request.form.get("id_propietario") or ""
        propietario, propietario_error = _parse_optional_propietario_id(
            propietario_raw,
            clientes,
        )
        pdf_file = request.files.get("pdf_file")

        if not sector or not puerta or not fila or not asiento:
            flash("Sector, puerta, fila y asiento son obligatorios.", "danger")
        elif propietario_error:
            flash(propietario_error, "danger")
        elif len(sector) > 128 or len(puerta) > 128 or len(fila) > 128 or len(asiento) > 128:
            flash("Los campos: sector, puerta, fila y asiento no pueden exceder cada uno los 128 caracteres.", "danger")
        else:
            limites = {
                "sector": (sector, 9999),
                "puerta": (puerta, 9999),
                "fila": (fila, 9999),
                "asiento": (asiento, 9999),
            }
            valores = {}
            for campo, (valor, maximo) in limites.items():
                try:
                    numero = int(valor)
                except (TypeError, ValueError):
                    flash(f"{campo.capitalize()} debe ser numérico.", "danger")
                    break
                if numero < 1 or numero > maximo:
                    flash(f"{campo.capitalize()} debe estar entre 1 y {maximo}.", "danger")
                else:
                    valores[campo] = numero
            else:
                try:
                    validated_pdf = validate_pdf_upload(pdf_file, required=True)
                except PdfValidationError as exc:
                    current_app.logger.warning(
                        "PDF de abono rechazado en insertar_abono: %s",
                        exc,
                    )
                    flash(str(exc), "danger")
                    return render_template(
                        "insertar_abono.html",
                        clientes=clientes,
                        max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
                        max_pdf_upload_mb=max(
                            config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024),
                            1,
                        ),
                    )
                conn = db.get_connection()
                try:
                    conn.execute(
                        """
                        INSERT INTO abonos (sector, puerta, fila, asiento, id_propietario)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            valores["sector"],
                            valores["puerta"],
                            valores["fila"],
                            valores["asiento"],
                            propietario,
                        ),
                    )
                    created_abono = conn.execute(
                        """
                        SELECT id
                        FROM abonos
                        WHERE sector = ? AND puerta = ? AND fila = ? AND asiento = ?
                        """,
                        (
                            valores["sector"],
                            valores["puerta"],
                            valores["fila"],
                            valores["asiento"],
                        ),
                    ).fetchone()
                    if validated_pdf and created_abono:
                        now_ts = int(time.time())
                        conn.execute(
                            """
                            INSERT INTO documentos_pdf (
                                abono_id,
                                filename,
                                content_type,
                                byte_size,
                                content_sha256,
                                pdf_data,
                                uploaded_by,
                                created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                created_abono["id"],
                                build_abono_pdf_filename(
                                    valores["sector"],
                                    valores["puerta"],
                                    valores["fila"],
                                    valores["asiento"],
                                ),
                                validated_pdf["content_type"],
                                validated_pdf["byte_size"],
                                validated_pdf["content_sha256"],
                                validated_pdf["data"],
                                g.current_user["username"]
                                if g.get("current_user")
                                else None,
                                now_ts,
                                now_ts,
                            ),
                        )
                    conn.commit()
                    flash("Abono registrado y PDF guardado correctamente.", "success")
                    return redirect(url_for("resources.listar_abonos"))
                except IntegrityError:
                    conn.conn.rollback()
                    flash("Ya existe un abono con esa combinación.", "warning")
                finally:
                    conn.close()
                return render_template(
                    "insertar_abono.html",
                    clientes=clientes,
                    max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
                    max_pdf_upload_mb=max(
                        config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024),
                        1,
                    ),
                )

    return render_template(
        "insertar_abono.html",
        clientes=clientes,
        max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
        max_pdf_upload_mb=max(config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024), 1),
    )


@resources_bp.route("/insertar/parking", methods=["GET", "POST"])
def insertar_parking():
    clientes = _clientes_options()
    if request.method == "POST":
        parking_id_raw = request.form.get("parking_id")
        nombre = normalize_text(request.form.get("nombre"))
        propietario_raw = request.form.get("id_propietario") or ""
        propietario, propietario_error = _parse_optional_propietario_id(
            propietario_raw,
            clientes,
        )
        pdf_file = request.files.get("pdf_file")

        if not parking_id_raw or not nombre:
            flash("ID y nombre del parking son obligatorios.", "danger")
        elif propietario_error:
            flash(propietario_error, "danger")
        elif len(nombre) > 128 or len(parking_id_raw) > 128:
            flash("Los campos: nombre y ID del parking no pueden exceder cada uno los 128 caracteres.", "danger")
        else:
            try:
                parking_id = int(parking_id_raw)
            except (TypeError, ValueError):
                flash("El ID del parking debe ser numérico.", "danger")
                return render_template(
                    "insertar_parking.html",
                    clientes=clientes,
                    max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
                    max_pdf_upload_mb=max(
                        config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024),
                        1,
                    ),
                )
            if parking_id < 1 or parking_id > 999999:
                flash("El ID del parking debe estar entre 1 y 999999.", "danger")
                return render_template(
                    "insertar_parking.html",
                    clientes=clientes,
                    max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
                    max_pdf_upload_mb=max(
                        config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024),
                        1,
                    ),
                )
            try:
                validated_pdf = validate_pdf_upload(pdf_file, required=True)
            except PdfValidationError as exc:
                current_app.logger.warning(
                    "PDF de parking rechazado en insertar_parking: %s",
                    exc,
                )
                flash(str(exc), "danger")
                return render_template(
                    "insertar_parking.html",
                    clientes=clientes,
                    max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
                    max_pdf_upload_mb=max(
                        config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024),
                        1,
                    ),
                )
            conn = db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO parkings (id, nombre, id_propietario) VALUES (?, ?, ?)",
                    (parking_id, nombre, propietario),
                )
                now_ts = int(time.time())
                conn.execute(
                    """
                    INSERT INTO documentos_pdf (
                        parking_id,
                        filename,
                        content_type,
                        byte_size,
                        content_sha256,
                        pdf_data,
                        uploaded_by,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parking_id,
                        build_parking_pdf_filename(nombre, parking_id),
                        validated_pdf["content_type"],
                        validated_pdf["byte_size"],
                        validated_pdf["content_sha256"],
                        validated_pdf["data"],
                        g.current_user["username"] if g.get("current_user") else None,
                        now_ts,
                        now_ts,
                    ),
                )
                conn.commit()
                flash("Parking registrado y PDF guardado correctamente.", "success")
                return redirect(url_for("resources.listar_parkings"))
            except IntegrityError:
                conn.conn.rollback()
                flash("Ya existe un parking con ese ID.", "warning")
            finally:
                conn.close()

    return render_template(
        "insertar_parking.html",
        clientes=clientes,
        max_pdf_upload_bytes=config.MAX_PDF_UPLOAD_BYTES,
        max_pdf_upload_mb=max(config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024), 1),
    )


@resources_bp.route("/insertar/partido", methods=["GET", "POST"])
def insertar_partido():
    if request.method == "POST":
        jornada_raw = request.form.get("jornada")
        rival = normalize_text(request.form.get("rival"))
        fecha_raw = request.form.get("fecha")
        localia = request.form.get("localia", "casa") == "casa"
        competicion = normalize_text(request.form.get("competicion"))
        estadio = normalize_text(request.form.get("estadio"))

        jornada = None
        if jornada_raw:
            try:
                jornada = int(jornada_raw)
            except ValueError:
                flash("La jornada debe ser un número.", "warning")
                jornada = None
            else:
                if jornada < 1 or jornada > 60:
                    flash("La jornada debe estar entre 1 y 60.", "warning")
                    jornada = None

        for elem in [competicion, estadio, rival]:
            if elem and len(elem) > 128:
                flash("Los campos no pueden exceder los 128 caracteres.", "danger")
                break
        from ..utils import normalize_datetime_value, build_team_names

        fecha = normalize_datetime_value(fecha_raw)
        if not rival or not fecha:
            flash("Rival y fecha son obligatorios.", "danger")
        else:
            equipo_local, equipo_visitante = build_team_names(localia, rival)
            conn = db.get_connection()
            conn.execute(
                """
                INSERT INTO partidos (
                    jornada,
                    rival,
                    fecha,
                    localia,
                    competicion,
                    estadio,
                    equipo_local,
                    equipo_visitante
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    jornada,
                    rival,
                    fecha,
                    1 if localia else 0,
                    competicion,
                    estadio,
                    equipo_local,
                    equipo_visitante,
                ),
            )
            conn.commit()
            conn.close()
            flash("Partido añadido al calendario.", "success")
            return redirect(url_for("home.home_page"))

    return render_template("insertar_partido.html")
