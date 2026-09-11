#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
from pathlib import Path
import secrets


SECRET_NAMES = (
    "postgres_admin_password",
    "app_db_password",
    "n8n_db_password",
    "n8n_mailer_password",
    "flask_secret_key",
    "n8n_webhook_secret",
    "n8n_encryption_key",
    "n8n_runner_auth_token",
)


def write_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="ascii")
    # Compose monta archivos locales sin poder cambiar uid/gid/modo. El directorio
    # 0700 restringe el acceso en el host y 0444 permite leerlos a usuarios no root
    # dentro de los contenedores, que reciben cada secreto de forma individual.
    path.chmod(0o444)


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera secretos locales para Compose.")
    parser.add_argument("--directory", default="deploy/secrets", type=Path)
    args = parser.parse_args()
    target = args.directory.resolve()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.chmod(0o700)

    expected = [target / name for name in (*SECRET_NAMES, "default_admin_hash", "default_admin_salt", "api_football_key")]
    existing = [path.name for path in expected if path.exists()]
    if existing:
        parser.error("no se sobrescriben secretos existentes: " + ", ".join(existing))

    password = getpass.getpass("Contraseña inicial del administrador de la app: ")
    confirmation = getpass.getpass("Repite la contraseña: ")
    if password != confirmation:
        parser.error("las contraseñas no coinciden")
    if len(password) < 12 or len(password) > 128:
        parser.error("la contraseña debe tener entre 12 y 128 caracteres")
    api_key = getpass.getpass("API-Football key (Enter para dejarla vacía): ").strip()

    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000, dklen=32)
    for name in SECRET_NAMES:
        write_secret(target / name, secrets.token_hex(32))
    write_secret(target / "default_admin_hash", base64.urlsafe_b64encode(password_hash).decode("ascii"))
    write_secret(target / "default_admin_salt", base64.urlsafe_b64encode(salt).decode("ascii"))
    write_secret(target / "api_football_key", api_key)
    print(f"Secretos creados en {target}. No los añadas a Git.")


if __name__ == "__main__":
    main()
