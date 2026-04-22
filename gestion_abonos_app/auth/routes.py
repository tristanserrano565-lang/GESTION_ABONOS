from __future__ import annotations

import secrets
import time
from urllib.parse import urljoin, urlparse

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
from .security import hash_password, verify_password

auth_bp = Blueprint("auth", __name__)

LOGIN_EXEMPT = {
    "auth.login",
    "auth.logout",
    "static",
}


def _is_safe_url(target: str) -> bool:
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


def _get_user_by(value):
    conn = db.get_connection()
    user = conn.execute(
        "SELECT * FROM usuarios WHERE username = ?", (value,)
    ).fetchone()
    conn.close()
    return user


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


def _login_bucket(username: str) -> str:
    normalized_username = (username or "").strip().lower()[:128] or "-"
    return f"{_request_ip()}|{normalized_username}"


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

    wait_seconds = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        login_bucket = _login_bucket(username)
        blocked, wait_seconds = rate_limit.check_limit(
            rate_limit.LOGIN_FAILURE_SCOPE,
            login_bucket,
            config.MAX_LOGIN_ATTEMPTS,
            config.LOGIN_WINDOW_SECONDS,
        )
        if blocked:
            current_app.logger.warning(
                "Login bloqueado temporalmente para bucket=%s",
                login_bucket,
            )
            flash("Demasiados intentos. Espera unos minutos e intentalo de nuevo.", "danger")
            return _blocked_login_response(wait_seconds)
        user = _get_user_by(username)
        if not user or not verify_password(password, user["password_hash"], user["salt"]):
            rate_limit.record_event(rate_limit.LOGIN_FAILURE_SCOPE, login_bucket)
            blocked_after_failure, wait_after_failure = rate_limit.check_limit(
                rate_limit.LOGIN_FAILURE_SCOPE,
                login_bucket,
                config.MAX_LOGIN_ATTEMPTS,
                config.LOGIN_WINDOW_SECONDS,
            )
            if blocked_after_failure:
                wait_seconds = wait_after_failure
            flash("Credenciales invalidas.", "danger")
        else:
            rate_limit.clear_events(rate_limit.LOGIN_FAILURE_SCOPE, login_bucket)
            session.clear()
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["login_ts"] = int(time.time())
            session.permanent = True
            flash(f"Bienvenido, {user['username']}.", "success")
            next_url = request.args.get("next")
            if not _is_safe_url(next_url):
                next_url = url_for("home.home_page")
            return redirect(next_url)

    return render_template("login.html", wait_seconds=wait_seconds)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    _validate_csrf()
    session.clear()
    flash("Sesion finalizada.", "info")
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

        if not username or not password:
            flash("Usuario y contrasena son obligatorios.", "danger")
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
            return redirect(url_for("home.home_page"))

    return render_template("insertar_usuario.html")


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
        elif len(nueva) < 8:
            flash("La nueva contraseña debe tener al menos 8 caracteres.", "warning")
        else:
            nuevo_hash, nuevo_salt = hash_password(nueva)
            conn = db.get_connection()
            conn.execute(
                "UPDATE usuarios SET password_hash = ?, salt = ? WHERE username = ?",
                (nuevo_hash, nuevo_salt, user["username"]),
            )
            conn.commit()
            conn.close()
            flash("Contraseña actualizada correctamente.", "success")
            return redirect(url_for("home.home_page"))

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
        if endpoint.startswith("static"):
            g.current_user = None
            return
        if endpoint == "auth.login":
            g.current_user = None
            return
        username = session.get("username")
        login_ts = session.get("login_ts")
        if login_ts is not None:
            if time.time() - login_ts > config.SESSION_MAX_AGE_SECONDS:
                session.clear()
                g.current_user = None
                return
        g.current_user = _get_user_by(username) if username else None
        if g.current_user:
            session["role"] = g.current_user["role"]

    @app.before_request
    def enforce_csrf():
        _generate_csrf()
        if request.method == "POST":
            if (request.endpoint or "").startswith("static"):
                return
            if (request.endpoint or "") == "auth.login":
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
