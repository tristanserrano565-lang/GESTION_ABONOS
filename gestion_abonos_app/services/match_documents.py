"""Carga atómica de entradas por partido con asociación a abonos y parkings."""
from __future__ import annotations

import time

from .. import config
from .pdf_documents import PdfValidationError, validate_pdf_upload


def store_match_documents(conn, partido_id: int, uploads: list, resource_keys: list[str], username: str) -> tuple[int, int]:
    """Valida el lote completo y guarda entradas nuevas; el llamador confirma la transacción.

    No sustituye PDFs existentes: los envíos conservan siempre el mismo binario.
    Una carga repetida del mismo contenido para el mismo recurso es inocua.
    """
    if not uploads or len(uploads) > config.MAX_PDF_BATCH_FILES:
        raise PdfValidationError(f"Selecciona entre 1 y {config.MAX_PDF_BATCH_FILES} PDFs.")
    if len(uploads) != len(resource_keys):
        raise PdfValidationError("Selecciona un recurso para cada PDF.")
    parsed_ids = []
    for value in resource_keys:
        kind, value = value.split(":", 1) if ":" in value else ("abono", value)
        if kind not in ("abono", "parking"):
            raise PdfValidationError("Tipo de recurso inválido.")
        if not value.isascii() or not value.isdecimal() or len(value) > 10 or int(value) < 1:
            raise PdfValidationError("Selecciona un recurso válido para cada PDF.")
        parsed_ids.append((kind, int(value)))
    if len(set(parsed_ids)) != len(parsed_ids):
        raise PdfValidationError("Un recurso no puede aparecer dos veces en el mismo lote.")
    partido = conn.execute("SELECT localia FROM partidos WHERE id = ?", (partido_id,)).fetchone()
    if not partido or not partido["localia"]:
        raise PdfValidationError("Solo se pueden subir entradas para partidos en casa.")

    prepared = []
    total_bytes = 0
    unchanged = 0
    hashes = set()
    for index, (upload, (kind, resource_id)) in enumerate(zip(uploads, parsed_ids), 1):
        table, column = ("abonos", "abono_id") if kind == "abono" else ("parkings", "parking_id")
        resource = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (resource_id,)).fetchone()
        if not resource:
            raise PdfValidationError(f"PDF {index}: el recurso seleccionado no existe.")
        try:
            document = validate_pdf_upload(upload, required=True)
        except PdfValidationError as exc:
            raise PdfValidationError(f"PDF {index}: {exc}") from exc
        total_bytes += document["byte_size"]
        if total_bytes > config.MAX_PDF_BATCH_BYTES:
            raise PdfValidationError("El lote supera el tamaño total permitido.")
        digest = document["content_sha256"]
        if digest in hashes:
            raise PdfValidationError(f"PDF {index}: el mismo archivo está asociado a varios recursos.")
        hashes.add(digest)
        existing = conn.execute(
            f"SELECT content_sha256 FROM documentos_pdf WHERE partido_id = ? AND {column} = ?",
            (partido_id, resource_id),
        ).fetchone()
        if existing:
            if existing["content_sha256"] != digest:
                raise PdfValidationError(f"PDF {index}: este recurso ya tiene otra entrada para el partido. No se ha sustituido.")
            unchanged += 1
            continue
        duplicate = conn.execute(
            "SELECT 1 FROM documentos_pdf WHERE partido_id = ? AND content_sha256 = ?",
            (partido_id, digest),
        ).fetchone()
        if duplicate:
            raise PdfValidationError(f"PDF {index}: este archivo ya pertenece a otro recurso del partido.")
        prepared.append((column, resource, document))

    now_ts = int(time.time())
    for column, resource, document in prepared:
        filename = document["original_name"]
        conn.execute(
            f"""INSERT INTO documentos_pdf (
                {column}, partido_id, filename, content_type, byte_size,
                content_sha256, pdf_data, uploaded_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (resource["id"], partido_id, filename, document["content_type"],
             document["byte_size"], document["content_sha256"], document["data"],
             username, now_ts, now_ts),
        )
    return len(prepared), unchanged


def edit_match_document(conn, partido_id: int, document_id: int, target_resource_id: int | None) -> None:
    """Revincula o borra una entrada libre sin envíos, dentro del bloqueo del partido.

    El llamador mantiene el bloqueo de envíos y confirma la transacción.
    Las asignaciones toman un bloqueo compartido del PDF antes de persistir.
    """
    lock = " FOR UPDATE" if conn.conn.dialect.name == "postgresql" else ""
    document = conn.execute(
        "SELECT id, abono_id, parking_id FROM documentos_pdf WHERE id = ? AND partido_id = ?" + lock,
        (document_id, partido_id),
    ).fetchone()
    if document is None:
        raise PdfValidationError("La entrada no existe en este partido. Recarga la página.")
    table, column = ("abonos", "abono_id") if document["abono_id"] is not None else ("parkings", "parking_id")
    if conn.execute("SELECT 1 FROM envios_email WHERE documento_pdf_id = ?", (document_id,)).fetchone():
        raise PdfValidationError("Este PDF tiene registros de envío y debe conservarse para mantener la trazabilidad.")
    if conn.execute(
        f"SELECT 1 FROM asignaciones_{table} WHERE id_partido = ? AND {column} = ?",
        (partido_id, document[column]),
    ).fetchone():
        raise PdfValidationError("Libera primero el recurso asignado para modificar o borrar su entrada.")
    if target_resource_id is None:
        conn.execute("DELETE FROM documentos_pdf WHERE id = ? AND partido_id = ?", (document_id, partido_id))
        return
    target = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (target_resource_id,)).fetchone()
    if target is None:
        raise PdfValidationError("El recurso de destino no existe.")
    if target_resource_id == document[column]:
        raise PdfValidationError("Selecciona un recurso diferente.")
    if conn.execute(
        f"SELECT 1 FROM documentos_pdf WHERE partido_id = ? AND {column} = ?",
        (partido_id, target_resource_id),
    ).fetchone():
        raise PdfValidationError("El recurso de destino ya tiene una entrada para este partido.")
    if conn.execute(
        f"SELECT 1 FROM asignaciones_{table} WHERE id_partido = ? AND {column} = ?",
        (partido_id, target_resource_id),
    ).fetchone():
        raise PdfValidationError("El recurso de destino está asignado. Libéralo antes de vincular esta entrada.")
    conn.execute(
        f"UPDATE documentos_pdf SET {column} = ?, updated_at = ? WHERE id = ? AND partido_id = ?",
        (target_resource_id, int(time.time()), document_id, partido_id),
    )
