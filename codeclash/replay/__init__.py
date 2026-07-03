"""Replay layer: turn recorded tournament games into browsable playback.

``codeclash replay <folder>`` is the CLI consumer of this package. The reusable core
lives here (:mod:`codeclash.replay.base`) plus per-arena renderers under
``codeclash/arenas/<arena>/replay.py``; a future viewer integration can consume the
same renderers.
"""

from __future__ import annotations

import json
from pathlib import Path

from codeclash.replay.base import (
    GameRef,
    ReplayData,
    ReplayRenderer,
    TournamentInfo,
    build_index,
    build_page,
    discover_games,
    read_sim,
)

__all__ = [
    "GameRef",
    "ReplayData",
    "ReplayRenderer",
    "TournamentInfo",
    "build_index",
    "build_page",
    "discover_games",
    "get_replayer",
    "load_tournament",
    "read_sim",
]


def get_replayer(arena: str) -> ReplayRenderer | None:
    """Return the renderer for an arena, or ``None`` if none exists yet.

    Imports are lazy so pulling in one arena's replay code never drags in the others.
    """
    if arena == "BattleSnake":
        from codeclash.arenas.battlesnake.replay import BattleSnakeReplayer

        return BattleSnakeReplayer()
    if arena == "RobotRumble":
        from codeclash.arenas.robotrumble.replay import RobotRumbleReplayer

        return RobotRumbleReplayer()
    return None


def _player_model(player: dict) -> str | None:
    """Extract a display model name from a player config (mirrors the viewer)."""
    config = player.get("config", {}) or {}
    model = config.get("model")
    if isinstance(model, dict):
        return model.get("model_name")
    return model


def _arena_from_name(folder: Path) -> str:
    """Derive the arena from a tournament folder name.

    e.g. ``PvpTournament.RobotRumble.r1.s1.p2.flail.luisa.260702175500`` -> ``RobotRumble``.
    """
    parts = folder.name.split(".")
    return parts[1] if len(parts) > 1 else ""


def load_tournament(folder: Path) -> TournamentInfo:
    """Read a tournament folder: metadata (if present) plus every discoverable game.

    The arena comes from ``metadata.json`` when available, otherwise from the folder name
    (which encodes it, e.g. ``PvpTournament.RobotRumble.r1.s1.p2...``).
    """
    folder = Path(folder)
    meta_path = folder / "metadata.json"
    if not meta_path.exists():
        arena = _arena_from_name(folder)
        renderer = get_replayer(arena)
        games = discover_games(folder, renderer.sim_glob) if renderer else []
        return TournamentInfo(
            folder=folder, arena=arena, players=[], rounds=None, sims_per_round=None, round_winners={}, games=games
        )
    meta = json.loads(meta_path.read_text())
    config = meta.get("config", {})
    game = config.get("game", {})
    arena = game.get("name") or meta.get("game", {}).get("name") or _arena_from_name(folder)

    players = [
        {"name": p.get("name", "?"), "model": _player_model(p)}
        for p in config.get("players", [])
        if isinstance(p, dict)
    ]
    round_winners = {int(k): v.get("winner") for k, v in meta.get("round_stats", {}).items()}

    renderer = get_replayer(arena)
    games = discover_games(folder, renderer.sim_glob) if renderer else []

    return TournamentInfo(
        folder=folder,
        arena=arena,
        players=players,
        rounds=config.get("tournament", {}).get("rounds"),
        sims_per_round=game.get("sims_per_round"),
        round_winners=round_winners,
        games=games,
    )
