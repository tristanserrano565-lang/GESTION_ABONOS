from __future__ import annotations

import base64
import os
from typing import Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 600000


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
