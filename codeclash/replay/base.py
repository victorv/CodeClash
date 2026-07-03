"""General replay scaffolding shared by every arena.

The split of responsibilities:

* **base (this module)** owns everything arena-agnostic — the HTML player shell,
  all playback controls, the tournament-info header, the index page, the ascii
  scaffold, sim discovery (incl. `rounds/round_<R>.tar.gz` archives), and file I/O.
* **each arena** (``codeclash/arenas/<arena>/replay.py``) subclasses
  :class:`ReplayRenderer` and owns just two things: ``parse()`` (raw sim bytes ->
  normalized :class:`ReplayData`) and ``DRAW_JS`` (its bespoke canvas + side panel).

The sim on disk can be any format — text (`.jsonl`, `.txt`, `.md`), JSON, or binary.
Discovery hands the arena the raw ``bytes`` and lets it decide how to decode; only the
:class:`ReplayData` it returns is typed and uniform.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReplayData:
    """Normalized, arena-agnostic playback payload.

    The player shell only relies on ``w``, ``h``, ``frames`` (each frame carrying a
    ``turn``), ``winner`` and ``draw``. Everything an arena's ``DRAW_JS`` additionally
    needs (colors, team names, walls, max health, ...) rides along in ``extra`` and is
    merged into the JS ``G`` object.
    """

    w: int
    h: int
    frames: list[dict]
    winner: str | None = None
    draw: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def payload(self) -> dict:
        """The ``G`` object handed to the JS player."""
        return {"w": self.w, "h": self.h, "frames": self.frames, "winner": self.winner, "draw": self.draw, **self.extra}

    def __bool__(self) -> bool:  # truthy iff it actually has frames to play
        return bool(self.frames)


@dataclass
class GameRef:
    """A single playable game located within a tournament folder.

    Either it lives inside a per-round tar archive (``archive`` + ``member``) or it is a
    loose file on disk (``path``).
    """

    round: int
    sim: int
    archive: Path | None = None
    member: str | None = None
    path: Path | None = None


@dataclass
class TournamentInfo:
    """Everything the replay layer needs about a tournament folder."""

    folder: Path
    arena: str
    players: list[dict]  # [{"name": str, "model": str | None}, ...]
    rounds: int | None
    sims_per_round: int | None
    round_winners: dict[int, str]
    games: list[GameRef]


class ReplayRenderer(ABC):
    """Per-arena contract: how to read a sim and how to draw it."""

    arena: str = ""  # must match config.game.name, e.g. "BattleSnake"
    sim_glob: str = "sim_*.jsonl"  # basename glob used to discover sims
    DRAW_JS: str = ""  # defines a global ``ARENA`` object with setup/draw/side
    CSS: str = ""  # optional arena-specific style additions

    @abstractmethod
    def parse(self, raw: bytes, players: list[dict] | None = None) -> ReplayData:
        """Decode raw sim bytes into a :class:`ReplayData`. Input format is opaque."""


# --------------------------------------------------------------------------------------
# Sim discovery + reading
# --------------------------------------------------------------------------------------


def _sim_index(name: str) -> int:
    m = re.search(r"sim_?(\d+)", name)
    return int(m.group(1)) if m else 0


def _round_num(name: str) -> int:
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


def discover_games(folder: Path, sim_glob: str) -> list[GameRef]:
    """Find every playable game under a tournament folder.

    Handles three layouts: ``rounds/round_<R>.tar.gz`` archives (the common case),
    uncompressed ``rounds/<R>/sim_*`` directories, and a flat folder of sim files
    (e.g. an ad-hoc sample with a single ``sim.json``).
    """
    games: list[GameRef] = []
    rounds_dir = folder / "rounds"
    if rounds_dir.is_dir():
        entries = sorted(rounds_dir.iterdir())
        # Archives first, recording which rounds they cover.
        archived: set[int] = set()
        for entry in entries:
            if entry.is_file() and entry.name.startswith("round") and entry.suffixes[-1:] == [".gz"]:
                rnum = _round_num(entry.name)
                archived.add(rnum)
                with tarfile.open(entry) as tf:
                    for m in tf.getmembers():
                        if m.isfile() and m.size > 0 and fnmatch.fnmatch(Path(m.name).name, sim_glob):
                            games.append(
                                GameRef(round=rnum, sim=_sim_index(Path(m.name).name), archive=entry, member=m.name)
                            )
        # Then round dirs — but skip any round already sourced from an archive. A round can
        # exist both extracted and tarred; counting both would double up the same games.
        for entry in entries:
            if entry.is_dir() and _round_num(entry.name) not in archived:
                rnum = _round_num(entry.name)
                for f in sorted(entry.iterdir()):
                    if f.is_file() and f.stat().st_size > 0 and fnmatch.fnmatch(f.name, sim_glob):
                        games.append(GameRef(round=rnum, sim=_sim_index(f.name), path=f))
    else:
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.stat().st_size > 0 and fnmatch.fnmatch(f.name, sim_glob):
                games.append(GameRef(round=0, sim=_sim_index(f.name), path=f))
    games.sort(key=lambda g: (g.round, g.sim))
    return games


def read_sim(game: GameRef) -> bytes:
    """Read the raw bytes for a game, extracting from its tar archive if needed."""
    if game.archive is not None:
        with tarfile.open(game.archive) as tf:
            f = tf.extractfile(game.member)
            if f is None:
                raise FileNotFoundError(f"{game.member} not found in {game.archive}")
            return f.read()
    return game.path.read_bytes()


# --------------------------------------------------------------------------------------
# Shared HTML player (shell + controls) — arena-agnostic
# --------------------------------------------------------------------------------------

SHELL_CSS = """
body{background:#0d1117;color:#e6edf3;font:14px system-ui,sans-serif;margin:0;padding:16px;display:flex;gap:20px;flex-wrap:wrap}
canvas{background:#161b22;border-radius:8px}
#panel{min-width:260px}
.row{margin:8px 0}
button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px;cursor:pointer;font-size:14px}
button:hover{background:#30363d}
#tour{margin-bottom:12px}
.tour-players{font-weight:700;font-size:15px}
.tour-players .pl{white-space:nowrap}
.tour-meta{font-size:12px;margin-top:2px}
.muted{color:#8b949e}
#winner{font-weight:700;font-size:16px;margin-top:12px}
/* BattleSnake side panel */
.sn{display:flex;align-items:center;gap:8px;margin:6px 0}
.sw{width:14px;height:14px;border-radius:3px}
.hb{height:8px;background:#30363d;border-radius:4px;flex:1;overflow:hidden}
.hf{height:100%}
.dead{opacity:.4;text-decoration:line-through}
/* RobotRumble side panel */
.team{margin:12px 0;padding:10px;background:#161b22;border-radius:8px}
.tname{display:flex;align-items:center;gap:8px;font-weight:700}
.stat{display:flex;justify-content:space-between;margin:6px 0;font-variant-numeric:tabular-nums}
.team .hb{margin-top:4px}
.tdead{opacity:.45}
"""

SHELL_BODY = """
<canvas id="c"></canvas>
<div id="panel">
 <div id="tour"></div>
 <div class="row"><b>Turn <span id="t">0</span></b> / <span id="maxturn">0</span></div>
 <div class="row">
  <button id="first">&#9198;</button><button id="prev">&#9664;</button>
  <button id="play">&#9654; play</button><button id="next">&#9654;</button><button id="last">&#9199;</button>
 </div>
 <div class="row">speed <input id="speed" type="range" min="1" max="30" value="8"></div>
 <input id="scrub" type="range" min="0" max="0" value="0" style="width:100%">
 <div id="side"></div>
 <div id="winner"></div>
</div>
"""

# The player state machine. Expects globals ``G`` (ReplayData payload), optional ``TOUR``
# (tournament header), and ``ARENA`` (provided by the arena's DRAW_JS) exposing
# setup(cv, G, ctx) / draw(ctx, cv, G, i) / side(G, i).
PLAYER_JS = """
(function(){
  const G = window.G, TOUR = window.TOUR || null, F = G.frames;
  const cv = document.getElementById('c'), ctx = cv.getContext('2d');
  if(!CanvasRenderingContext2D.prototype.roundRect){CanvasRenderingContext2D.prototype.roundRect=function(x,y,w,h,r){this.beginPath();this.moveTo(x+r,y);this.arcTo(x+w,y,x+w,y+h,r);this.arcTo(x+w,y+h,x,y+h,r);this.arcTo(x,y+h,x,y,r);this.arcTo(x,y,x+w,y,r);this.closePath();return this;};}
  ARENA.setup(cv, G, ctx);
  const maxturn = F.length - 1;
  document.getElementById('maxturn').textContent = maxturn;
  document.getElementById('scrub').max = maxturn;
  if(TOUR) document.getElementById('tour').innerHTML = renderTour(TOUR);
  let i = 0, playing = false, timer = null;
  function draw(){
    ARENA.draw(ctx, cv, G, i);
    document.getElementById('t').textContent = F[i].turn;
    document.getElementById('scrub').value = i;
    document.getElementById('side').innerHTML = ARENA.side(G, i);
    document.getElementById('winner').textContent = (i === maxturn) ? ('Winner: ' + (G.draw ? 'TIE' : (G.winner || '\\u2014'))) : '';
  }
  function go(n){ i = Math.max(0, Math.min(maxturn, n)); draw(); }
  function play(){
    playing = !playing;
    document.getElementById('play').innerHTML = playing ? '\\u23f8 pause' : '\\u25b6 play';
    if(playing){ timer = setInterval(()=>{ if(i >= maxturn){ play(); return; } go(i+1); }, 1000 / +document.getElementById('speed').value); }
    else clearInterval(timer);
  }
  document.getElementById('play').onclick = play;
  document.getElementById('next').onclick = ()=>go(i+1);
  document.getElementById('prev').onclick = ()=>go(i-1);
  document.getElementById('first').onclick = ()=>go(0);
  document.getElementById('last').onclick = ()=>go(maxturn);
  document.getElementById('scrub').oninput = (e)=>go(+e.target.value);
  document.getElementById('speed').oninput = ()=>{ if(playing){ play(); play(); } };
  document.addEventListener('keydown', (e)=>{
    if(e.key === 'ArrowRight') go(i+1);
    else if(e.key === 'ArrowLeft') go(i-1);
    else if(e.key === ' '){ e.preventDefault(); play(); }
  });
  function renderTour(t){
    const players = (t.players || []).map(p => `<span class="pl">${p.name}${p.model ? ` <span class="muted">(${p.model})</span>` : ''}</span>`).join(' vs ');
    const bits = [];
    if(t.arena) bits.push(`<b>${t.arena}</b>`);
    if(t.round != null) bits.push(`round ${t.round}`);
    if(t.sim != null) bits.push(`sim ${t.sim}`);
    if(t.round_winner) bits.push(`round winner: ${t.round_winner}`);
    return `<div class="tour-players">${players}</div><div class="muted tour-meta">${bits.join(' \\u00b7 ')}</div>`;
  }
  draw();
})();
"""


def _script_safe(obj) -> str:
    """JSON for inlining inside a <script> tag (guard against ``</script>``)."""
    return json.dumps(obj).replace("</", "<\\/")


def _doc(head_extra: str, scripts: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        + head_extra
        + "</head><body>\n"
        + SHELL_BODY
        + "\n"
        + scripts
        + "\n</body></html>\n"
    )


def build_page(data: ReplayData, tour: dict, renderer: ReplayRenderer) -> str:
    """A complete, self-contained replay page (CSS + player + arena JS + data inlined).

    Built on demand by the server and served straight from memory — nothing is written to
    disk.
    """
    head = f"<title>{renderer.arena} replay</title><style>{SHELL_CSS}{renderer.CSS}</style>"
    scripts = (
        "<script>window.G="
        + _script_safe(data.payload)
        + ";window.TOUR="
        + _script_safe(tour)
        + ";</script>\n"
        + "<script>"
        + renderer.DRAW_JS
        + "</script>\n"
        + "<script>"
        + PLAYER_JS
        + "</script>"
    )
    return _doc(head, scripts)


def build_index(tour: TournamentInfo) -> str:
    """Index page: every game (round x sim) with its winner, linking to its replay."""
    players = " vs ".join(p["name"] for p in tour.players)
    rows = []
    for g in tour.games:
        winner = tour.round_winners.get(g.round, "")
        cell = f'<a class="btn" href="game?r={g.round}&s={g.sim}">&#9654; watch</a>'
        rows.append(f"<tr><td>{g.round}</td><td>{g.sim}</td><td>{winner}</td><td>{cell}</td></tr>")
    style = (
        "body{background:#0d1117;color:#e6edf3;font:14px system-ui,sans-serif;margin:0;padding:24px}"
        "h1{font-size:18px}.muted{color:#8b949e}"
        "table{border-collapse:collapse;margin-top:12px}"
        "td,th{padding:6px 14px;border-bottom:1px solid #21262d;text-align:left}"
        "a{color:#58a6ff}"
        ".btn{display:inline-block;padding:3px 10px;border:1px solid #30363d;border-radius:6px;"
        "background:#21262d;color:#e6edf3;text-decoration:none}.btn:hover{background:#30363d}"
    )
    header = (
        f"<h1>{tour.arena} &middot; {players}</h1>"
        f'<div class="muted">{tour.rounds} round(s) &times; {tour.sims_per_round} sims &middot; {len(tour.games)} games'
        f" &middot; {tour.folder.name}</div>"
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{tour.arena} replays</title>'
        f"<style>{style}</style></head><body>{header}"
        "<table><tr><th>round</th><th>sim</th><th>round winner</th><th></th></tr>"
        + "".join(rows)
        + "</table></body></html>\n"
    )
