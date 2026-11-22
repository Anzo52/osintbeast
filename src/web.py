import hmac
import os

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

import data.utils.database as database
from helix_integration import run_helix, validate_username as validate_helix_username
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username
from sherlock_integration import run_sherlock, validate_username as validate_sherlock_username
from tool_runs import execute_tool_run_with_id


TOOLS = {
    "holehe": (validate_email, lambda value: run_holehe(value)["output"]),
    "maigret": (validate_username, run_maigret),
    "helix": (validate_helix_username, run_helix),
    "sherlock": (validate_sherlock_username, run_sherlock),
}


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        API_TOKEN=os.environ.get("OSINTBEAST_API_TOKEN"),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if config:
        app.config.update(config)

    @app.before_request
    def require_api_token():
        token = app.config.get("API_TOKEN")
        if not token:
            return jsonify(error="Service unavailable."), 503
        scheme, separator, supplied_token = request.headers.get(
            "Authorization", ""
        ).partition(" ")
        if (
            scheme != "Bearer"
            or not separator
            or not hmac.compare_digest(supplied_token, token)
        ):
            return jsonify(error="Authentication required."), 401

    @app.post("/api/runs")
    def launch_run():
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"tool", "query"}
            or not isinstance(payload["tool"], str)
            or not isinstance(payload["query"], str)
            or payload["tool"] not in TOOLS
        ):
            return jsonify(error="Invalid request."), 400

        tool = payload["tool"]
        try:
            query_text = TOOLS[tool][0](payload["query"])
        except ValueError:
            return jsonify(error="Invalid request."), 400

        try:
            run_id, status, _ = execute_tool_run_with_id(
                tool, query_text, TOOLS[tool][1]
            )
        except Exception:
            return jsonify(error="Run could not be completed."), 500
        return jsonify(run_id=run_id, status=status), 201

    @app.get("/api/runs/<int:run_id>")
    def get_run(run_id):
        run = database.get_tool_run_status(run_id)
        if run is None:
            return jsonify(error="Not found."), 404
        public_fields = ("run_id", "tool", "status", "started_at", "finished_at")
        return jsonify({field: run[field] for field in public_fields}), 200

    @app.get("/api/results/<int:result_id>")
    def get_result(result_id):
        result = database.get_tool_result(result_id)
        if result is None:
            return jsonify(error="Not found."), 404
        public_fields = ("result_id", "tool", "result", "created_at")
        return jsonify({field: result[field] for field in public_fields}), 200

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        message = "Not found." if error.code == 404 else "Request failed."
        return jsonify(error=message), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(_error):
        return jsonify(error="Request failed."), 500

    return app
