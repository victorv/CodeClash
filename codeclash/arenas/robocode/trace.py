"""Turn classic Robocode ``-recordXML`` battle records into compact, consumable traces.

A single ``record_{idx}.xml`` from Robocode is enormous (tens of MB, ~200k lines for a
10-round battle) and leaks raw internals — it is neither consumable by a competing model
nor a good replay source. This module streams that XML once and emits, for each Robocode
round, a lean ``sim_{n}.jsonl`` (the single source of truth for BOTH the agent-facing
behavioral trace and the replay viewer, mirroring BattleSnake's ``sim_*.jsonl``). The arena
then pools every recorded game in the round into one human-readable ``trace.md`` (per-tank win
rate, accuracy, movement/aggression averages + a per-game index) — see
:func:`write_aggregate_trace`.

The ``sim_{n}.jsonl`` layout (one JSON object per line):

* header  ``{"w", "h", "round", "robots": {id: name}}``
* frames  ``{"t", "u": [{"i","x","y","e","bh","gh","rh","v","s"}], "b": [{"o","x","y","p","s"}]}``
* result  ``{"winner": name|null, "draw": bool}``

where ``u`` are robot (unit) snapshots keyed by Robocode robot id, ``b`` are bullets (``o``
is the owning robot id, ``p`` the fire power), headings are radians, and ``s`` is the
Robocode state string (``ACTIVE``/``DEAD``/``HIT_WALL``/``HIT_ROBOT`` for robots,
``MOVING``/``HIT_VICTIM``/``HIT_WALL``/``HIT_BULLET``/``EXPLODED`` for bullets).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import ParseError, iterparse

# Cap stored frames per round so a 1500+ turn round stays small and the replay scrubs
# smoothly; behavioral stats are always computed over *every* turn, independent of stride.
MAX_FRAMES_PER_ROUND = 800


def _round_to(v: str | None, ndigits: int) -> float:
    return round(float(v), ndigits) if v is not None else 0.0


def _clean_name(name: str) -> str:
    """Strip Robocode's fixed primary-class + selection-glob suffix so a robot reads as just its
    CodeClash agent name (``spinbot.MyTank*`` -> ``spinbot``), matching how ``get_results`` keys
    scores. Only the exact ``.MyTank``/``*`` suffix is removed, so names without it are untouched
    and agent names containing dots are preserved."""
    n = name.rstrip("*")
    if n.endswith(".MyTank"):  # RC_FILE.stem; the only robot each agent fields
        n = n[: -len(".MyTank")]
    return n or name


@dataclass
class _RobotAgg:
    """Running per-robot behavioral stats for one round (computed over all turns)."""

    name: str
    shots: int = 0
    hits: int = 0
    wall_hits: int = 0
    rams: int = 0
    min_energy: float = 100.0
    final_energy: float = 0.0
    death_turn: int | None = None
    vel_sum: float = 0.0
    vel_n: int = 0
    _prev_state: str = "ACTIVE"

    def observe(self, turn: int, state: str, energy: float, velocity: float) -> None:
        self.final_energy = energy
        self.min_energy = min(self.min_energy, energy)
        self.vel_sum += abs(velocity)
        self.vel_n += 1
        if state == "HIT_WALL" and self._prev_state != "HIT_WALL":
            self.wall_hits += 1
        if state == "HIT_ROBOT" and self._prev_state != "HIT_ROBOT":
            self.rams += 1
        if state == "DEAD" and self.death_turn is None:
            self.death_turn = turn
        self._prev_state = state

    @property
    def avg_speed(self) -> float:
        return self.vel_sum / self.vel_n if self.vel_n else 0.0

    @property
    def accuracy(self) -> float:
        return self.hits / self.shots if self.shots else 0.0


@dataclass
class RoundResult:
    """Everything produced for a single Robocode round."""

    round: int
    width: int
    height: int
    robots: dict[int, str]  # id -> name
    frames: list[dict]
    winner: str | None
    draw: bool
    stats: dict[int, _RobotAgg] = field(default_factory=dict)
    turns: int = 0
    avg_gap: float = 0.0  # average distance between the two tanks


def iter_rounds(xml_path: Path):
    """Stream a Robocode record XML, yielding one :class:`RoundResult` per round.

    Uses ``iterparse`` and clears each turn element so memory stays bounded regardless of
    battle length.
    """
    width = height = 0
    cur: int | None = None
    robots: dict[int, str] = {}
    frames: list[dict] = []
    stats: dict[int, _RobotAgg] = {}
    seen_bullets: set[str] = set()
    hit_bullets: set[str] = set()
    turn_count = 0
    gap_sum = 0.0
    gap_n = 0
    stride = 1
    last_pos: dict[int, tuple[float, str]] = {}  # id -> (energy, state) on the latest turn

    def finish(rnd: int) -> RoundResult:
        # Winner = the sole robot still alive (not DEAD, energy > 0) on the last recorded turn.
        alive = [rid for rid, (energy, state) in last_pos.items() if state != "DEAD" and energy > 0]
        winner, draw = (None, True)
        if len(alive) == 1:
            winner, draw = robots.get(alive[0]), False
        # Always keep the final turn as the last frame, even if the stride skipped it.
        if frames and last_frame is not None and frames[-1]["t"] != last_frame["t"]:
            frames.append(last_frame)
        return RoundResult(
            round=rnd,
            width=width,
            height=height,
            robots=dict(robots),
            frames=list(frames),
            winner=winner,
            draw=draw,
            stats=dict(stats),
            turns=turn_count,
            avg_gap=(gap_sum / gap_n if gap_n else 0.0),
        )

    last_frame = None
    context = iterparse(str(xml_path), events=("end",))
    # Robocode appends a 22-byte empty-ZIP end-of-central-directory marker after the closing
    # </record>, which trips strict XML parsing on the final byte. All real turns are already
    # parsed by then, so we tolerate a trailing ParseError and still flush the last round.
    try:
        for _event, elem in context:
            tag = elem.tag
            if tag == "rules":
                width = int(float(elem.get("battlefieldWidth", "800")))
                height = int(float(elem.get("battlefieldHeight", "600")))
            elif tag == "turn":
                rnd = int(elem.get("round", "0"))
                turn = int(elem.get("turn", "0"))
                if rnd != cur:
                    if cur is not None:
                        yield finish(cur)
                    # reset per-round accumulators
                    cur = rnd
                    robots = {}
                    frames = []
                    stats = {}
                    seen_bullets = set()
                    hit_bullets = set()
                    turn_count = 0
                    gap_sum = 0.0
                    gap_n = 0
                    stride = 1
                    last_pos = {}
                    last_frame = None

                robots_el = elem.find("robots")
                bullets_el = elem.find("bullets")

                u = []
                positions: list[tuple[float, float]] = []
                if robots_el is not None:
                    for r in robots_el:
                        rid = int(r.get("id", "0"))
                        name = _clean_name(r.get("name") or r.get("teamName") or f"robot{rid}")
                        robots.setdefault(rid, name)
                        state = r.get("state", "ACTIVE")
                        energy = _round_to(r.get("energy"), 1)
                        x = _round_to(r.get("x"), 1)
                        y = _round_to(r.get("y"), 1)
                        velocity = _round_to(r.get("velocity"), 2)
                        agg = stats.get(rid)
                        if agg is None:
                            agg = stats[rid] = _RobotAgg(name=name)
                        agg.observe(turn, state, energy, velocity)
                        positions.append((x, y))
                        last_pos[rid] = (energy, state)
                        u.append(
                            {
                                "i": rid,
                                "x": x,
                                "y": y,
                                "e": energy,
                                "bh": _round_to(r.get("bodyHeading"), 3),
                                "gh": _round_to(r.get("gunHeading"), 3),
                                "rh": _round_to(r.get("radarHeading"), 3),
                                "v": velocity,
                                "s": state,
                            }
                        )

                b = []
                if bullets_el is not None:
                    for bl in bullets_el:
                        bid = bl.get("id", "")
                        owner = int(bid.split("-", 1)[0]) if "-" in bid else 0
                        state = bl.get("state", "MOVING")
                        if bid and bid not in seen_bullets:
                            seen_bullets.add(bid)
                            if owner in stats:
                                stats[owner].shots += 1
                        if state == "HIT_VICTIM" and bid and bid not in hit_bullets:
                            hit_bullets.add(bid)
                            if owner in stats:
                                stats[owner].hits += 1
                        b.append(
                            {
                                "o": owner,
                                "x": _round_to(bl.get("x"), 1),
                                "y": _round_to(bl.get("y"), 1),
                                "p": _round_to(bl.get("power"), 1),
                                "s": state,
                            }
                        )

                if len(positions) == 2:
                    gap_sum += math.dist(positions[0], positions[1])
                    gap_n += 1

                frame = {"t": turn, "u": u, "b": b}
                last_frame = frame
                # Downsample stored frames on the fly once we know the round is long.
                if turn == 0 or (turn % stride == 0):
                    frames.append(frame)
                if len(frames) > MAX_FRAMES_PER_ROUND:
                    # Round is long: double the stride and thin what we've stored so far.
                    stride *= 2
                    frames = [f for k, f in enumerate(frames) if k % 2 == 0]
                turn_count = turn + 1
                elem.clear()
    except ParseError:
        pass

    if cur is not None:
        yield finish(cur)


def write_round_sim(path: Path, rr: RoundResult) -> None:
    """Write one round's ``sim_{n}.jsonl`` (header + frames + result)."""
    lines = [
        json.dumps(
            {"w": rr.width, "h": rr.height, "round": rr.round, "robots": {str(k): v for k, v in rr.robots.items()}}
        )
    ]
    for f in rr.frames:
        lines.append(json.dumps(f, separators=(",", ":")))
    lines.append(json.dumps({"winner": rr.winner, "draw": rr.draw}))
    path.write_text("\n".join(lines) + "\n")


@dataclass
class GameSummary:
    """A lightweight, frame-free summary of one recorded game, for the aggregate trace."""

    sim_idx: int
    robots: dict[int, str]
    winner: str | None
    draw: bool
    turns: int
    stats: dict[int, _RobotAgg]


def process_record(xml_path: Path, round_dir: Path, idx: int, sims_per_run: int) -> list[GameSummary]:
    """Parse one ``record_{idx}.xml`` into per-round ``sim_{n}.jsonl`` files.

    ``sim`` indices are global across the battle (``idx * sims_per_run + round``) so they stay
    unique within the round folder. Returns a frame-free :class:`GameSummary` per game (the
    frames are written to disk, not held in memory) so the caller can build one aggregate
    trace over every recorded game in the round.
    """
    summaries: list[GameSummary] = []
    for rr in iter_rounds(xml_path):
        sim_idx = idx * sims_per_run + rr.round
        write_round_sim(round_dir / f"sim_{sim_idx}.jsonl", rr)
        summaries.append(
            GameSummary(
                sim_idx=sim_idx,
                robots=rr.robots,
                winner=rr.winner,
                draw=rr.draw,
                turns=rr.turns,
                stats=rr.stats,
            )
        )
    return summaries


def write_aggregate_trace(round_dir: Path, games: list[GameSummary], games_played: int) -> None:
    """Write a single ``trace.md`` pooling behavioral stats over every recorded game in the round.

    This is the agent-facing digest: rather than one file per battle (an artifact of how sims
    are batched), it gives per-tank win rate, survival, accuracy and movement/aggression
    averaged across the whole recorded sample, plus a per-game index into the ``sim_*.jsonl``.
    """
    path = round_dir / "trace.md"
    if not games:
        path.write_text("# RoboCode round trace\n\nNo battles were recorded this round.\n")
        return

    # Pool per-tank stats across every recorded game, keyed by tank name (stable across battles).
    agg: dict[str, dict] = {}
    order: list[str] = []
    for g in games:
        for a in g.stats.values():
            d = agg.get(a.name)
            if d is None:
                d = agg[a.name] = dict(
                    games=0, wins=0, survivals=0, shots=0, hits=0, walls=0, rams=0, speed=0.0, min_e=0.0, deaths=[]
                )
                order.append(a.name)
            d["games"] += 1
            d["wins"] += int(not g.draw and g.winner == a.name)
            d["shots"] += a.shots
            d["hits"] += a.hits
            d["walls"] += a.wall_hits
            d["rams"] += a.rams
            d["speed"] += a.avg_speed
            d["min_e"] += a.min_energy
            if a.death_turn is None:
                d["survivals"] += 1
            else:
                d["deaths"].append(a.death_turn)

    n = len(games)
    turns = [g.turns for g in games]
    draws = sum(1 for g in games if g.draw)

    out = ["# RoboCode round trace", ""]
    out.append(f"Behavioral summary pooled over **{n} recorded game(s)** (of {games_played} played this round).")
    out.append(
        f"Game length: min {min(turns)}, avg {sum(turns) // n}, max {max(turns)} turns."
        + (f" Draws: {draws}." if draws else "")
    )
    out.append("")
    out.append("Stats are per-game averages across the recorded sample; accuracy is total hits / total shots.")
    out.append("")
    out.append(
        "| tank | win rate | survival | avg shots | accuracy | avg speed | avg walls/game | avg rams/game | avg min energy | avg death turn |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for nm in sorted(order, key=lambda k: agg[k]["wins"] / agg[k]["games"], reverse=True):
        d = agg[nm]
        gp = d["games"]
        acc = d["hits"] / d["shots"] if d["shots"] else 0.0
        avg_death = f"{sum(d['deaths']) // len(d['deaths'])}" if d["deaths"] else "—"
        out.append(
            f"| `{nm}` | {d['wins'] / gp:.0%} ({d['wins']}/{gp}) | {d['survivals'] / gp:.0%} | "
            f"{d['shots'] / gp:.1f} | {acc:.0%} | {d['speed'] / gp:.1f} | {d['walls'] / gp:.1f} | "
            f"{d['rams'] / gp:.1f} | {d['min_e'] / gp:.0f} | {avg_death} |"
        )
    out.append("")

    # Per-game index so a reader can jump to a specific replayable sim.
    out.append("## Per-game results")
    out.append("")
    out.append("| sim | winner | turns |")
    out.append("|---|---|---|")
    for g in sorted(games, key=lambda g: g.sim_idx):
        winner = "TIE" if g.draw else (g.winner or "—")
        out.append(f"| `sim_{g.sim_idx}.jsonl` | {winner} | {g.turns} |")
    out.append("")

    path.write_text("\n".join(out))
