"""CoreWar replay renderer: the MARS core as a grid, each cell coloured by its owning warrior.

Parses ``sim_*.jsonl`` from ``trace.py`` and folds the per-frame ``c`` deltas into a cumulative
owner-map; recent cells flash bright, PCs are white. No cell holds Redcode contents, so replays
show behaviour, not source (conventions per the corewar-docs visualisation page).
"""

from __future__ import annotations

import json

from codeclash.replay.base import ReplayData, ReplayRenderer

PALETTE = ["#3B78FF", "#E5484D", "#30A46C", "#F5A623", "#8E4EC6", "#12A594", "#E93D82", "#F76B15"]

DRAW_JS = """
const ARENA = (function(){
  let CORE, GW, GH, CELL, COL, NAMES, cw, ch;
  // Cumulative owner-map folded from frame deltas, with a cursor so forward playback is O(delta).
  let owner, builtTo;
  function setup(cv, G){
    CORE = G.core; GW = G.w; GH = G.h; COL = G.colors; NAMES = G.names;
    CELL = Math.max(2, Math.floor(Math.min(880 / GW, 620 / GH)));
    cw = cv.width = GW * CELL; ch = cv.height = GH * CELL;
    owner = new Int16Array(CORE).fill(-1);
    builtTo = -1;
  }
  function ensure(F, i){
    if(builtTo > i){ owner.fill(-1); builtTo = -1; }        // scrubbed backward: rebuild
    for(let k = builtTo + 1; k <= i; k++){
      const cs = F[k].c;
      for(let j = 0; j < cs.length; j++) owner[cs[j][0]] = cs[j][1];
    }
    builtTo = i;
  }
  const cellX = (a) => (a % GW) * CELL, cellY = (a) => Math.floor(a / GW) * CELL;
  function rect(ctx, a, color){ ctx.fillStyle = color; ctx.fillRect(cellX(a), cellY(a), CELL, CELL); }
  // Dim the owner colour for settled cells; recent activity is drawn at full strength.
  function dim(hex){
    const n = parseInt(hex.slice(1), 16);
    const r = (n>>16)&255, g = (n>>8)&255, b = n&255;
    return `rgb(${(r*0.55)|0},${(g*0.55)|0},${(b*0.55)|0})`;
  }
  const DIM = {};
  function draw(ctx, cv, G, i){
    const F = G.frames, f = F[i];
    ensure(F, i);
    ctx.fillStyle = '#2b2f36';                               // grey = untouched core (initial DAT)
    ctx.fillRect(0, 0, cw, ch);
    for(let a = 0; a < CORE; a++){
      const o = owner[a];
      if(o >= 0){ const c = COL[o]; rect(ctx, a, (DIM[c] || (DIM[c] = dim(c)))); }
    }
    (f.c || []).forEach(([a, o]) => rect(ctx, a, COL[o] || '#fff'));   // recent activity, bright
    (f.p || []).forEach((addr, w) => {                                 // program counters, white
      if(f.d && f.d[w]) rect(ctx, addr, '#ffffff');
    });
  }
  function side(G, i){
    const f = G.frames[i], ids = Object.keys(NAMES);
    return ids.map(id => {
      const w = +id, dead = f.d ? !f.d[w] : false, procs = f.n ? f.n[w] : 0;
      return `<div class="sn ${dead?'dead':''}"><span class="sw" style="background:${COL[w]}"></span>
        <span style="min-width:120px">${NAMES[id]}</span>
        <span>${dead ? '\\u2620 dead' : procs + ' proc' + (procs===1?'':'s')}</span></div>`;
    }).join('') + '<div class="row muted" style="font-size:12px">cell colour = owning warrior \\u00b7 '
      + 'bright = just now \\u00b7 white = program counter</div>';
  }
  return {setup, draw, side};
})();
"""


class CoreWarReplayer(ReplayRenderer):
    arena = "CoreWar"
    sim_glob = "sim_*.jsonl"
    DRAW_JS = DRAW_JS

    def parse(self, raw: bytes, players=None) -> ReplayData:
        rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        if not rows:
            return ReplayData(w=1, h=1, frames=[])
        header = rows[0]
        result = rows[-1] if isinstance(rows[-1], dict) and "winner" in rows[-1] else {}
        warriors = {str(k): v for k, v in header.get("warriors", {}).items()}
        colors = {int(k): PALETTE[i % len(PALETTE)] for i, k in enumerate(sorted(warriors, key=int))}

        frames = []
        for r in rows:
            if isinstance(r, dict) and "t" in r:
                frames.append(
                    {"turn": r["t"], "c": r.get("c", []), "p": r.get("p", []), "n": r.get("n", []), "d": r.get("d", [])}
                )

        return ReplayData(
            w=header.get("w", 1),
            h=header.get("h", 1),
            frames=frames,
            winner=result.get("winner"),
            draw=result.get("draw", False),
            extra={
                "core": header.get("core", header.get("w", 1) * header.get("h", 1)),
                "colors": colors,
                "names": warriors,
                "starts": header.get("starts", []),
            },
        )

    def peek_winner(self, raw: bytes, players=None) -> tuple[str | None, bool] | None:
        for line in reversed(raw.decode().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            if isinstance(obj, dict) and "winner" in obj:
                return obj.get("winner"), obj.get("draw", False)
            return None
        return None
