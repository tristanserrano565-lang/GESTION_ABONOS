from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import requests
from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from .. import config, db
from .partido_locks import (
    release_partido_operation_lock,
    try_acquire_partido_operation_lock,
)

DELIVERY_STATE_PENDING = "pending"
DELIVERY_STATE_PROCESSING = "processing"
DELIVERY_STATE_SENT = "sent"
DELIVERY_STATE_ERROR = "error"
DELIVERY_STATE_CANCELLED = "cancelled"
DELIVERY_SNAPSHOT_SALT = "partido-delivery-snapshot"


@dataclass
class DeliveryOverview:
    configured: bool
    total_assigned: int
    ready_count: int
    ready_to_send_count: int
    missing_email_count: int
    missing_pdf_count: int
    pending_count: int
    processing_count: int
    error_count: int
    sent_count: int
    last_sent_at: int | None

    @property
    def can_send(self) -> bool:
        return self.configured and (
            self.ready_to_send_count > 0
            or self.pending_count > 0
            or self.error_count > 0
            or self.processing_count > 0
        )


@dataclass
class DeliveryRunSummary:
    overview_before: DeliveryOverview
    overview_after: DeliveryOverview
    queued_new: int = 0
    already_registered: int = 0
    cancelled_obsolete: int = 0
    requeued_stale: int = 0
    reactivated_exhausted_errors: int = 0
    sent_now: int = 0
    failed_now: int = 0
    snapshot_total: int = 0
    snapshot_skipped_changed: int = 0


class DeliverySendError(RuntimeError):
    """Error controlado al enviar un documento a n8n."""


class DeliveryBusyError(DeliverySendError):
    """Se lanza cuando el partido está bloqueado por otra operación."""


def _delivery_snapshot_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(config.SECRET_KEY, salt=DELIVERY_SNAPSHOT_SALT)


def _normalize_snapshot_keys(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    snapshot_keys: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip().lower()
        if len(normalized) == 64:
            snapshot_keys.add(normalized)
    return snapshot_keys


def build_delivery_snapshot_token(partido_id: int) -> str:
    conn = db.get_connection()
    try:
        _, ready_keys = _ready_assignment_rows(conn, partido_id)
    finally:
        conn.close()

    payload = {
        "partido_id": int(partido_id),
        "keys": sorted(ready_keys),
        "generated_at": int(time.time()),
    }
    return _delivery_snapshot_serializer().dumps(payload)


def _load_delivery_snapshot_keys(snapshot_token: str, partido_id: int) -> set[str]:
    if not snapshot_token:
        raise DeliverySendError(
            "La selección de recursos a enviar ya no es válida. Recarga el detalle del partido y vuelve a intentarlo."
        )

    try:
        payload = _delivery_snapshot_serializer().loads(snapshot_token)
    except BadSignature as exc:
        raise DeliverySendError(
            "La selección de recursos a enviar no es válida o ha sido manipulada. Recarga el detalle del partido."
        ) from exc

    if not isinstance(payload, dict) or int(payload.get("partido_id") or 0) != int(partido_id):
        raise DeliverySendError(
            "La selección de recursos no corresponde con este partido. Recarga la página e inténtalo de nuevo."
        )

    return _normalize_snapshot_keys(payload.get("keys"))


def _partido_label(row: Any) -> str:
    localia = int(row["localia"] or 0)
    rival = (row["rival"] or "Rival").strip()
    local = row["equipo_local"] or (config.ATLETICO_TEAM_NAME if localia else rival)
    visitante = row["equipo_visitante"] or (
        rival if localia else config.ATLETICO_TEAM_NAME
    )
    return f"{local} vs {visitante}"


def _workflow_tipo(tipo_recurso: str) -> str:
    return "entrada" if tipo_recurso == "abono" else "parking"


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _build_idempotency_key(row: Any) -> str:
    raw_key = "|".join(
        (
            str(row["partido_id"]),
            str(row["cliente_id"]),
            _normalize_email(row["destino_email"]),
            str(row["tipo_recurso"]),
            str(row["recurso_id"]),
            str(row["documento_sha256"] or row["documento_pdf_id"]),
        )
    )
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _truncate_error(value: str, limit: int = 300) -> str:
    text_value = (value or "").strip()
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 1].rstrip() + "…"


def _current_assignment_rows(conn, partido_id: int):
    return conn.execute(
        """
        SELECT *
        FROM (
            SELECT
                aa.id_partido AS partido_id,
                aa.id_cliente AS cliente_id,
                aa.abono_id AS recurso_id,
                'abono' AS tipo_recurso,
                c.nombre AS cliente_nombre,
                TRIM(COALESCE(c.email, '')) AS destino_email,
                d.id AS documento_pdf_id,
                d.content_sha256 AS documento_sha256,
                p.rival,
                p.fecha,
                p.localia,
                p.equipo_local,
                p.equipo_visitante
            FROM asignaciones_abonos aa
            JOIN clientes c ON c.id = aa.id_cliente
            JOIN partidos p ON p.id = aa.id_partido
            LEFT JOIN documentos_pdf d ON d.abono_id = aa.abono_id AND d.partido_id = aa.id_partido
            WHERE aa.id_partido = ?

            UNION ALL

            SELECT
                ap.id_partido AS partido_id,
                ap.id_cliente AS cliente_id,
                ap.parking_id AS recurso_id,
                'parking' AS tipo_recurso,
                c.nombre AS cliente_nombre,
                TRIM(COALESCE(c.email, '')) AS destino_email,
                d.id AS documento_pdf_id,
                d.content_sha256 AS documento_sha256,
                p.rival,
                p.fecha,
                p.localia,
                p.equipo_local,
                p.equipo_visitante
            FROM asignaciones_parkings ap
            JOIN clientes c ON c.id = ap.id_cliente
            JOIN partidos p ON p.id = ap.id_partido
            LEFT JOIN documentos_pdf d ON d.parking_id = ap.parking_id AND d.partido_id = ap.id_partido
            WHERE ap.id_partido = ?
        ) AS recursos_actuales
        ORDER BY
            CASE WHEN tipo_recurso = 'abono' THEN 0 ELSE 1 END,
            cliente_id,
            recurso_id
        """,
        (partido_id, partido_id),
    ).fetchall()


def _ready_assignment_rows(conn, partido_id: int):
    ready_rows: list[tuple[Any, str]] = []
    ready_keys: set[str] = set()
    for row in _current_assignment_rows(conn, partido_id):
        email = (row["destino_email"] or "").strip()
        documento_pdf_id = row["documento_pdf_id"]
        if not email or documento_pdf_id is None:
            continue
        idempotency_key = _build_idempotency_key(row)
        ready_rows.append((row, idempotency_key))
        ready_keys.add(idempotency_key)
    return ready_rows, ready_keys


def get_partido_delivery_overview(partido_id: int) -> DeliveryOverview:
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_assigned,
                SUM(
                    CASE
                        WHEN destino_email <> '' AND documento_pdf_id IS NOT NULL THEN 1
                        ELSE 0
                    END
                ) AS ready_count,
                SUM(
                    CASE
                        WHEN destino_email <> ''
                         AND documento_pdf_id IS NOT NULL
                         AND COALESCE(estado_envio, '') <> 'sent'
                        THEN 1
                        ELSE 0
                    END
                ) AS ready_to_send_count,
                SUM(CASE WHEN destino_email = '' THEN 1 ELSE 0 END) AS missing_email_count,
                SUM(CASE WHEN documento_pdf_id IS NULL THEN 1 ELSE 0 END) AS missing_pdf_count,
                SUM(
                    CASE WHEN estado_envio = 'pending' THEN 1 ELSE 0 END
                ) AS pending_count,
                SUM(
                    CASE WHEN estado_envio = 'processing' THEN 1 ELSE 0 END
                ) AS processing_count,
                SUM(
                    CASE WHEN estado_envio = 'error' THEN 1 ELSE 0 END
                ) AS error_count,
                SUM(
                    CASE WHEN estado_envio = 'sent' THEN 1 ELSE 0 END
                ) AS sent_count,
                MAX(
                    CASE WHEN estado_envio = 'sent' THEN enviado_en ELSE NULL END
                ) AS last_sent_at
            FROM (
                SELECT
                    TRIM(COALESCE(c.email, '')) AS destino_email,
                    d.id AS documento_pdf_id,
                    ee.estado AS estado_envio,
                    ee.enviado_en AS enviado_en
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

                UNION ALL

                SELECT
                    TRIM(COALESCE(c.email, '')) AS destino_email,
                    d.id AS documento_pdf_id,
                    ee.estado AS estado_envio,
                    ee.enviado_en AS enviado_en
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
            ) AS resumen_actual
            """,
            (partido_id, partido_id),
        ).fetchone()
    finally:
        conn.close()

    return DeliveryOverview(
        configured=bool(config.N8N_WEBHOOK_URL),
        total_assigned=int(row["total_assigned"] or 0),
        ready_count=int(row["ready_count"] or 0),
        ready_to_send_count=int(row["ready_to_send_count"] or 0),
        missing_email_count=int(row["missing_email_count"] or 0),
        missing_pdf_count=int(row["missing_pdf_count"] or 0),
        pending_count=int(row["pending_count"] or 0),
        processing_count=int(row["processing_count"] or 0),
        error_count=int(row["error_count"] or 0),
        sent_count=int(row["sent_count"] or 0),
        last_sent_at=int(row["last_sent_at"]) if row["last_sent_at"] is not None else None,
    )


def _enqueue_current_assignments(
    conn,
    ready_rows: list[tuple[Any, str]],
    *,
    solicitado_por: str,
    snapshot_keys: set[str],
) -> tuple[int, int, set[str]]:
    now_ts = int(time.time())
    queued_new = 0
    already_registered = 0
    matched_snapshot_keys: set[str] = set()

    for row, idempotency_key in ready_rows:
        if idempotency_key not in snapshot_keys:
            continue
        matched_snapshot_keys.add(idempotency_key)
        email = (row["destino_email"] or "").strip()
        documento_pdf_id = row["documento_pdf_id"]
        inserted = conn.execute(
            """
            INSERT INTO envios_email (
                partido_id,
                cliente_id,
                documento_pdf_id,
                tipo_recurso,
                recurso_id,
                destino_email,
                documento_sha256,
                idempotency_key,
                estado,
                intentos,
                solicitado_por,
                solicitado_en
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                row["partido_id"],
                row["cliente_id"],
                documento_pdf_id,
                row["tipo_recurso"],
                row["recurso_id"],
                email,
                row["documento_sha256"],
                idempotency_key,
                DELIVERY_STATE_PENDING,
                0,
                solicitado_por,
                now_ts,
            ),
        )
        if inserted.rowcount:
            queued_new += 1
        else:
            already_registered += 1

    conn.commit()
    return queued_new, already_registered, matched_snapshot_keys


def _cancel_obsolete_queue_rows(conn, partido_id: int, current_keys: set[str]) -> int:
    obsolete_rows = conn.execute(
        """
        SELECT id, idempotency_key
        FROM envios_email
        WHERE partido_id = ?
          AND estado IN ('pending', 'processing', 'error')
        """,
        (partido_id,),
    ).fetchall()

    cancelled = 0
    for row in obsolete_rows:
        if row["idempotency_key"] in current_keys:
            continue
        conn.execute(
            """
            UPDATE envios_email
            SET estado = ?, ultimo_error = ?
            WHERE id = ?
            """,
            (
                DELIVERY_STATE_CANCELLED,
                "Envio descartado por cambio en asignacion, email o PDF asociado.",
                row["id"],
            ),
        )
        cancelled += 1

    if cancelled:
        conn.commit()
    return cancelled


def _requeue_stale_processing_rows(conn, partido_id: int) -> int:
    updated = conn.execute(
        """
        UPDATE envios_email
        SET estado = ?, ultimo_error = ?
        WHERE partido_id = ?
          AND estado = ?
        """,
        (
            DELIVERY_STATE_PENDING,
            "Envio reencolado tras detectar un intento interrumpido.",
            partido_id,
            DELIVERY_STATE_PROCESSING,
        ),
    )
    if updated.rowcount:
        conn.commit()
    return int(updated.rowcount or 0)


def _reactivate_exhausted_error_rows(conn, partido_id: int) -> int:
    updated = conn.execute(
        """
        UPDATE envios_email
        SET estado = ?, intentos = 0, ultimo_error = ?
        WHERE partido_id = ?
          AND estado = ?
          AND intentos >= ?
        """,
        (
            DELIVERY_STATE_PENDING,
            "Envio reactivado manualmente tras agotar intentos previos.",
            partido_id,
            DELIVERY_STATE_ERROR,
            config.EMAIL_DELIVERY_MAX_ATTEMPTS,
        ),
    )
    if updated.rowcount:
        conn.commit()
    return int(updated.rowcount or 0)


def _claim_delivery_row(conn, envio_id: int) -> bool:
    now_ts = int(time.time())
    updated = conn.execute(
        """
        UPDATE envios_email
        SET estado = ?, intentos = intentos + 1, ultimo_intento_en = ?, ultimo_error = NULL
        WHERE id = ?
          AND (
                estado = 'pending'
                OR estado = 'error'
              )
          AND (
                estado = 'error'
                OR intentos < ?
              )
        """,
        (
            DELIVERY_STATE_PROCESSING,
            now_ts,
            envio_id,
            config.EMAIL_DELIVERY_MAX_ATTEMPTS,
        ),
    )
    if updated.rowcount:
        conn.commit()
        return True
    return False


def _load_delivery_payload(conn, envio_id: int):
    return conn.execute(
        """
        SELECT
            ee.*,
            d.filename,
            d.content_type,
            d.pdf_data,
            p.rival,
            p.fecha,
            p.localia,
            p.equipo_local,
            p.equipo_visitante
        FROM envios_email ee
        JOIN documentos_pdf d ON d.id = ee.documento_pdf_id
        JOIN partidos p ON p.id = ee.partido_id
        WHERE ee.id = ?
          AND ((ee.tipo_recurso = 'abono' AND d.abono_id = ee.recurso_id AND d.partido_id = ee.partido_id)
            OR (ee.tipo_recurso = 'parking' AND d.parking_id = ee.recurso_id AND d.partido_id = ee.partido_id))
        """,
        (envio_id,),
    ).fetchone()


def _mark_delivery_sent(conn, envio_id: int) -> None:
    conn.execute(
        """
        UPDATE envios_email
        SET estado = ?, enviado_en = ?, ultimo_error = NULL
        WHERE id = ?
        """,
        (DELIVERY_STATE_SENT, int(time.time()), envio_id),
    )
    conn.commit()


def _mark_delivery_error(conn, envio_id: int, error_message: str) -> None:
    conn.execute(
        """
        UPDATE envios_email
        SET estado = ?, ultimo_error = ?
        WHERE id = ?
        """,
        (DELIVERY_STATE_ERROR, _truncate_error(error_message), envio_id),
    )
    conn.commit()


def _retryable_n8n_status(status_code: int | None) -> bool:
    if status_code is None:
        return True
    return status_code in {408, 409, 423, 425, 429, 500, 502, 503, 504}


def _response_excerpt(response: Any, limit: int = 160) -> str:
    if response is None:
        return ""
    text_value = (getattr(response, "text", None) or "").strip()
    if not text_value:
        return ""
    return _truncate_error(text_value.replace("\n", " "), limit=limit)


def _build_n8n_request_error_message(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    body_excerpt = _response_excerpt(response)

    if (
        status_code == 404
        and body_excerpt
        and "webhook" in body_excerpt.lower()
        and "not registered" in body_excerpt.lower()
    ):
        return (
            "n8n ha respondido con HTTP 404 porque el webhook no está registrado. "
            "Activa el workflow y usa su Production URL. "
            "Si estás en local con pruebas manuales, la URL de test sería /webhook-test/... "
            "pero solo sirve para una petición mientras está escuchando."
        )

    if status_code:
        message = f"n8n ha respondido con HTTP {status_code}."
        if body_excerpt:
            message += f" Detalle: {body_excerpt}"
        return message

    return "No se ha podido contactar con n8n."


def _send_to_n8n(payload: Any) -> None:
    headers = {"Accept": "application/json"}
    if config.N8N_WEBHOOK_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {config.N8N_WEBHOOK_BEARER_TOKEN}"
    if config.N8N_WEBHOOK_SECRET_HEADER and config.N8N_WEBHOOK_SECRET:
        headers[config.N8N_WEBHOOK_SECRET_HEADER] = config.N8N_WEBHOOK_SECRET

    total_attempts = max(config.N8N_WEBHOOK_MAX_RETRIES, 0) + 1
    retry_delay = max(config.N8N_WEBHOOK_RETRY_DELAY_SECONDS, 0)
    last_error: DeliverySendError | None = None

    for attempt in range(1, total_attempts + 1):
        response = None
        try:
            response = requests.post(
                config.N8N_WEBHOOK_URL,
                headers=headers,
                data={
                    "email": payload["destino_email"],
                    "partido": _partido_label(payload),
                    "tipo": _workflow_tipo(payload["tipo_recurso"]),
                    "idempotency_key": payload["idempotency_key"],
                    "recurso_id": str(payload["recurso_id"]),
                    "tipo_recurso": payload["tipo_recurso"],
                },
                files={
                    "pdf": (
                        payload["filename"],
                        payload["pdf_data"],
                        payload["content_type"] or "application/pdf",
                    )
                },
                timeout=max(config.N8N_WEBHOOK_TIMEOUT_SECONDS, 5),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            last_error = DeliverySendError(_build_n8n_request_error_message(exc))
            if attempt < total_attempts and _retryable_n8n_status(status_code):
                time.sleep(retry_delay)
                continue
            raise last_error from exc

        try:
            response_payload = response.json()
        except ValueError:
            return

        if isinstance(response_payload, dict) and response_payload.get("ok") is False:
            message = (
                response_payload.get("message")
                or response_payload.get("error")
                or "n8n ha respondido sin confirmar el envio."
            )
            last_error = DeliverySendError(_truncate_error(str(message)))
            if attempt < total_attempts:
                time.sleep(retry_delay)
                continue
            raise last_error
        return

    if last_error is not None:
        raise last_error


def send_assigned_resources_for_partido(
    partido_id: int,
    *,
    solicitado_por: str,
    snapshot_token: str,
) -> DeliveryRunSummary:
    if not config.N8N_WEBHOOK_URL:
        raise DeliverySendError(
            "N8N_WEBHOOK_URL no está configurada. No se pueden lanzar envíos."
        )

    snapshot_keys = _load_delivery_snapshot_keys(snapshot_token, partido_id)
    conn = db.get_connection()
    try:
        if not try_acquire_partido_operation_lock(conn, partido_id):
            raise DeliveryBusyError(
                "Otro usuario está modificando o enviando recursos de este partido. Inténtalo de nuevo en unos segundos."
            )

        overview_before = get_partido_delivery_overview(partido_id)
        ready_rows, current_ready_keys = _ready_assignment_rows(conn, partido_id)
        queued_new, already_registered, matched_snapshot_keys = _enqueue_current_assignments(
            conn,
            ready_rows,
            solicitado_por=solicitado_por,
            snapshot_keys=snapshot_keys,
        )
        cancelled_obsolete = _cancel_obsolete_queue_rows(conn, partido_id, current_ready_keys)
        requeued_stale = _requeue_stale_processing_rows(conn, partido_id)
        reactivated_exhausted_errors = _reactivate_exhausted_error_rows(conn, partido_id)

        candidate_ids = conn.execute(
            """
            SELECT id, tipo_recurso, solicitado_en, idempotency_key
            FROM envios_email
            WHERE partido_id = ?
              AND (
                    estado = 'error'
                    OR (estado = 'pending' AND intentos < ?)
                  )
            ORDER BY
                CASE WHEN tipo_recurso = 'abono' THEN 0 ELSE 1 END,
                solicitado_en,
                id
            """,
            (partido_id, config.EMAIL_DELIVERY_MAX_ATTEMPTS),
        ).fetchall()
        selected_candidate_ids = [
            row for row in candidate_ids if row["idempotency_key"] in matched_snapshot_keys
        ]

        sent_now = 0
        failed_now = 0

        for index, row in enumerate(selected_candidate_ids):
            envio_id = int(row["id"])
            if not _claim_delivery_row(conn, envio_id):
                continue

            payload = _load_delivery_payload(conn, envio_id)
            if payload is None:
                _mark_delivery_error(
                    conn,
                    envio_id,
                    "No se ha encontrado el PDF o el partido asociado al envío.",
                )
                failed_now += 1
                continue

            try:
                _send_to_n8n(payload)
            except DeliverySendError as exc:
                current_app.logger.warning(
                    "Error enviando %s #%s del partido %s a n8n: %s",
                    payload["tipo_recurso"],
                    payload["recurso_id"],
                    payload["partido_id"],
                    exc,
                )
                _mark_delivery_error(conn, envio_id, str(exc))
                failed_now += 1
            except Exception as exc:
                current_app.logger.exception(
                    "Error inesperado enviando %s #%s del partido %s",
                    payload["tipo_recurso"],
                    payload["recurso_id"],
                    payload["partido_id"],
                )
                _mark_delivery_error(
                    conn,
                    envio_id,
                    "Se ha producido un error inesperado durante el envio.",
                )
                failed_now += 1
            else:
                _mark_delivery_sent(conn, envio_id)
                sent_now += 1

            if (
                index < len(selected_candidate_ids) - 1
                and config.EMAIL_DELIVERY_INTER_SEND_DELAY_SECONDS > 0
            ):
                time.sleep(config.EMAIL_DELIVERY_INTER_SEND_DELAY_SECONDS)

    finally:
        release_partido_operation_lock(conn, partido_id)
        conn.close()

    overview_after = get_partido_delivery_overview(partido_id)
    return DeliveryRunSummary(
        overview_before=overview_before,
        overview_after=overview_after,
        queued_new=queued_new,
        already_registered=already_registered,
        cancelled_obsolete=cancelled_obsolete,
        requeued_stale=requeued_stale,
        reactivated_exhausted_errors=reactivated_exhausted_errors,
        sent_now=sent_now,
        failed_now=failed_now,
        snapshot_total=len(snapshot_keys),
        snapshot_skipped_changed=len(snapshot_keys - matched_snapshot_keys),
    )
