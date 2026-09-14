from __future__ import annotations

import base64
import os
from typing import Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 600000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def password_policy_error(password: str) -> str | None:
    """Valida la política mínima compartida por altas y cambios de contraseña."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"La contraseña no puede superar los {MAX_PASSWORD_LENGTH} caracteres."
    if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        return "La contraseña debe incluir al menos una letra y un número."
    return None


def _derive(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def hash_password(password: str) -> Tuple[str, str]:
    salt = os.urandom(16)
    key = _derive(password, salt)
    return (
        base64.urlsafe_b64encode(key).decode("utf-8"),
        base64.urlsafe_b64encode(salt).decode("utf-8"),
    )


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    try:
        salt_bytes = base64.urlsafe_b64decode(salt)
        expected_hash = base64.urlsafe_b64decode(password_hash)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt_bytes,
            iterations=PBKDF2_ITERATIONS,
        )
        kdf.verify(password.encode("utf-8"), expected_hash)
        return True
    except Exception:
        return False


_DUMMY_PASSWORD_HASH, _DUMMY_SALT = hash_password("dummy-auth-password")


def consume_dummy_password_check(password: str) -> None:
    verify_password(password, _DUMMY_PASSWORD_HASH, _DUMMY_SALT)
