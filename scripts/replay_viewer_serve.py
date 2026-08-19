"""Offline Replay Viewer static+API server (127.0.0.1, no external deps)."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from arctic_route_orchestrator.replay.presentation import PresentationAdapter


class Handler(BaseHTTPRequestHandler):
    server_version = "ArcticRouteReplayViewer"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[viewer] %s\n" % (fmt % args))

    def _json(self, status: int, document: object) -> None:
        body = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            tick = (query.get("t") or [""])[0]
            if not tick:
                self._json(400, {"error": "missing t"})
                return
            try:
                document = self.server.adapter.state_at(tick).to_dict()
            except Exception as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, document)
            return
        root = self.server.root.resolve()
        relative = parsed.path.lstrip("/")
        candidate = (root / relative).resolve()
        if not str(candidate).startswith(str(root)):
            self._json(404, {"error": "not found"})
            return
        if not candidate.is_file():
            if relative in ("", "/") or candidate.name == "":
                candidate = root / "index.html"
            else:
                self._json(404, {"error": "not found"})
                return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _adapter_for(manifest: Path, snapshots_dir: Path | None) -> PresentationAdapter | None:
    if not manifest.exists():
        return None
    manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
    folder = snapshots_dir or manifest.parent / "snapshots"
    snapshots = [
        json.loads((folder / f"{entry['index']:04d}.json").read_text(encoding="utf-8"))
        for entry in manifest_doc["snapshots"]
    ]
    return PresentationAdapter(manifest_doc, snapshots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-viewer-serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8131)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent.parent / "viewer")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.root = args.root.resolve()
    server.adapter = (
        _adapter_for(args.manifest, args.snapshots_dir) if args.manifest else None
    )
    print(
        "serving",
        server.root,
        "at",
        f"http://{args.host}:{args.port}",
        "api_state=", "on" if server.adapter else "off",
        flush=True,
    )
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
