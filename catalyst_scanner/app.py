"""Flask front end for the catalyst scanner."""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from . import config
from .scanner import Scanner

log = logging.getLogger("scanner.app")


def create_app(scanner: Scanner) -> Flask:
    app = Flask(__name__)
    app.config["SCANNER"] = scanner

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            refresh_ms=config.UI_REFRESH_SECONDS * 1000,
            ttl_hours=config.ALERT_TTL_SECONDS / 3600,
            min_score=config.MIN_SCORE,
        )

    @app.route("/api/rows")
    def api_rows():
        include_filtered = request.args.get("all") == "1"
        rows = scanner.rows(include_filtered=include_filtered)
        return jsonify({
            "rows": rows,
            "count": len(rows),
            "ttl_seconds": config.ALERT_TTL_SECONDS,
            "health": scanner.health(),
        })

    @app.route("/api/health")
    def api_health():
        return jsonify(scanner.health())

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
