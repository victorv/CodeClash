"""RoboCode replay renderer.

Parses a compact ``sim_*.jsonl`` (produced by :mod:`codeclash.arenas.robocode.trace` from
Robocode's ``-recordXML``) into normalized playback data and draws one Robocode round: the
battlefield, each tank with its body/gun/radar headings and energy, and bullets in flight.

The jsonl format (see ``trace.py``): a header line ``{w, h, round, robots:{id:name}}``,
per-turn frames ``{t, u:[{i,x,y,e,bh,gh,rh,v,s}], b:[{o,x,y,p,s}]}``, and a final result
line ``{winner, draw}``. Robocode uses a y-up field (origin bottom-left) and headings in
radians measured clockwise from north, both handled in the draw code.
"""

from __future__ import annotations

import json

from codeclash.replay.base import ReplayData, ReplayRenderer

PALETTE = ["#3B78FF", "#E5484D", "#30A46C", "#F5A623", "#8E4EC6", "#12A594", "#E93D82", "#F76B15"]

DRAW_JS = """
const ARENA = (function(){
  let W, H, S, COL, NAMES, cw, ch;
  const R = 18;  // Robocode robots are ~36px across
  function setup(cv, G){
    W = G.w; H = G.h; COL = G.colors; NAMES = G.names;
    S = Math.min(760 / W, 560 / H);
    cw = cv.width = Math.round(W * S); ch = cv.height = Math.round(H * S);
  }
  const px = (x) => x * S, py = (y) => ch - y * S;  // y-up world -> y-down canvas
  // Robocode heading: radians clockwise from north. Unit vector on the y-down canvas.
  const dx = (a) => Math.sin(a), dy = (a) => -Math.cos(a);
  function tank(ctx, u){
    const cx = px(u.x), cy = py(u.y), c = COL[u.i] || '#888', dead = (u.s === 'DEAD');
    // radar (thin, faint) and gun (barrel) as rays from the center
    if(!dead){
      ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx + dx(u.rh)*R*2.4, cy + dy(u.rh)*R*2.4); ctx.stroke();
    }
    // body: rounded rect rotated to bodyHeading (north = up before rotation)
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(u.bh);
    ctx.globalAlpha = dead ? 0.25 : 1;
    ctx.fillStyle = c;
    const w = R*1.5, h = R*1.7;
    ctx.beginPath(); ctx.roundRect(-w/2, -h/2, w, h, 4); ctx.fill();
    ctx.fillStyle = 'rgba(0,0,0,0.35)'; ctx.fillRect(-w/2, -h/2, w, h*0.18);  // "front" band
    ctx.restore();
    if(!dead){
      ctx.strokeStyle = '#e6edf3'; ctx.lineWidth = 3; ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx + dx(u.gh)*R*1.9, cy + dy(u.gh)*R*1.9); ctx.stroke();
      ctx.lineCap = 'butt';
    } else {
      ctx.strokeStyle = '#e5484d'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx-6,cy-6); ctx.lineTo(cx+6,cy+6); ctx.moveTo(cx+6,cy-6); ctx.lineTo(cx-6,cy+6); ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  function draw(ctx, cv, G, i){
    const f = G.frames[i];
    ctx.clearRect(0,0,cw,ch);
    ctx.fillStyle = 'rgba(255,255,255,0.02)'; ctx.fillRect(0,0,cw,ch);
    (f.b || []).forEach(b=>{
      const c = COL[b.o] || '#ffd21f', hit = (b.s === 'HIT_VICTIM' || b.s === 'EXPLODED');
      ctx.fillStyle = hit ? '#ffd21f' : c;
      ctx.globalAlpha = hit ? 0.9 : 1;
      ctx.beginPath(); ctx.arc(px(b.x), py(b.y), hit ? 5 : (1.6 + (b.p||1)*1.3), 0, 7); ctx.fill();
      ctx.globalAlpha = 1;
    });
    (f.u || []).forEach(u=>tank(ctx, u));
  }
  function side(G, i){
    const f = G.frames[i], ids = Object.keys(G.names);
    const byId = {}; (f.u || []).forEach(u=>byId[u.i]=u);
    return ids.map(id=>{
      const u = byId[id], dead = !u || u.s === 'DEAD' || u.e <= 0;
      const e = u ? Math.max(0, u.e) : 0, pct = Math.min(100, e);
      return `<div class="sn ${dead?'dead':''}"><span class="sw" style="background:${G.colors[id]}"></span>
        <span style="min-width:120px">${NAMES[id]}</span>
        <span class="hb"><span class="hf" style="width:${pct}%;background:${G.colors[id]}"></span></span>
        <span>${dead?'\\u2620':e.toFixed(0)}</span></div>`;
    }).join('') + '<div class="row muted" style="font-size:12px">bar = energy \\u00b7 white ray = gun \\u00b7 faint ray = radar</div>';
  }
  return {setup, draw, side};
})();
"""


class RoboCodeReplayer(ReplayRenderer):
    arena = "RoboCode"
    sim_glob = "sim_*.jsonl"
    DRAW_JS = DRAW_JS

    def parse(self, raw: bytes, players=None) -> ReplayData:
        rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        if not rows:
            return ReplayData(w=800, h=600, frames=[])
        header = rows[0]
        result = rows[-1] if isinstance(rows[-1], dict) and "winner" in rows[-1] else {}
        robots = {str(k): v for k, v in header.get("robots", {}).items()}
        # Stable color per robot id, in id order.
        colors = {rid: PALETTE[i % len(PALETTE)] for i, rid in enumerate(sorted(robots, key=int))}

        frames = []
        for r in rows:
            if not (isinstance(r, dict) and "t" in r):
                continue
            frames.append({"turn": r["t"], "u": r.get("u", []), "b": r.get("b", [])})

        return ReplayData(
            w=header.get("w", 800),
            h=header.get("h", 600),
            frames=frames,
            winner=result.get("winner"),
            draw=result.get("draw", False),
            extra={"colors": colors, "names": robots},
        )

    def peek_winner(self, raw: bytes, players=None) -> tuple[str | None, bool] | None:
        """Read just the trailing result line — no frame building."""
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
