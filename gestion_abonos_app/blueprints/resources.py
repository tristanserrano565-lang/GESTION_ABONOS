from __future__ import annotations

from collections import defaultdict
import time

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy.exc import IntegrityError

from .. import cache, db
from ..utils import format_abono, format_parking, normalize_text

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
        SELECT a.*, c.nombre AS propietario
        FROM abonos a
        LEFT JOIN clientes c ON c.id = a.id_propietario
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
        SELECT p.*, c.nombre AS propietario
        FROM parkings p
        LEFT JOIN clientes c ON c.id = p.id_propietario
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
        "SELECT id, nombre FROM clientes ORDER BY nombre"
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


@resources_bp.route("/insertar/cliente", methods=["GET", "POST"])
def insertar_cliente():
    if request.method == "POST":
        nombre = normalize_text(request.form.get("nombre"))
        if not nombre:
            flash("El nombre del cliente es obligatorio.", "danger")
        elif len(nombre) > 128:
            flash("El nombre del cliente no puede exceder los 128 caracteres.", "danger")
        else:
            conn = db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO clientes (nombre) VALUES (?)",
                    (nombre,),
                )
                conn.commit()
                flash("Cliente creado correctamente.", "success")
                return redirect(url_for("resources.listar_clientes"))
            except IntegrityError:
                conn.conn.rollback()
                flash("Ya existe un cliente con ese nombre.", "warning")
            finally:
                conn.close()
    return render_template("insertar_cliente.html")


@resources_bp.route("/insertar/abono", methods=["GET", "POST"])
def insertar_abono():
    clientes = _clientes_options()
    if request.method == "POST":
        sector = request.form.get("sector")
        puerta = request.form.get("puerta")
        fila = request.form.get("fila")
        asiento = request.form.get("asiento")
        propietario = request.form.get("id_propietario") or None

        if not sector or not puerta or not fila or not asiento:
            flash("Sector, puerta, fila y asiento son obligatorios.", "danger")
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
                    conn.commit()
                    flash("Abono registrado.", "success")
                    return redirect(url_for("resources.listar_abonos"))
                except IntegrityError:
                    conn.conn.rollback()
                    flash("Ya existe un abono con esa combinación.", "warning")
                finally:
                    conn.close()
                return render_template("insertar_abono.html", clientes=clientes)

    return render_template("insertar_abono.html", clientes=clientes)


@resources_bp.route("/insertar/parking", methods=["GET", "POST"])
def insertar_parking():
    clientes = _clientes_options()
    if request.method == "POST":
        parking_id_raw = request.form.get("parking_id")
        nombre = normalize_text(request.form.get("nombre"))
        propietario = request.form.get("id_propietario") or None

        if not parking_id_raw or not nombre:
            flash("ID y nombre del parking son obligatorios.", "danger")
        elif len(nombre) > 128 or len(parking_id_raw) > 128:
            flash("Los campos: nombre y ID del parking no pueden exceder cada uno los 128 caracteres.", "danger")
        else:
            try:
                parking_id = int(parking_id_raw)
            except (TypeError, ValueError):
                flash("El ID del parking debe ser numérico.", "danger")
                return render_template("insertar_parking.html", clientes=clientes)
            if parking_id < 1 or parking_id > 999999:
                flash("El ID del parking debe estar entre 1 y 999999.", "danger")
                return render_template("insertar_parking.html", clientes=clientes)
            conn = db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO parkings (id, nombre, id_propietario) VALUES (?, ?, ?)",
                    (parking_id, nombre, propietario),
                )
                conn.commit()
                flash("Parking registrado.", "success")
                return redirect(url_for("resources.listar_parkings"))
            except IntegrityError:
                conn.conn.rollback()
                flash("Ya existe un parking con ese ID.", "warning")
            finally:
                conn.close()

    return render_template("insertar_parking.html", clientes=clientes)


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
