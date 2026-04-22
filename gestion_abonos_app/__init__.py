from __future__ import annotations

from datetime import timedelta

from flask import Flask, flash, g, redirect, request, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config, db, filters, utils
from .blueprints.home import home_bp
from .blueprints.resources import resources_bp
from .auth import auth_bp, init_auth_hooks


def _build_content_security_policy() -> str:
    directives = {
        "default-src": ["'self'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
        "object-src": ["'none'"],
        "script-src": ["'self'", "https://cdn.jsdelivr.net"],
        "style-src": [
            "'self'",
            "'unsafe-inline'",
            "https://cdn.jsdelivr.net",
            "https://fonts.googleapis.com",
        ],
        "font-src": ["'self'", "data:", "https://fonts.gstatic.com"],
        "img-src": ["'self'", "data:", "https:"],
        "connect-src": ["'self'"],
    }
    return "; ".join(
        f"{directive} {' '.join(sources)}"
        for directive, sources in directives.items()
    )


def create_app() -> Flask:
    if not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY no está configurada.")

    app = Flask(
        __name__,
        template_folder=str(config.BASE_DIR / "templates"),
        static_folder=str(config.BASE_DIR / "static"),
    )
    proxy_fix_enabled = any(
        value > 0
        for value in (
            config.PROXY_FIX_X_FOR,
            config.PROXY_FIX_X_PROTO,
            config.PROXY_FIX_X_HOST,
            config.PROXY_FIX_X_PORT,
            config.PROXY_FIX_X_PREFIX,
        )
    )
    if proxy_fix_enabled:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=config.PROXY_FIX_X_FOR,
            x_proto=config.PROXY_FIX_X_PROTO,
            x_host=config.PROXY_FIX_X_HOST,
            x_port=config.PROXY_FIX_X_PORT,
            x_prefix=config.PROXY_FIX_X_PREFIX,
        )

    app.config["SECRET_KEY"] = config.SECRET_KEY
    cookie_secure = config.COOKIE_SECURE
    app.config.update(
        MAX_CONTENT_LENGTH=config.MAX_PDF_UPLOAD_BYTES + (256 * 1024),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=config.SESSION_COOKIE_SAMESITE,
        SESSION_COOKIE_SECURE=cookie_secure,
        SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=config.SESSION_MAX_AGE_SECONDS),
        PREFERRED_URL_SCHEME="https" if cookie_secure or config.FORCE_HTTPS else "http",
    )

    db.init_db()
    filters.register_filters(app)

    app.register_blueprint(home_bp)
    app.register_blueprint(resources_bp)
    app.register_blueprint(auth_bp)
    init_auth_hooks(app)

    @app.before_request
    def enforce_https():
        if not config.FORCE_HTTPS:
            return None
        if request.is_secure:
            return None
        if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return None
        return redirect(request.url.replace("http://", "https://", 1), code=301)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(_exc):
        max_mb = max(config.MAX_PDF_UPLOAD_BYTES // (1024 * 1024), 1)
        flash(
            f"El archivo supera el tamaño máximo permitido de {max_mb} MB.",
            "danger",
        )
        target = request.referrer or url_for("home.home_page")
        return redirect(target), 413

    @app.after_request
    def apply_security_headers(response):
        if config.ENABLE_SECURITY_HEADERS:
            response.headers.setdefault(
                "Content-Security-Policy",
                _build_content_security_policy(),
            )
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault(
                "Referrer-Policy",
                "strict-origin-when-cross-origin",
            )
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), geolocation=(), microphone=()",
            )
            response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
            response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
            if request.is_secure:
                response.headers.setdefault(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )

        if (
            response.mimetype == "text/html"
            and (request.endpoint or "") != "static"
            and (g.get("current_user") is not None or request.endpoint == "auth.login")
        ):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, private, max-age=0"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response

    @app.context_processor
    def inject_globals():
        return {
            "ATLETICO_TEAM_NAME": config.ATLETICO_TEAM_NAME,
            "format_abono": utils.format_abono,
            "format_parking": utils.format_parking,
            "competition_theme": utils.competition_theme,
            "current_user": getattr(g, "current_user", None),
            "csrf_token": lambda: getattr(g, "csrf_token", None) or session.get("csrf_token"),
        }

    return app
