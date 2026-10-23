"""
The dashboard web server: a local HTTP server on the standard library, serving the page in static/ and a small JSON
API. It is meant for localhost only and has no authentication, so don't expose it to a network.

    open-call-dashboard --port 8000
"""
import argparse
import errno
import gzip
import json
import logging
import webbrowser
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from open_call_agent.paths import DEFAULT_DATA_DIR, open_calls_dir, scraped_dir

from .datasets import dataset_stats, list_datasets
from .jobs import JOB_TYPES, JobRunner
from .notes import NOTE_STATUSES, read_notes, save_note
from .runs import find_run, is_running, list_runs, restart_run, stop_run
from .sources import default_source, list_sources, load_rows
from .stats import llm_stats, scrape_stats, table_stats

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class DashboardHandler(SimpleHTTPRequestHandler):
    """
    Serves the static page and the /api/ endpoints.
    """

    def __init__(self, *args, context=None, **kwargs):
        self.context = context
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        # The page and its scripts change as the dashboard is worked on, so browsers must ask rather than reuse
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return super().do_GET()
        query = {key: values[0] for key, values in parse_qs(urlparse(self.path).query).items()}
        self.respond(lambda: self.get(path, query))

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or "{}")
        self.respond(lambda: self.post(path, body))

    def get(self, path, query):
        data_dir, runner = self.context["data_dir"], self.context["runner"]
        if path == "/api/config":
            sources = list_sources(data_dir)
            return {"sources": sources, "default_source": default_source(data_dir), "statuses": NOTE_STATUSES,
                    "job_types": {kind: {"label": job["label"], "gpu": job["gpu"], "fields": job["fields"]}
                                  for kind, job in JOB_TYPES.items()},
                    "data_dir": str(data_dir), "today": date.today().isoformat()}
        if path == "/api/festivals":
            source = query.get("source") or default_source(data_dir)
            # A check writes its results as each site finishes, so the table follows a running check while it goes
            checking = is_running(data_dir, runner, "open_calls")
            if not source:
                return {"source": None, "rows": [], "notes": {}, "stats": table_stats(data_dir, None, {}),
                        "checking": checking}
            notes = read_notes(data_dir)
            return {"source": source, "rows": load_rows(data_dir, source), "notes": notes,
                    "stats": table_stats(data_dir, source, notes), "checking": checking}
        if path == "/api/datasets":
            datasets = list_datasets(data_dir)
            return {"datasets": datasets, "stats": dataset_stats(datasets)}
        if path == "/api/checking":
            # Cheap enough for the festivals table to ask while it follows a running check
            return {"checking": is_running(data_dir, runner, "open_calls")}
        if path == "/api/stats/scrape":
            return {"stats": scrape_stats(data_dir, runner)}
        if path == "/api/stats/llm":
            return {"stats": llm_stats(data_dir, runner)}
        if path == "/api/runs":
            return {"runs": list_runs(data_dir, runner, query.get("kind"))}
        if path == "/api/runs/detail":
            return {"run": find_run(data_dir, runner, query.get("id", ""))}
        if path == "/api/jobs":
            return {"jobs": runner.as_list()}
        if path == "/api/jobs/log":
            return runner.log(int(query.get("id", 0)), int(query.get("offset", 0)))
        raise FileNotFoundError(f"Unknown endpoint {path}")

    def post(self, path, body):
        data_dir, runner = self.context["data_dir"], self.context["runner"]
        if path == "/api/notes":
            notes = save_note(data_dir, body.get("id"), body.get("status", ""), body.get("note", ""))
            return {"notes": notes, "stats": table_stats(data_dir, body.get("source"), notes)}
        if path == "/api/jobs/start":
            return {"job": runner.start(body.get("kind"), body.get("options") or {}).as_dict()}
        if path == "/api/jobs/stop":
            return {"job": runner.stop(int(body.get("id", 0))).as_dict()}
        if path == "/api/runs/stop":
            return {"run": stop_run(data_dir, runner, body.get("id", ""))}
        if path == "/api/runs/restart":
            return {"job": restart_run(data_dir, runner, body.get("id", "")).as_dict()}
        raise FileNotFoundError(f"Unknown endpoint {path}")

    def respond(self, handler):
        """
        Run a handler and send its result as JSON, turning errors into 400, 404 and 500 responses.

        :param handler: A function returning a JSON-serialisable dict.
        """
        try:
            status, payload = 200, handler()
        except FileNotFoundError as error:
            status, payload = 404, {"error": str(error)}
        except ValueError as error:
            status, payload = 400, {"error": str(error)}
        except Exception as error:
            logger.exception("%s failed", self.path)
            status, payload = 500, {"error": f"{type(error).__name__}: {error}"}

        body = json.dumps(payload).encode()
        # The festivals table is a few megabytes of text, which compresses to a fraction of that
        compress = len(body) > 10_000 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = gzip.compress(body, 1) if compress else body

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if compress:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.address_string(), format % args)


def serve(data_dir=DEFAULT_DATA_DIR, host="127.0.0.1", port=8000, project_dir=None, open_browser=False):
    """
    Run the dashboard until Ctrl+C, then stop any jobs it started.

    :param data_dir: The data directory.
    :param host: The address to listen on, localhost by default.
    :param port: The port.
    :param project_dir: The directory the scrapers run in, the working directory by default.
    :param open_browser: Open the dashboard in a browser once it is up.
    """
    data_dir = Path(data_dir).resolve()
    # A relative --data-dir is read from wherever the dashboard was started, which is the easiest way to end up
    # looking at an empty page: the directory simply isn't there
    if not data_dir.is_dir():
        print(f"There is no {data_dir}. A relative --data-dir is resolved from {Path.cwd()}, so start the dashboard "
              f"in the project directory or give it an absolute path.")
    elif not scraped_dir(data_dir).is_dir() and not open_calls_dir(data_dir).is_dir():
        print(f"No scraped data or results in {data_dir}. Start the dashboard in the project directory, or pass "
              f"--data-dir <path>.")
    context = {"data_dir": data_dir, "runner": JobRunner(data_dir, project_dir)}
    try:
        server = ThreadingHTTPServer((host, port), partial(DashboardHandler, context=context))
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        # Without this the command dies in a traceback while the browser still shows the dashboard already running
        # on that port, so it looks as though this one started and found nothing
        print(f"Port {port} is already in use. Another dashboard is probably still running and is what your browser "
              f"is showing. Stop it, or start this one with --port <other port>.")
        return 1
    url = f"http://{'localhost' if host == '127.0.0.1' else host}:{server.server_port}"
    print(f"Dashboard on {url} (data: {data_dir}). Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping jobs started from the dashboard...")
        context["runner"].stop_all()
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="open-call-dashboard", description="Browse reviewed festivals and run the "
                                                                             "scrapers in a local dashboard.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help=f"Data directory (default: {DEFAULT_DATA_DIR}).")
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000).")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return serve(args.data_dir, args.host, args.port, open_browser=args.open)
