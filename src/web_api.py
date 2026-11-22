import hmac
import ipaddress
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import data.utils.database as database
from helix_integration import run_helix, validate_username as validate_helix_username
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username
from sherlock_integration import (
    run_sherlock,
    validate_username as validate_sherlock_username,
)
from tool_runs import UnexpectedToolError, execute_tool_run

MAX_REQUEST_BYTES = 16 * 1024
MAX_TOKEN_LENGTH = 256
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_LOG = logging.getLogger(__name__)
RUNNERS = {
    "holehe": (validate_email, lambda value: run_holehe(value)["output"]),
    "maigret": (validate_username, run_maigret),
    "helix": (validate_helix_username, run_helix),
    "sherlock": (validate_sherlock_username, run_sherlock),
}


def _validate_tokens(tokens):
    if not isinstance(tokens, dict) or not tokens:
        raise ValueError("OSINTBEAST_API_TOKENS must be a non-empty JSON object.")
    if any(
        not isinstance(token, str)
        or len(token) > MAX_TOKEN_LENGTH
        or not TOKEN_PATTERN.fullmatch(token)
        or not isinstance(role, str)
        or role not in {"operator", "viewer"}
        for token, role in tokens.items()
    ):
        raise ValueError(
            "API tokens must be 32-256 URL-safe characters with an "
            "'operator' or 'viewer' role."
        )
    return dict(tokens)


def load_tokens():
    raw_tokens = os.environ.get("OSINTBEAST_API_TOKENS")
    if not raw_tokens:
        raise ValueError("OSINTBEAST_API_TOKENS must be configured.")
    try:
        tokens = json.loads(raw_tokens)
    except json.JSONDecodeError as exc:
        raise ValueError("OSINTBEAST_API_TOKENS must contain valid JSON.") from exc
    return _validate_tokens(tokens)


def _authorized_role(authorization, tokens):
    if not isinstance(authorization, str):
        return None
    scheme, separator, supplied_token = authorization.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not supplied_token
        or " " in supplied_token
        or len(supplied_token) > MAX_TOKEN_LENGTH
    ):
        return None
    supplied_bytes = supplied_token.encode("utf-8")
    role = None
    for token, configured_role in tokens.items():
        if hmac.compare_digest(supplied_bytes, token.encode("ascii")):
            role = configured_role
    return role


def _make_handler(tokens):
    class ApiHandler(BaseHTTPRequestHandler):
        # Bound idle socket reads, including headers read before authentication.
        timeout = 10

        def _send_json(
            self, status, payload, authenticate=False, close_connection=False
        ):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if authenticate:
                self.send_header("WWW-Authenticate", "Bearer")
            if close_connection:
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _require_role(self, allowed_roles):
            role = _authorized_role(self.headers.get("Authorization"), tokens)
            if role is None:
                self._send_json(
                    401,
                    {"error": "Authentication required."},
                    authenticate=True,
                    close_connection=True,
                )
                return None
            if role not in allowed_roles:
                self._send_json(
                    403, {"error": "Insufficient permission."}, close_connection=True
                )
                return None
            return role

        def do_GET(self):
            if self._require_role({"operator", "viewer"}) is None:
                return
            try:
                if self.path == "/api/runs":
                    rows = database.list_tool_runs(
                        limit=100, redact_sensitive=True
                    )
                    keys = (
                        "id", "tool", "query", "status", "started_at",
                        "finished_at", "error",
                    )
                    payload = {"runs": [dict(zip(keys, row)) for row in rows]}
                elif self.path == "/api/results":
                    rows = database.list_tool_results(
                        limit=100, redact_sensitive=True
                    )
                    keys = ("id", "tool", "query", "result", "created_at")
                    payload = {"results": [dict(zip(keys, row)) for row in rows]}
                else:
                    self._send_json(404, {"error": "Not found."})
                    return
            except database.DATABASE_ERRORS:
                self._send_json(500, {"error": "Database operation failed."})
                return
            self._send_json(200, payload)

        def do_POST(self):
            if self._require_role({"operator"}) is None:
                return
            if self.path != "/api/runs":
                self._send_json(404, {"error": "Not found."})
                return

            try:
                content_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send_json(400, {"error": "Invalid content length."})
                return
            if not 0 <= content_length <= MAX_REQUEST_BYTES:
                self._send_json(413, {"error": "Request body is too large."})
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "Request body must be valid JSON."})
                return
            if (
                not isinstance(payload, dict)
                or set(payload) != {"tool", "query"}
                or not isinstance(payload["tool"], str)
                or not isinstance(payload["query"], str)
            ):
                self._send_json(
                    400, {"error": "Request must include string tool and query fields."}
                )
                return

            tool_name = payload["tool"]
            if tool_name not in RUNNERS:
                self._send_json(400, {"error": "Unsupported tool."})
                return
            validator, runner = RUNNERS[tool_name]
            try:
                query_text = validator(payload["query"])
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                status, result = execute_tool_run(
                    tool_name,
                    query_text,
                    runner,
                    sensitive_input=tool_name == "holehe",
                )
            except UnexpectedToolError:
                _LOG.exception("Unexpected sensitive tool failure")
                self._send_json(500, {"error": "Could not complete tool run."})
                return
            except (*database.DATABASE_ERRORS, RuntimeError):
                self._send_json(500, {"error": "Could not record tool run."})
                return
            except Exception:
                _LOG.exception("Unexpected tool failure")
                self._send_json(500, {"error": "Could not complete tool run."})
                return
            response = {"status": status}
            response["result" if status == "success" else "error"] = result
            self._send_json(201, response)

        def _reject_other_method(self):
            if self._require_role({"operator", "viewer"}) is not None:
                self._send_json(405, {"error": "Method not allowed."})

        do_DELETE = _reject_other_method
        do_HEAD = _reject_other_method
        do_OPTIONS = _reject_other_method
        do_PATCH = _reject_other_method
        do_PUT = _reject_other_method

        def log_message(self, format_string, *args):
            return

    return ApiHandler


def create_server(host="127.0.0.1", port=8765, tokens=None):
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("The API can bind only to an IPv4 loopback address.") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("The API can bind only to an IPv4 loopback address.")
    configured_tokens = _validate_tokens(tokens) if tokens is not None else load_tokens()
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("API port must be between 0 and 65535.")
    database.setup_database()
    return ThreadingHTTPServer(
        (str(address), port), _make_handler(configured_tokens)
    )


def main():
    server = create_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
