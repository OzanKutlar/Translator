"""HTTP server receiving raw chapter content from Tampermonkey."""

import logging
from flask import Flask, request, jsonify
from .pipeline import Pipeline


def _read_payload() -> dict:
    """Parse the JSON body defensively; malformed bodies become an empty dict."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def create_ingest_app(pipeline: Pipeline) -> Flask:
    app = Flask("BookIngest")

    # Suppress verbose Flask route logs to keep CLI clean
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    @app.route("/book-content", methods=["POST"])
    def book_content():
        data = _read_payload()
        content = data.get("content", "")
        content = content if isinstance(content, str) else ""
        title = data.get("title", "")
        title = title if isinstance(title, str) else ""
        is_active = bool(data.get("isActive", False))
        url = data.get("url", "")
        url = url if isinstance(url, str) else ""

        if not content.strip():
            return jsonify({"action": "wait", "retryAfter": 3, "message": "Empty content received"}), 400

        # Directive includes 'pending' and 'backlogLimit' (None while a goal is set)
        directive = pipeline.offer_page(content, is_active, title=title, url=url)
        return jsonify(directive), 200

    @app.route("/book-status", methods=["GET"])
    def book_status():
        is_active = request.args.get("isActive", "false").lower() == "true"
        directive = pipeline.poll_status(is_active)
        return jsonify(directive), 200

    return app
