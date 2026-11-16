"""Serve the dashboard.

    python -m fabricpulse.serve            # http://localhost:8765/dashboard/

A plain static file server rooted at the repository, so the dashboard can read
results/*.json. Generate those first with `python -m fabricpulse.run` and
`python -m fabricpulse.run --sweep`.
"""

from __future__ import annotations

import argparse
import functools
import http.server
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    if not (ROOT / "results" / "summary.json").exists():
        print("No results yet. Run: python -m fabricpulse.run  (and --sweep)")
    handler = functools.partial(Handler, directory=str(ROOT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"FabricPulse dashboard: http://localhost:{args.port}/dashboard/")
        srv.serve_forever()


if __name__ == "__main__":
    main()
