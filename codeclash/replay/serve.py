"""Local server behind ``codeclash replay``.

Serves an index of the tournament's games and renders each game's replay on demand, in
memory, when its link is clicked (``/game?r=<round>&s=<sim>``). Nothing is written to
disk. Stdlib only — no extra dependencies.
"""

from __future__ import annotations

import html
import socketserver
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from codeclash.replay import base
from codeclash.replay.base import ReplayRenderer, TournamentInfo


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_server(folder: Path, tour: TournamentInfo, renderer: ReplayRenderer, port: int = 8000) -> None:
    """Serve the folder's replays until interrupted, opening the index in a browser."""
    games = {(g.round, g.sim): g for g in tour.games}

    def render_game(game) -> str:
        payload = {
            "arena": tour.arena,
            "players": tour.players,
            "round": game.round,
            "sim": game.sim,
            "round_winner": tour.round_winners.get(game.round),
        }
        return base.build_page(renderer.parse(base.read_sim(game), tour.players), payload, renderer)

    def error_page(game, exc) -> str:
        source = game.member or (game.path.name if game.path else "?")
        return (
            '<!doctype html><meta charset="utf-8">'
            "<body style='background:#0d1117;color:#e6edf3;font:14px system-ui;padding:24px'>"
            f"<h2>Couldn't render round {game.round}, sim {game.sim}</h2>"
            f"<p style='color:#8b949e'>source: <code>{html.escape(source)}</code></p>"
            f"<pre style='color:#e5484d;white-space:pre-wrap'>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</pre>"
            "<p><a style='color:#58a6ff' href='/'>&larr; back to index</a></p></body>"
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._html(base.build_index(tour))
                return
            if parsed.path == "/game":
                q = urllib.parse.parse_qs(parsed.query)
                try:
                    r, s = int(q["r"][0]), int(q["s"][0])
                except (KeyError, ValueError, IndexError):
                    self.send_error(400, "need integer r and s")
                    return
                game = games.get((r, s))
                if game is None:
                    self.send_error(404, f"no game at round {r}, sim {s}")
                    return
                try:
                    self._html(render_game(game))
                except Exception as exc:  # one bad game shouldn't take down the server
                    print(f"[replay] failed to render round {r} sim {s}: {type(exc).__name__}: {exc}")
                    self._html(error_page(game, exc), status=500)
                return
            self.send_error(404)

        def _html(self, page: str, status: int = 200):
            body = page.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the console quiet
            pass

    try:
        httpd = _Server(("127.0.0.1", port), Handler)
    except OSError:
        httpd = _Server(("127.0.0.1", 0), Handler)  # requested port busy → grab a free one
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Serving replays for {folder.name} at {url}")
    print("Click any game to watch it. Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
