"""Late Session Analysis — Web Dashboard.

Usage:
    python -m web.app          # production
    python -m web.app --debug  # development with reload
"""

from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path
from flask import Flask
from dotenv import load_dotenv

from web.db import init_db


def create_app() -> Flask:
    """Flask application factory."""
    _project_root = Path(__file__).resolve().parent.parent
    load_dotenv(_project_root / ".env")

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # ── config ──────────────────────────────────────────────
    # use a persistent directory so SQLite survives restarts
    instance_dir = Path(os.getenv("WEB_INSTANCE_DIR", str(_project_root / "web_instance")))
    instance_dir.mkdir(parents=True, exist_ok=True)
    db_path = instance_dir / "dashboard.db"

    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-production")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ── custom filters ─────────────────────────────────────
    import json as _json

    @app.template_filter("from_json")
    def from_json(value):
        try:
            return _json.loads(value) if isinstance(value, str) else value
        except (TypeError, _json.JSONDecodeError):
            return {}

    @app.template_filter("fmt")
    def fmt_filter(value, spec=".2f"):
        """Format a number: {{ 3.14159 | fmt('.2f') }} → '3.14'."""
        try:
            return format(float(value), spec)
        except (ValueError, TypeError):
            return str(value)

    # ── db ──────────────────────────────────────────────────
    init_db(app)

    # ── routes ──────────────────────────────────────────────
    from web.routes.dashboard import bp as dashboard_bp
    from web.routes.config_thresholds import bp as config_bp
    from web.routes.config_api import bp as api_bp
    from web.routes.pipeline import bp as pipeline_bp
    from web.routes.recommendations import bp as recs_bp
    from web.routes.simulation import bp as sim_bp
    from web.routes.backtest import bp as backtest_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(config_bp, url_prefix="/config")
    app.register_blueprint(api_bp, url_prefix="/config/api")
    app.register_blueprint(pipeline_bp, url_prefix="/pipeline")
    app.register_blueprint(recs_bp, url_prefix="/recommendations")
    app.register_blueprint(sim_bp, url_prefix="/simulation")
    app.register_blueprint(backtest_bp, url_prefix="/backtest")

    # ── scheduler (delayed import to avoid circular) ────────
    with app.app_context():
        from web.scheduler import init_scheduler
        init_scheduler(app)

    return app


# ── entry point (python -m web.app) ────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Late Session Web Dashboard")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=5000, help="Bind port")
    args = parser.parse_args()

    # add project root to path so existing imports work
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    app = create_app()

    if args.debug:
        app.config["DEBUG"] = True

    app.logger.info(f"Starting Late Session Dashboard on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
