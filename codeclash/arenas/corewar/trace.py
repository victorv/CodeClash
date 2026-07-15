"""Distill a pMARS ``-T`` battle trace into replay ``sim_{i}.jsonl`` files + an aggregate ``trace.md``.

The arena records the first R scored battles with ``-T`` (see ``tracedisp.c`` in the mirror); those
still count toward the score, so replays are genuine scored battles. Only cell ownership/activity and
process counts are recorded, never instruction contents. Stream grammar (one record per line)::

    V 1 <coreSize> <warriors> <cycles>   W <idx> <pos> <len> <name>   R <round>
    e/w <cycle> <warrior> <addr>         s <cycle> <warrior> <tasks>
    x <cycle> <warrior> <tasks> <addr>   D <cycle> <warrior>

sim_{i}.jsonl: header {core,w,h,cycles,warriors,starts}; frames {t,c:[[addr,owner]],p,n,d}; result {winner,draw}.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

MAX_FRAMES = 300
# Deltas (not frame count) dominate size; keep an evenly-spaced sample when a frame exceeds this.
MAX_CELLS_PER_FRAME = 90


def grid_dims(core: int) -> tuple[int, int]:
    # Roughly-4:3 row-major grid holding `core` cells (addr -> (addr%w, addr//w)).
    w = max(1, round(math.sqrt(core * 4 / 3)))
    h = math.ceil(core / w)
    return w, h


@dataclass
class _WarriorAgg:
    """Running per-warrior behavioural stats over a single battle."""

    idx: int
    name: str
    position: int = 0
    length: int = 0
    execs: int = 0
    writes: int = 0
    peak_procs: int = 1
    death_cycle: int | None = None
    cells_owned: int = 0  # at end of battle


@dataclass
class TraceResult:
    """Everything distilled from one traced battle."""

    core: int
    gw: int
    gh: int
    cycles: int
    warriors: dict[int, str]  # idx -> display name
    starts: list[list[int]]  # [[position, length], ...] by warrior idx
    frames: list[dict]
    winner: str | None
    draw: bool
    stats: dict[int, _WarriorAgg] = field(default_factory=dict)
    total_cycles: int = 0
    cells_dropped: int = 0  # cell-changes shed by the per-frame cap (0 = fully lossless)


def _build_battle(
    core: int,
    cycles: int,
    names: dict[int, str],
    starts_by_idx: dict[int, list[int]],
    events: list[tuple],
    max_cycle: int,
) -> TraceResult:
    """Fold one battle's event list into downsampled frames + behavioural stats."""
    nwar = len(names)
    gw, gh = grid_dims(core or 1)
    stats = {i: _WarriorAgg(idx=i, name=names[i]) for i in range(nwar)}
    for i, se in starts_by_idx.items():
        if i in stats:
            stats[i].position, stats[i].length = se[0], se[1]

    stride = max(1, math.ceil((max_cycle + 1) / MAX_FRAMES))
    owner = [-1] * (core or 1)  # cumulative cell owner (for end coverage)
    procs = [1] * nwar
    alive = [True] * nwar
    last_pc = [starts_by_idx.get(i, [0])[0] for i in range(nwar)]

    frames: list[dict] = []
    fchanges: dict[int, int] = {}  # addr -> owner, for the frame being built
    cur_frame = 0
    dropped = 0  # cell-changes shed by the per-frame cap (reported, not silent)

    def flush(frame_idx: int) -> None:
        nonlocal dropped
        items = list(fchanges.items())
        if len(items) > MAX_CELLS_PER_FRAME:
            step = math.ceil(len(items) / MAX_CELLS_PER_FRAME)
            kept = items[::step]
            dropped += len(items) - len(kept)
            items = kept
        frames.append(
            {
                "t": frame_idx * stride,
                "c": [[a, o] for a, o in items],
                "p": list(last_pc),
                "n": list(procs),
                "d": [1 if a else 0 for a in alive],
            }
        )

    for tag, cyc, w, val in events:
        fidx = cyc // stride
        if fidx != cur_frame:
            flush(cur_frame)
            fchanges = {}
            cur_frame = fidx
        if tag == "e":
            stats[w].execs += 1
            last_pc[w] = val
            if 0 <= val < len(owner):
                owner[val] = w
                fchanges[val] = w
        elif tag == "w":
            stats[w].writes += 1
            if 0 <= val < len(owner):
                owner[val] = w
                fchanges[val] = w
        elif tag == "s":
            procs[w] = val
            stats[w].peak_procs = max(stats[w].peak_procs, val)
        elif tag == "x":
            procs[w] = val
        elif tag == "D":
            alive[w] = False
            procs[w] = 0
            if stats[w].death_cycle is None:
                stats[w].death_cycle = cyc
    flush(cur_frame)

    for a in owner:
        if a >= 0:
            stats[a].cells_owned += 1
    survivors = [i for i in range(nwar) if alive[i]]
    winner, draw = (None, True)
    if len(survivors) == 1:
        winner, draw = names[survivors[0]], False

    return TraceResult(
        core=core,
        gw=gw,
        gh=gh,
        cycles=cycles,
        warriors=names,
        starts=[starts_by_idx.get(i, [0, 0]) for i in range(nwar)],
        frames=frames,
        winner=winner,
        draw=draw,
        stats=stats,
        total_cycles=max_cycle,
        cells_dropped=dropped,
    )


def parse_battles(trace_path: Path, agent_names: list[str] | None = None) -> list[TraceResult]:
    # Split a `-T` trace (>=1 battles, delimited by R markers) into a TraceResult per battle.
    # `agent_names` (by warrior index) labels the replay; the in-file `;name` is a fallback.
    core = warriors_n = cycles = 0
    fallback_names: dict[int, str] = {}
    pending_starts: dict[int, list[int]] = {}
    battles: list[TraceResult] = []

    cur_events: list[tuple] | None = None
    cur_starts: dict[int, list[int]] = {}
    cur_max = 0

    def names_for() -> dict[int, str]:
        nwar = warriors_n or (max(fallback_names) + 1 if fallback_names else 1)
        out = {}
        for i in range(nwar):
            if agent_names and i < len(agent_names):
                out[i] = agent_names[i]
            else:
                out[i] = fallback_names.get(i, f"warrior{i}")
        return out

    def finish() -> None:
        nonlocal cur_events
        if cur_events is None:
            return
        battles.append(_build_battle(core, cycles, names_for(), cur_starts, cur_events, cur_max))
        cur_events = None

    for line in trace_path.read_text().splitlines():
        if not line:
            continue
        parts = line.split()
        tag = parts[0]
        if tag == "V":
            core, warriors_n, cycles = int(parts[2]), int(parts[3]), int(parts[4])
        elif tag == "W":
            idx = int(parts[1])
            pending_starts[idx] = [int(parts[2]), int(parts[3])]
            fallback_names[idx] = parts[4] if len(parts) > 4 else f"warrior{idx}"
        elif tag == "R":
            finish()
            cur_events, cur_starts, cur_max = [], dict(pending_starts), 0
            pending_starts = {}
        elif cur_events is not None:
            cyc = int(parts[1])
            cur_max = max(cur_max, cyc)
            if tag in ("e", "w", "s", "x"):
                cur_events.append((tag, cyc, int(parts[2]), int(parts[3])))
            elif tag == "D":
                cur_events.append(("D", cyc, int(parts[2]), 0))
    finish()
    return battles


def write_sim(path: Path, tr: TraceResult) -> None:
    """Write one battle's ``sim_{i}.jsonl`` (header + frames + result)."""
    lines = [
        json.dumps(
            {
                "core": tr.core,
                "w": tr.gw,
                "h": tr.gh,
                "cycles": tr.cycles,
                "warriors": {str(k): v for k, v in tr.warriors.items()},
                "starts": tr.starts,
            }
        )
    ]
    for f in tr.frames:
        lines.append(json.dumps(f, separators=(",", ":")))
    lines.append(json.dumps({"winner": tr.winner, "draw": tr.draw}))
    path.write_text("\n".join(lines) + "\n")


def write_trace_md(path: Path, battles: list[TraceResult]) -> None:
    """A short, human-readable, source-free summary aggregated over all traced battles."""
    if not battles:
        return
    names = battles[0].warriors
    core = battles[0].core or 1
    wins = {i: 0 for i in names}
    ties = 0
    for b in battles:
        winner_idx = next((i for i, n in names.items() if not b.draw and n == b.winner), None)
        if winner_idx is None:
            ties += 1
        else:
            wins[winner_idx] += 1

    out = ["# CoreWar battle trace", ""]
    out.append(f"{len(battles)} traced battle(s), core size {core}, {len(names)} warriors.")
    out.append("")
    out.append(f"**Win tally across traced battles** (ties: {ties})")
    out.append("")
    out.append("| warrior | wins | avg core owned | avg peak procs | times eliminated |")
    out.append("|---|--:|--:|--:|--:|")
    for i in sorted(names):
        cov = sum(b.stats[i].cells_owned for b in battles) / len(battles) / core * 100
        pk = sum(b.stats[i].peak_procs for b in battles) / len(battles)
        died = sum(1 for b in battles if b.stats[i].death_cycle is not None)
        out.append(f"| {names[i]} | {wins[i]} | {cov:.1f}% | {pk:.1f} | {died} |")
    out.append("")
    # Per-battle index so a reader can jump to a specific replay.
    out.append("| battle (sim) | winner | steps |")
    out.append("|---|---|--:|")
    for idx, b in enumerate(battles):
        out.append(f"| {idx} | {'tie' if b.draw else b.winner} | {b.total_cycles} |")
    out.append("")
    path.write_text("\n".join(out) + "\n")


def distill_trace(trace_path: Path, out_dir: Path, agent_names: list[str]) -> list[TraceResult] | None:
    # Write one sim_{i}.jsonl per battle + an aggregate trace.md; None if trace empty/absent.
    if not trace_path.exists() or trace_path.stat().st_size == 0:
        return None
    battles = [b for b in parse_battles(trace_path, agent_names) if b.frames]
    if not battles:
        return None
    for i, b in enumerate(battles):
        write_sim(out_dir / f"sim_{i}.jsonl", b)
    write_trace_md(out_dir / "trace.md", battles)
    return battles
