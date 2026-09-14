"""Validación en el origen de los tokens emitidos por Cloudflare Access."""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import Flask, g, make_response, request


ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
MAX_TOKEN_BYTES = 16 * 1024
MAX_JWKS_BYTES = 64 * 1024
MAX_CLOCK_SKEW_SECONDS = 30
MIN_REFRESH_INTERVAL_SECONDS = 30
TEAM_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$"
)
AUDIENCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class AccessConfigurationError(RuntimeError):
    """Indica que la protección se activó con una configuración insegura."""


class AccessTokenError(ValueError):
    """Indica que una petición no presenta una identidad válida de Access."""


@dataclass(frozen=True)
class AccessIdentity:
    """Identidad mínima autenticada por Cloudflare que puede usar la aplicación."""

    subject: str
    email: str


def _decode_segment(value: str) -> bytes:
    """Decodifica un segmento base64url rechazando tamaños y caracteres inválidos."""
    if not value or len(value) > MAX_TOKEN_BYTES:
        raise AccessTokenError("segmento_invalido")
    try:
        return base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise AccessTokenError("segmento_invalido") from exc


def _decode_json_segment(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_segment(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccessTokenError("json_invalido") from exc
    if not isinstance(decoded, dict):
        raise AccessTokenError("json_invalido")
    return decoded


def _integer_claim(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AccessTokenError(f"claim_{name}_invalido")
    return value


class CloudflareAccessValidator:
    """Valida JWT RS256 y mantiene en memoria las claves públicas rotables."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        cache_seconds: int = 3600,
        timeout_seconds: int = 5,
        max_token_age_seconds: int = 8 * 60 * 60,
    ) -> None:
        normalized_domain = team_domain.strip().lower().rstrip(".")
        normalized_audience = audience.strip()
        if not TEAM_DOMAIN_RE.fullmatch(normalized_domain):
            raise AccessConfigurationError(
                "CLOUDFLARE_ACCESS_TEAM_DOMAIN debe ser nombre.cloudflareaccess.com."
            )
        if not AUDIENCE_RE.fullmatch(normalized_audience):
            raise AccessConfigurationError(
                "CLOUDFLARE_ACCESS_AUD no tiene un formato válido."
            )
        if cache_seconds < 60 or timeout_seconds < 1 or max_token_age_seconds < 60:
            raise AccessConfigurationError("Los límites de Cloudflare Access no son válidos.")

        self.team_domain = normalized_domain
        self.audience = normalized_audience
        self.issuer = f"https://{normalized_domain}"
        self.jwks_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.cache_seconds = cache_seconds
        self.timeout_seconds = timeout_seconds
        self.max_token_age_seconds = max_token_age_seconds
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._cache_expires_at = 0.0
        self._last_refresh_at = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.trust_env = False

    def _load_jwks(self) -> dict[str, rsa.RSAPublicKey]:
        """Descarga un JWKS acotado por HTTPS sin redirects ni proxies de entorno."""
        try:
            with self._session.get(
                self.jwks_url,
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
                allow_redirects=False,
                stream=True,
                verify=True,
            ) as response:
                if response.status_code != 200:
                    raise AccessTokenError("jwks_no_disponible")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_JWKS_BYTES:
                        raise AccessTokenError("jwks_demasiado_grande")
        except requests.RequestException as exc:
            raise AccessTokenError("jwks_no_disponible") from exc

        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessTokenError("jwks_invalido") from exc
        raw_keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(raw_keys, list) or not raw_keys:
            raise AccessTokenError("jwks_invalido")

        parsed: dict[str, rsa.RSAPublicKey] = {}
        for item in raw_keys:
            if not isinstance(item, dict) or item.get("kty") != "RSA":
                continue
            kid = item.get("kid")
            modulus = item.get("n")
            exponent = item.get("e")
            if not all(isinstance(value, str) and value for value in (kid, modulus, exponent)):
                continue
            try:
                public_numbers = rsa.RSAPublicNumbers(
                    int.from_bytes(_decode_segment(exponent), "big"),
                    int.from_bytes(_decode_segment(modulus), "big"),
                )
                parsed[kid] = public_numbers.public_key()
            except (AccessTokenError, ValueError):
                continue
        if not parsed:
            raise AccessTokenError("jwks_sin_claves_validas")
        return parsed

    def _key_for(self, kid: str) -> rsa.RSAPublicKey:
        """Obtiene la clave por `kid`, refrescando de forma limitada ante rotaciones."""
        now = time.monotonic()
        with self._lock:
            if now < self._cache_expires_at and kid in self._keys:
                return self._keys[kid]
            needs_refresh = (
                not self._keys
                or now >= self._cache_expires_at
                or now - self._last_refresh_at >= MIN_REFRESH_INTERVAL_SECONDS
            )
            can_refresh = (
                self._last_refresh_at == 0.0
                or now - self._last_refresh_at >= MIN_REFRESH_INTERVAL_SECONDS
            )
            if needs_refresh and can_refresh:
                self._last_refresh_at = now
                self._keys = self._load_jwks()
                self._cache_expires_at = time.monotonic() + self.cache_seconds
            if now >= self._cache_expires_at:
                raise AccessTokenError("jwks_no_disponible")
            key = self._keys.get(kid)
            if key is None:
                raise AccessTokenError("kid_desconocido")
            return key

    def validate(self, token: str, *, now_ts: int | None = None) -> AccessIdentity:
        """Verifica firma, emisor, audiencia, tipo e intervalo temporal del JWT."""
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise AccessTokenError("token_ausente_o_grande")
        parts = token.split(".")
        if len(parts) != 3:
            raise AccessTokenError("token_invalido")
        header = _decode_json_segment(parts[0])
        payload = _decode_json_segment(parts[1])
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            raise AccessTokenError("cabecera_invalida")

        signature = _decode_segment(parts[2])
        try:
            self._key_for(kid).verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise AccessTokenError("firma_invalida") from exc

        current_ts = int(time.time()) if now_ts is None else int(now_ts)
        issued_at = _integer_claim(payload, "iat")
        not_before = _integer_claim(payload, "nbf")
        expires_at = _integer_claim(payload, "exp")
        if issued_at > current_ts + MAX_CLOCK_SKEW_SECONDS:
            raise AccessTokenError("token_futuro")
        if not_before > current_ts + MAX_CLOCK_SKEW_SECONDS:
            raise AccessTokenError("token_no_activo")
        if expires_at <= current_ts - MAX_CLOCK_SKEW_SECONDS:
            raise AccessTokenError("token_caducado")
        if expires_at <= issued_at or expires_at - issued_at > self.max_token_age_seconds:
            raise AccessTokenError("duracion_invalida")
        if payload.get("iss") != self.issuer or payload.get("type") != "app":
            raise AccessTokenError("origen_invalido")

        audience = payload.get("aud")
        valid_audience = audience == self.audience or (
            isinstance(audience, list)
            and all(isinstance(item, str) for item in audience)
            and self.audience in audience
        )
        if not valid_audience:
            raise AccessTokenError("audiencia_invalida")

        subject = payload.get("sub")
        email = payload.get("email")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise AccessTokenError("identidad_invalida")
        if (
            not isinstance(email, str)
            or not email
            or len(email) > 320
            or "@" not in email
        ):
            raise AccessTokenError("email_invalido")
        return AccessIdentity(subject=subject, email=email.lower())


def register_cloudflare_access(
    app: Flask,
    validator: CloudflareAccessValidator | None,
) -> None:
    """Registra la barrera de Access antes de autenticación y lógica de negocio."""

    @app.before_request
    def enforce_cloudflare_access():
        if validator is None or request.endpoint == "healthz":
            return None
        try:
            g.cloudflare_access_identity = validator.validate(
                request.headers.get(ACCESS_HEADER, "")
            )
        except AccessTokenError:
            response = make_response("Acceso denegado.", 403)
            response.headers["Cache-Control"] = "no-store"
            return response
        return None
