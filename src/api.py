"""A small, authenticated WSGI API for authorized tool runs."""

import hmac
import json
import os
import re
import sqlite3
from wsgiref.simple_server import make_server

import data.utils.database as database
import helix_integration
import holehe_integration
import maigret_integration
import sherlock_integration
import tool_runs
from tool_registry import get_tool_metadata


MAX_BODY_SIZE = 4096


def _holehe_runner(value):
    return holehe_integration.run_holehe(value)["output"]


def _tools():
    return {
        "holehe": (holehe_integration.validate_email, _holehe_runner),
        "maigret": (maigret_integration.validate_username, maigret_integration.run_maigret),
        "helix": (helix_integration.validate_username, helix_integration.run_helix),
        "sherlock": (sherlock_integration.validate_username, sherlock_integration.run_sherlock),
    }


def _response(start_response, status, payload, headers=()):
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        *headers,
    ])
    return [body]


def _error(start_response, status, code, message, headers=()):
    return _response(start_response, status, {"error": {"code": code, "message": message}}, headers)


def create_app(token):
    """Create a single-operator API; the token grants access to all runs."""
    if not isinstance(token, str) or not token or not token.isascii():
        raise ValueError("A non-empty ASCII API token is required.")
    expected = ("Bearer " + token).encode("ascii")

    def app(environ, start_response):
        authorization = environ.get("HTTP_AUTHORIZATION", "").encode("utf-8")
        if not hmac.compare_digest(authorization, expected):
            return _error(start_response, "401 Unauthorized", "unauthorized",
                          "A valid bearer token is required.", [("WWW-Authenticate", "Bearer")])

        method = environ.get("REQUEST_METHOD", "")
        path = environ.get("PATH_INFO", "")
        if path == "/runs" and method == "POST":
            if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
                return _error(start_response, "415 Unsupported Media Type", "unsupported_media_type",
                              "Content-Type must be application/json.")
            length = environ.get("CONTENT_LENGTH", "")
            if not length.isascii() or not length.isdecimal() or len(length) > 5:
                return _error(start_response, "400 Bad Request", "invalid_request",
                              "A valid Content-Length is required.")
            length = int(length)
            if length > MAX_BODY_SIZE:
                return _error(start_response, "413 Payload Too Large", "request_too_large",
                              "Request body is too large.")
            try:
                body = environ["wsgi.input"].read(length)
                if len(body) != length:
                    raise ValueError("Incomplete body")
                request = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                return _error(start_response, "400 Bad Request", "invalid_json",
                              "Request body must be valid JSON.")
            if not isinstance(request, dict) or set(request) != {"tool", "query", "authorized"}:
                return _error(start_response, "400 Bad Request", "invalid_request",
                              "Provide only tool, query, and authorized.")
            name = request["tool"]
            if not isinstance(name, str):
                return _error(start_response, "400 Bad Request", "invalid_tool", "Unknown or unsupported tool.")
            name = name.strip().lower()
            tools = _tools()
            if get_tool_metadata(name) is None or name not in tools:
                return _error(start_response, "400 Bad Request", "invalid_tool", "Unknown or unsupported tool.")
            if request["authorized"] is not True:
                return _error(start_response, "403 Forbidden", "authorization_required",
                              "Confirm that you are authorized to assess the target.")
            try:
                query = tools[name][0](request["query"])
            except ValueError as exc:
                return _error(start_response, "400 Bad Request", "invalid_query", str(exc))
            try:
                run_id, status, _ = tool_runs.execute_tool_run_with_id(name, query, tools[name][1])
            except (sqlite3.Error, RuntimeError):
                return _error(start_response, "500 Internal Server Error", "storage_error",
                              "Could not record the run.")
            except Exception:
                return _error(start_response, "500 Internal Server Error", "internal_error",
                              "Could not complete the request.")
            return _response(start_response, "201 Created", {"run_id": run_id, "status": status},
                             [("Location", f"/runs/{run_id}")])

        match = re.fullmatch(r"/runs/([1-9][0-9]{0,18})", path)
        if match and method == "GET":
            run_id = int(match.group(1))
            if run_id > 9223372036854775807:
                return _error(start_response, "404 Not Found", "not_found", "Run not found.")
            try:
                row = database.get_tool_run(run_id)
            except sqlite3.Error:
                return _error(start_response, "500 Internal Server Error", "storage_error",
                              "Could not retrieve the run.")
            except Exception:
                return _error(start_response, "500 Internal Server Error", "internal_error",
                              "Could not complete the request.")
            if row is None:
                return _error(start_response, "404 Not Found", "not_found", "Run not found.")
            keys = ("run_id", "tool", "query", "status", "result", "error", "started_at", "finished_at")
            return _response(start_response, "200 OK", dict(zip(keys, row)))
        return _error(start_response, "404 Not Found", "not_found", "Endpoint not found.")

    return app


if __name__ == "__main__":
    app = create_app(os.environ.get("OSINTBEAST_API_TOKEN"))
    database.setup_database()
    with make_server("127.0.0.1", 8000, app) as server:
        server.serve_forever()
