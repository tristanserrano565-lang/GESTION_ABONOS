from __future__ import annotations

from datetime import timedelta

import os
import secrets
import threading
import time

from flask import Flask, g, session

from . import config, db, filters, utils
from .blueprints.home import home_bp
from .blueprints.resources import resources_bp
from .auth import auth_bp, init_auth_hooks
from .services.matches import sync_upcoming_matches


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(config.BASE_DIR / "templates"),
        static_folder=str(config.BASE_DIR / "static"),
    )
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SERVER_INSTANCE_ID"] = secrets.token_urlsafe(16)
    cookie_secure = config.COOKIE_SECURE
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=config.SESSION_MAX_AGE_SECONDS),
    )

    db.init_db()
    filters.register_filters(app)

    app.register_blueprint(home_bp)
    app.register_blueprint(resources_bp)
    app.register_blueprint(auth_bp)
    init_auth_hooks(app)

    if config.ENABLE_BG_SYNC:
        def _sync_loop():
            try:
                sync_upcoming_matches(force=True)
            except Exception:
                pass
            while True:
                try:
                    sync_upcoming_matches(force=False)
                except Exception:
                    pass
                time.sleep(config.SYNC_INTERVAL_MINUTES * 60)

        if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            thread = threading.Thread(target=_sync_loop, daemon=True)
            thread.start()

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
