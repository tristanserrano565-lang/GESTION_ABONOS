from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePath
from typing import Any, Optional

from .. import config

ALLOWED_PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
PDF_HEADER = b"%PDF-"
PDF_EOF_MARKER = b"%%EOF"
PDF_TAIL_SCAN_BYTES = 4096
SUSPICIOUS_PDF_MARKERS = (
    b"/javascript",
    b"/js",
    b"/launch",
    b"/openaction",
    b"/embeddedfile",
    b"/richmedia",
    b"/xfa",
)


class PdfValidationError(ValueError):
    """Se lanza cuando el PDF recibido no cumple las validaciones mínimas."""


def build_abono_pdf_filename(
    sector: int,
    puerta: int,
    fila: int,
    asiento: int,
) -> str:
    return f"{puerta}_{sector}_{fila}_{asiento}.pdf"


def _normalize_filename_component(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    upper_value = ascii_only.upper()
    cleaned = re.sub(r"[^A-Z0-9]+", "_", upper_value).strip("_")
    return cleaned or fallback


def build_parking_pdf_filename(nombre: str, parking_id: int) -> str:
    normalized_name = _normalize_filename_component(nombre, "PARKING")
    return f"{normalized_name}_{parking_id}.pdf"


def validate_pdf_upload(
    uploaded_file: Any,
    *,
    required: bool = False,
) -> Optional[dict[str, Any]]:
    filename = ""
    if uploaded_file is not None:
        filename = (getattr(uploaded_file, "filename", "") or "").strip()

    if not filename:
        if required:
            raise PdfValidationError("Debes adjuntar un PDF válido.")
        return None

    safe_original_name = PurePath(filename).name
    if not safe_original_name.lower().endswith(".pdf"):
        raise PdfValidationError("El archivo debe tener extensión .pdf.")

    mimetype = (getattr(uploaded_file, "mimetype", "") or "").strip().lower()
    if mimetype and mimetype not in ALLOWED_PDF_MIME_TYPES:
        raise PdfValidationError("El fichero seleccionado no parece un PDF válido.")

    stream = getattr(uploaded_file, "stream", None)
    if stream is None:
        raise PdfValidationError("No se ha podido procesar el fichero subido.")

    pdf_data = stream.read(config.MAX_PDF_UPLOAD_BYTES + 1)
    if not pdf_data:
        raise PdfValidationError("El PDF no puede estar vacío.")
    if len(pdf_data) > config.MAX_PDF_UPLOAD_BYTES:
        max_mb = max(config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024), 1)
        raise PdfValidationError(
            f"El PDF supera el tamaño máximo permitido de {max_mb} MB."
        )
    if not pdf_data.startswith(PDF_HEADER):
        raise PdfValidationError("La firma del fichero no corresponde a un PDF.")
    if PDF_EOF_MARKER not in pdf_data[-PDF_TAIL_SCAN_BYTES:]:
        raise PdfValidationError("El PDF está incompleto o no tiene un cierre válido.")

    lowered = pdf_data.lower()
    if any(marker in lowered for marker in SUSPICIOUS_PDF_MARKERS):
        raise PdfValidationError(
            "El PDF contiene elementos activos o embebidos no permitidos."
        )

    return {
        "data": pdf_data,
        "byte_size": len(pdf_data),
        "content_type": "application/pdf",
        "content_sha256": hashlib.sha256(pdf_data).hexdigest(),
        "original_name": safe_original_name,
    }
