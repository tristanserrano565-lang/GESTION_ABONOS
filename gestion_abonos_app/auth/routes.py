from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
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
    session,
    url_for,
)

from .. import config, db
from . import rate_limit
from .security import (
    consume_dummy_password_check,
    hash_password,
    password_policy_error,
    verify_password,
)

auth_bp = Blueprint("auth", __name__)

MAX_USERNAME_LENGTH = 64
MAX_PASSWORD_LENGTH = 128

LOGIN_EXEMPT = {
    "auth.login",
    "auth.logout",
    "healthz",
    "robots_txt",
    "static",
}


def _is_safe_url(target: str) -> bool:
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


def _get_user_by(value, *, active_only: bool = False):
    conn = db.get_connection()
    try:
        statement = select(db.usuarios).where(db.usuarios.c.username == value)
        if active_only:
            statement = statement.where(db.usuarios.c.active.is_(True))
        return conn.conn.execute(statement).mappings().fetchone()
    finally:
        conn.close()


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_session_token(token) -> bool:
    return isinstance(token, str) and 20 <= len(token) <= 128


def _revoke_current_session() -> None:
    """Revoca en servidor la sesión representada por la cookie actual."""
    token = session.get("session_token")
    if not _valid_session_token(token):
        return
    conn = db.get_connection()
    try:
        conn.execute(
            "DELETE FROM user_sessions WHERE session_hash = ?",
            (_session_token_hash(token),),
        )
        conn.commit()
    finally:
        conn.close()


def _start_authenticated_session(user) -> bool:
    """Crea una sesión aleatoria y revoca las anteriores de la misma cuenta."""
    now_ts = int(time.time())
    token = secrets.token_urlsafe(32)
    conn = db.get_connection()
    try:
        current_user = conn.execute(
            """
            SELECT password_hash, salt, active
            FROM usuarios WHERE username = ? FOR UPDATE
            """,
            (user["username"],),
        ).fetchone()
        if (
            current_user is None
            or not current_user["active"]
            or current_user["password_hash"] != user["password_hash"]
            or current_user["salt"] != user["salt"]
        ):
            conn.conn.rollback()
            return False
        conn.execute(
            "DELETE FROM user_sessions WHERE username = ? OR expires_at <= ?",
            (user["username"], now_ts),
        )
        conn.execute(
            """
            INSERT INTO user_sessions (
                session_hash, username, created_at, last_seen_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _session_token_hash(token),
                user["username"],
                now_ts,
                now_ts,
                now_ts + config.SESSION_MAX_AGE_SECONDS,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    session.clear()
    session["session_token"] = token
    session["username"] = user["username"]
    session["role"] = user["role"]
    session["login_ts"] = now_ts
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    return True


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _invalid_login_payload(username: str, password: str) -> bool:
    if len(username) > MAX_USERNAME_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        return True
    return _has_control_chars(username) or _has_control_chars(password)


def _require_admin():
    user = g.get("current_user")
    if not user or user["role"] != "admin":
        abort(403)


def _ensure_login():
    if g.get("current_user"):
        return None
    flash("Debes iniciar sesión para continuar.", "warning")
    next_url = request.url if request.method == "GET" else None
    return redirect(url_for("auth.login", next=next_url))


def _request_ip() -> str:
    ip = (request.remote_addr or "").strip()
    return ip or "unknown"


def _login_bucket() -> str:
    return _request_ip()


def _login_wait_seconds():
    blocked, wait_seconds = rate_limit.check_limit(
        rate_limit.LOGIN_FAILURE_IP_SCOPE,
        _login_bucket(),
        config.MAX_LOGIN_ATTEMPTS,
        config.LOGIN_WINDOW_SECONDS,
    )
    if not blocked:
        return None
    return wait_seconds


def _blocked_login_response(wait_seconds: int):
    response = make_response(
        render_template("login.html", wait_seconds=wait_seconds),
        429,
    )
    response.headers["Retry-After"] = str(wait_seconds)
    return response


def _generate_csrf():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _validate_csrf():
    if request.method == "POST":
        sent = request.form.get("_csrf_token") or request.headers.get("X-CSRFToken")
        if not sent or sent != session.get("csrf_token"):
            abort(400)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("current_user"):
        return redirect(url_for("home.home_page"))

    wait_seconds = _login_wait_seconds()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        login_bucket = _login_bucket()
        if wait_seconds is not None:
            current_app.logger.warning(
                "Login bloqueado temporalmente para bucket=%s",
                login_bucket,
            )
            flash("Demasiados intentos. Espera unos minutos e intentalo de nuevo.", "danger")
            return _blocked_login_response(wait_seconds)
        blocked, wait_seconds = rate_limit.consume_limit(
            rate_limit.LOGIN_FAILURE_IP_SCOPE,
            login_bucket,
            config.MAX_LOGIN_ATTEMPTS,
            config.LOGIN_WINDOW_SECONDS,
        )
        if blocked:
            return _blocked_login_response(wait_seconds)
        invalid_payload = _invalid_login_payload(username, password)
        user = None
        password_ok = False
        if not invalid_payload:
            user = _get_user_by(username, active_only=True)
            if user:
                password_ok = verify_password(
                    password,
                    user["password_hash"],
                    user["salt"],
                )
            else:
                consume_dummy_password_check(password)
        else:
            consume_dummy_password_check(password[:MAX_PASSWORD_LENGTH])

        if not user or not password_ok:
            wait_seconds = _login_wait_seconds()
            flash("Credenciales invalidas.", "danger")
            if wait_seconds is not None:
                flash("Demasiados intentos. Espera unos minutos e intentalo de nuevo.", "warning")
                response = _blocked_login_response(wait_seconds)
                return response
        else:
            if _start_authenticated_session(user):
                rate_limit.clear_events(rate_limit.LOGIN_FAILURE_IP_SCOPE, login_bucket)
                flash(f"Bienvenido, {user['username']}.", "success")
                next_url = request.args.get("next")
                if not _is_safe_url(next_url):
                    next_url = url_for("home.home_page")
                return redirect(next_url)
            flash("La cuenta ha cambiado. Vuelve a introducir tus credenciales.", "warning")

    return render_template("login.html", wait_seconds=wait_seconds)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    _validate_csrf()
    _revoke_current_session()
    session.clear()
    flash("Sesión finalizada correctamente.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/insertar/usuario", methods=["GET", "POST"])
def insertar_usuario():
    resp = _ensure_login()
    if resp:
        return resp
    _require_admin()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "operador")
        password_error = password_policy_error(password)

        if not username or not password:
            flash("Usuario y contrasena son obligatorios.", "danger")
        elif len(username) > MAX_USERNAME_LENGTH or _has_control_chars(username):
            flash("El usuario no es válido o supera los 64 caracteres.", "danger")
        elif role not in {"admin", "operador"}:
            flash("El rol seleccionado no es válido.", "danger")
        elif password_error:
            flash(password_error, "warning")
        elif _get_user_by(username):
            flash("Ya existe un usuario con ese nombre.", "warning")
        else:
            password_hash, salt = hash_password(password)
            conn = db.get_connection()
            conn.execute(
                """
                INSERT INTO usuarios (username, password_hash, salt, role)
                VALUES (?, ?, ?, ?)
                """,
                (username, password_hash, salt, role),
            )
            conn.commit()
            conn.close()
            flash("Usuario creado correctamente.", "success")
            return redirect(url_for("auth.listar_usuarios"))

    return render_template("insertar_usuario.html")


@auth_bp.get("/administracion/usuarios")
def listar_usuarios():
    """Muestra las cuentas registradas únicamente a administradores."""
    resp = _ensure_login()
    if resp:
        return resp
    _require_admin()
    conn = db.get_connection()
    try:
        users = conn.execute(
            "SELECT username, role FROM usuarios WHERE active = true ORDER BY lower(username)"
        ).fetchall()
    finally:
        conn.close()
    return render_template("usuarios.html", usuarios=users)


@auth_bp.post("/administracion/usuarios/eliminar")
def eliminar_usuario():
    """Elimina una cuenta sin permitir perder el último admin ni su auditoría."""
    resp = _ensure_login()
    if resp:
        return resp
    _require_admin()
    username = request.form.get("username", "")
    if not username or len(username) > MAX_USERNAME_LENGTH or _has_control_chars(username):
        abort(400)
    if username == g.current_user["username"]:
        flash("No puedes eliminar tu propia cuenta.", "warning")
        return redirect(url_for("auth.listar_usuarios"))

    conn = db.get_connection()
    try:
        admins = conn.execute(
            "SELECT username FROM usuarios WHERE role = 'admin' AND active = true ORDER BY username FOR UPDATE"
        ).fetchall()
        user = conn.execute(
            "SELECT username, role FROM usuarios WHERE username = ? AND active = true FOR UPDATE",
            (username,),
        ).fetchone()
        if user is None:
            flash("El usuario indicado no existe.", "warning")
        elif user["role"] == "admin" and len(admins) <= 1:
            flash("No se puede eliminar el último administrador.", "warning")
        else:
            used = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM asignaciones_abonos WHERE asignador = ?
                    UNION ALL SELECT 1 FROM asignaciones_parkings WHERE asignador = ?
                    UNION ALL SELECT 1 FROM documentos_pdf WHERE uploaded_by = ?
                    UNION ALL SELECT 1 FROM envios_email WHERE solicitado_por = ?
                ) AS tiene_historial
                """,
                (username, username, username, username),
            ).fetchone()
            if used and used["tiene_historial"]:
                conn.execute(
                    "UPDATE usuarios SET active = false WHERE username = ?",
                    (username,),
                )
                conn.execute(
                    "DELETE FROM user_sessions WHERE username = ?",
                    (username,),
                )
                flash("Usuario desactivado y sesiones revocadas; se conserva su historial.", "success")
            else:
                conn.execute(
                    "DELETE FROM user_sessions WHERE username = ?",
                    (username,),
                )
                conn.execute("DELETE FROM usuarios WHERE username = ?", (username,))
                flash("Usuario eliminado correctamente.", "success")
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("auth.listar_usuarios"))


@auth_bp.route("/perfil/password", methods=["GET", "POST"])
def cambiar_contrasena():
    resp = _ensure_login()
    if resp:
        return resp
    user = g.current_user
    if request.method == "POST":
        actual = request.form.get("password_actual", "")
        nueva = request.form.get("password_nueva", "")
        confirma = request.form.get("password_confirmacion", "")
        if not verify_password(actual, user["password_hash"], user["salt"]):
            flash("La contraseña actual no es correcta.", "danger")
        elif not nueva or nueva != confirma:
            flash("La nueva contraseña no coincide.", "danger")
        elif password_error := password_policy_error(nueva):
            flash(password_error, "warning")
        else:
            nuevo_hash, nuevo_salt = hash_password(nueva)
            conn = db.get_connection()
            try:
                current_user = conn.execute(
                    """
                    SELECT password_hash, salt, active
                    FROM usuarios WHERE username = ? FOR UPDATE
                    """,
                    (user["username"],),
                ).fetchone()
                if (
                    current_user is None
                    or not current_user["active"]
                    or not verify_password(
                        actual,
                        current_user["password_hash"],
                        current_user["salt"],
                    )
                ):
                    conn.conn.rollback()
                    session.clear()
                    flash("La cuenta ha cambiado. Inicia sesión de nuevo.", "warning")
                    return redirect(url_for("auth.login"))
                conn.execute(
                    "UPDATE usuarios SET password_hash = ?, salt = ? WHERE username = ?",
                    (nuevo_hash, nuevo_salt, user["username"]),
                )
                conn.execute(
                    "DELETE FROM user_sessions WHERE username = ?",
                    (user["username"],),
                )
                conn.commit()
            finally:
                conn.close()
            session.clear()
            flash("Contraseña actualizada. Inicia sesión de nuevo.", "success")
            return redirect(url_for("auth.login"))

    return render_template("cambiar_contrasena.html")


def init_auth_hooks(app):
    @app.before_request
    def enforce_post_rate_limit():
        if request.method != "POST":
            return
        endpoint = request.endpoint or ""
        if endpoint.startswith("static"):
            return
        blocked, wait = rate_limit.consume_limit(
            rate_limit.POST_REQUEST_SCOPE,
            _request_ip(),
            config.POST_RATE_LIMIT_COUNT,
            config.POST_RATE_LIMIT_WINDOW_SECONDS,
        )
        if not blocked:
            return
        current_app.logger.warning(
            "POST bloqueado temporalmente para ip=%s endpoint=%s",
            _request_ip(),
            endpoint,
        )
        flash(
            f"Demasiadas peticiones. Intentalo en {wait} segundos.",
            "warning",
        )
        if endpoint == "auth.login":
            return _blocked_login_response(wait)
        target = request.referrer or url_for("home.home_page")
        response = make_response(redirect(target), 429)
        response.headers["Retry-After"] = str(wait)
        return response

    @app.before_request
    def load_logged_in_user():
        endpoint = request.endpoint or ""
        if endpoint in {"healthz", "robots_txt"} or endpoint.startswith("static"):
            g.current_user = None
            return
        username = session.get("username")
        token = session.get("session_token")
        if not username and not token:
            g.current_user = None
            return
        if not username or not _valid_session_token(token):
            session.clear()
            g.current_user = None
            return

        now_ts = int(time.time())
        session_hash = _session_token_hash(token)
        conn = db.get_connection()
        try:
            current = conn.execute(
                """
                SELECT u.username, u.password_hash, u.salt, u.role, u.active,
                       s.created_at, s.last_seen_at, s.expires_at
                FROM user_sessions s
                JOIN usuarios u ON u.username = s.username
                WHERE s.session_hash = ? AND s.username = ? AND u.active = true
                FOR UPDATE OF s
                """,
                (session_hash, username),
            ).fetchone()
            expired = bool(
                current
                and (
                    now_ts >= current["expires_at"]
                    or now_ts - current["last_seen_at"]
                    >= config.SESSION_IDLE_TIMEOUT_SECONDS
                )
            )
            if current is None or expired:
                conn.execute(
                    "DELETE FROM user_sessions WHERE session_hash = ?",
                    (session_hash,),
                )
                conn.commit()
                session.clear()
                g.current_user = None
                return
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE session_hash = ?",
                (now_ts, session_hash),
            )
            conn.commit()
            g.current_user = current
            session["role"] = current["role"]
        finally:
            conn.close()

    @app.before_request
    def enforce_csrf():
        endpoint = request.endpoint or ""
        if endpoint in {"healthz", "robots_txt"}:
            return
        _generate_csrf()
        if request.method == "POST":
            if endpoint.startswith("static"):
                return
            if endpoint != "auth.login" and g.get("current_user") is None:
                return
            _validate_csrf()

    @app.before_request
    def enforce_login():
        endpoint = request.endpoint or ""
        if endpoint.startswith("static"):
            return
        if endpoint in LOGIN_EXEMPT:
            return
        if g.get("current_user") is None:
            next_param = request.url if request.method == "GET" else None
            return redirect(url_for("auth.login", next=next_param))
