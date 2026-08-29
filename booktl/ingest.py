"""HTTP server receiving raw chapter content from Tampermonkey."""

import logging
from flask import Flask, request, jsonify
from .pipeline import Pipeline


def create_ingest_app(pipeline: Pipeline) -> Flask:
    app = Flask("BookIngest")

    # Suppress verbose Flask route logs to keep CLI clean
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    @app.route("/book-content", methods=["POST"])
    def book_content():
        data = request.json or {}
        content = data.get("content", "")
        is_active = bool(data.get("isActive", False))
        title = data.get("title", "")

        if not content.strip():
            return jsonify({"action": "wait", "retryAfter": 3, "message": "Empty content received"}), 400

        directive = pipeline.offer_page(content, is_active, title=title)
        return jsonify(directive), 200

    @app.route("/book-status", methods=["GET"])
    def book_status():
        is_active = request.args.get("isActive", "false").lower() == "true"
        directive = pipeline.poll_status(is_active)
        return jsonify(directive), 200

    return app
