"""Local browser interface for launching and reviewing authorized tool runs."""

import json
import ipaddress
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import data.utils.database as database
from helix_integration import run_helix, validate_username as validate_helix_username
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username
from sherlock_integration import run_sherlock, validate_username as validate_sherlock_username
from tool_registry import get_tool_metadata
from tool_runs import execute_tool_run, start_tool_run

_LOG = logging.getLogger(__name__)
RUNNABLE_TOOLS = {
    "holehe": ("email", validate_email, lambda value: run_holehe(value)["output"]),
    "maigret": ("username", validate_username, run_maigret),
    "helix": ("username", validate_helix_username, run_helix),
    "sherlock": ("username", validate_sherlock_username, run_sherlock),
}

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OSINT Beast · Authorized Runs</title>
  <style>
    body { font: 16px system-ui, sans-serif; line-height: 1.5; margin: 2rem auto; max-width: 52rem; padding: 0 1rem; color: #17202a; }
    .notice { border-left: 4px solid #a34a00; background: #fff4e5; padding: .8rem 1rem; }
    label { display: block; margin: 1rem 0 .25rem; }
    input, select, button { font: inherit; padding: .45rem; }
    input[type="text"], input[type="email"], select { max-width: 32rem; width: 100%; }
    button { cursor: pointer; }
    #authorization { display: inline; }
    #runs button { display: block; margin: .4rem 0; }
    pre { background: #f3f5f7; border: 1px solid #d5d9dc; overflow: auto; padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere; }
    [role="alert"] { color: #9c1c1c; }
  </style>
</head>
<body>
  <h1>Authorized tool runs</h1>
  <p class="notice"><strong>Authorization required:</strong> Only run checks against accounts and data you own or are explicitly authorized to assess. Results may contain sensitive information. This interface is local-only; keep access to this machine and its database restricted.</p>
  <form id="launch">
    <label for="tool">Registered tool</label>
    <select id="tool" required></select>
    <label id="input-label" for="query">Input</label>
    <input id="query" type="text" required autocomplete="off">
    <p><label id="authorization"><input id="authorized" type="checkbox" required> I am authorized to run this check.</label></p>
    <button id="submit" type="submit">Launch run</button>
  </form>
  <p id="message" role="status" aria-live="polite"></p>
  <p id="error" role="alert"></p>
  <h2>Recent runs</h2>
  <div id="runs"></div>
  <section id="detail" hidden>
    <h2 id="detail-title">Run details</h2>
    <p id="detail-status"></p>
    <pre id="detail-output"></pre>
  </section>
  <script>
    const toolSelect = document.querySelector("#tool");
    const inputLabel = document.querySelector("#input-label");
    const queryInput = document.querySelector("#query");
    const error = document.querySelector("#error");

    async function request(url, options) {
      const response = await fetch(url, options);
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "Request failed.");
      return body;
    }

    async function loadTools() {
      const tools = await request("/api/tools");
      for (const tool of tools) {
        const option = document.createElement("option");
        option.value = tool.name;
        option.textContent = `${tool.name} — ${tool.description}`;
        option.dataset.input = tool.input;
        toolSelect.append(option);
      }
      updateInputLabel();
    }

    function updateInputLabel() {
      const input = toolSelect.selectedOptions[0]?.dataset.input || "input";
      inputLabel.textContent = input === "email" ? "Email address" : "Username";
      queryInput.type = input === "email" ? "email" : "text";
      queryInput.autocomplete = input === "email" ? "email" : "off";
    }

    async function loadRuns() {
      const runs = await request("/api/runs?limit=20");
      const container = document.querySelector("#runs");
      container.replaceChildren();
      if (!runs.length) {
        container.textContent = "No runs have been recorded.";
        return;
      }
      for (const run of runs) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = `Run #${run.id} · ${run.tool} · ${run.input} · ${run.status}`;
        button.addEventListener("click", () => loadRun(run.id));
        container.append(button);
      }
    }

    async function loadRun(id) {
      const run = await request(`/api/runs/${id}`);
      document.querySelector("#detail").hidden = false;
      document.querySelector("#detail-title").textContent = `Run #${run.id} · ${run.tool}`;
      document.querySelector("#detail-status").textContent =
        `${run.status} · ${run.input} · started ${run.started_at}` +
        (run.finished_at ? ` · finished ${run.finished_at}` : "");
      document.querySelector("#detail-output").textContent = run.result || run.error || "";
      await loadRuns();
      if (run.status === "running") setTimeout(() => loadRun(id).catch(showError), 1500);
    }

    function showError(reason) {
      error.textContent = reason.message;
    }

    toolSelect.addEventListener("change", updateInputLabel);
    document.querySelector("#launch").addEventListener("submit", async event => {
      event.preventDefault();
      error.textContent = "";
      const message = document.querySelector("#message");
      const submit = document.querySelector("#submit");
      submit.disabled = true;
      message.textContent = "Submitting run…";
      try {
        const run = await request("/api/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tool: toolSelect.value,
            input: queryInput.value,
            authorized: document.querySelector("#authorized").checked
          })
        });
        message.textContent = `Run #${run.id} is ${run.status}.`;
        await loadRun(run.id);
      } catch (reason) {
        message.textContent = "";
        showError(reason);
      } finally {
        submit.disabled = false;
      }
    });

    loadTools().then(loadRuns).catch(showError);
  </script>
</body>
</html>
"""


def _run_in_background(run_id, tool_name, query_text, runner):
    try:
        execute_tool_run(
            tool_name,
            query_text,
            runner,
            run_id=run_id,
            sensitive_input=tool_name == "holehe",
        )
    except (OSError, RuntimeError, ValueError, *database.DATABASE_ERRORS):
        try:
            database.finish_tool_run(
                run_id,
                "failed",
                error_text="The run outcome could not be persisted.",
            )
        except (*database.DATABASE_ERRORS, ValueError) as exc:
            _LOG.error(
                "Could not persist completion for run %s (%s)",
                run_id,
                type(exc).__name__,
            )


class WebHandler(BaseHTTPRequestHandler):
    server_version = "OSINTBeast"

    def _has_local_host(self):
        host_header = self.headers.get("Host", "")
        try:
            host = urlsplit(f"//{host_header}")
            hostname = host.hostname
            port = host.port
        except ValueError:
            return False
        if (
            not hostname
            or host.username
            or host.password
            or host.path
            or host.query
            or host.fragment
            or port != self.server.server_port
        ):
            return False
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _has_same_origin(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == host.lower()
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
        )

    def _send(self, status, body, content_type):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _run_payload(self, row, detail=False):
        payload = {
            "id": row[0],
            "tool": row[1],
            "input": row[2],
            "status": row[3],
        }
        if detail:
            payload.update({
                "result": row[4],
                "error": row[5],
                "started_at": row[6],
                "finished_at": row[7],
            })
        else:
            payload.update({"started_at": row[4], "finished_at": row[5]})
        return payload

    def do_GET(self):
        if not self._has_local_host():
            self._send_json(403, {"error": "Use the local interface host."})
            return
        path = urlsplit(self.path)
        try:
            if path.path == "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif path.path == "/api/tools":
                tools = []
                for name, (input_kind, _, _) in RUNNABLE_TOOLS.items():
                    metadata = get_tool_metadata(name)
                    tools.append({
                        "name": name,
                        "input": input_kind,
                        "description": metadata["notes"],
                    })
                self._send_json(200, tools)
            elif path.path == "/api/runs":
                from urllib.parse import parse_qs

                database.reconcile_stale_tool_runs()
                values = parse_qs(path.query).get("limit", ["20"])
                limit = int(values[0])
                self._send_json(
                    200,
                    [self._run_payload(row) for row in database.list_tool_runs(limit)],
                )
            elif path.path.startswith("/api/runs/"):
                database.reconcile_stale_tool_runs()
                run_id_text = path.path.removeprefix("/api/runs/")
                if not run_id_text.isdecimal():
                    self._send_json(404, {"error": "Run not found."})
                    return
                row = database.get_tool_run(int(run_id_text))
                if row is None:
                    self._send_json(404, {"error": "Run not found."})
                else:
                    self._send_json(200, self._run_payload(row, detail=True))
            else:
                self._send_json(404, {"error": "Not found."})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except database.DATABASE_ERRORS:
            self._send_json(500, {"error": "Could not access the run database."})

    def do_POST(self):
        if not self._has_local_host():
            self._send_json(403, {"error": "Use the local interface host."})
            return
        if not self._has_same_origin():
            self._send_json(403, {"error": "Run requests must come from this interface."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "Run requests must use application/json."})
            return
        if urlsplit(self.path).path != "/api/runs":
            self._send_json(404, {"error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                self._send_json(400, {"error": "Request body must be between 1 and 16384 bytes."})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("A JSON object is required.")
            if payload.get("authorized") is not True:
                self._send_json(403, {"error": "Confirm that you are authorized before launching a run."})
                return
            tool_name = payload.get("tool")
            tool = RUNNABLE_TOOLS.get(tool_name) if isinstance(tool_name, str) else None
            if tool is None:
                self._send_json(400, {"error": "Choose a supported registered tool."})
                return
            input_kind, validator, runner = tool
            query_text = validator(payload.get("input"))
            run_id = start_tool_run(
                tool_name, query_text, sensitive_input=tool_name == "holehe"
            )
            thread = threading.Thread(
                target=_run_in_background,
                args=(run_id, tool_name, query_text, runner),
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError:
                database.finish_tool_run(
                    run_id,
                    "failed",
                    error_text="The tool run could not be started.",
                )
                self._send_json(503, {"error": "The tool run could not be started."})
                return
            self._send_json(202, {
                "id": run_id,
                "tool": tool_name,
                "input": query_text,
                "input_kind": input_kind,
                "status": "running",
            })
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Request body must contain valid JSON."})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except database.DATABASE_ERRORS:
            self._send_json(500, {"error": "Could not access the run database."})

    def log_message(self, format, *args):
        pass


def serve(host="127.0.0.1", port=8000):
    """Serve the web UI on loopback by default."""
    database.setup_database()
    server = ThreadingHTTPServer((host, port), WebHandler)
    server.daemon_threads = True
    print(f"OSINT Beast web UI: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
