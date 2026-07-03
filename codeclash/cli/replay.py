"""`codeclash replay` — turn a tournament log folder into browsable game playback.

Points at a tournament folder (the one holding ``metadata.json`` + ``rounds/``); the arena
is read from the metadata (or the folder name) and dispatched to its renderer. Games are
discovered under ``rounds/`` (including ``round_<R>.tar.gz`` archives).
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

import typer

from codeclash.replay import base, get_replayer, load_tournament

# Cap on how many games `--all` will pre-generate before it stops and says so.
ALL_CAP = 300


def _bad(msg: str):
    typer.echo(f"Error: {msg}", err=True)
    raise typer.Exit(2)


def replay(
    folder: Path = typer.Argument(..., help="Tournament log folder (contains metadata.json + rounds/)."),
    round: int | None = typer.Option(None, "--round", "-r", help="Round to replay (alone: every sim in the round)."),
    sim: int | None = typer.Option(None, "--sim", "-s", help="Sim index (alone: that sim across every round)."),
    all_games: bool = typer.Option(False, "--all", help="Pre-generate a playback page for every game."),
    ascii_out: bool = typer.Option(False, "--ascii", help="Dump a single game to the terminal instead of HTML."),
    open_browser: bool = typer.Option(False, "--open", "-o", help="Open the result in a browser when done."),
):
    """Build a browsable replay site (index + on-demand per-game pages) for a tournament.

    [dim]• codeclash replay logs/<tournament-folder>          # build the browsable index[/dim]
    [dim]• codeclash replay logs/<folder> -r 1 -s 0 --open    # one game, open in browser[/dim]
    [dim]• codeclash replay logs/<folder> -r 1                # every sim in round 1[/dim]
    [dim]• codeclash replay logs/<folder> --all              # pre-generate every game[/dim]
    [dim]• codeclash replay logs/<folder> --ascii            # dump a game to the terminal[/dim]
    """
    # Reject contradictory modes up front, rather than silently picking one.
    if ascii_out and all_games:
        _bad("use either --ascii or --all, not both.")
    if ascii_out and open_browser:
        _bad("--ascii writes nothing to open; drop --open.")
    if all_games and (round is not None or sim is not None):
        _bad("--all replays every game — drop -r/-s (or omit --all to pick specific games).")

    tour = load_tournament(folder)
    renderer = get_replayer(tour.arena)
    if renderer is None:
        typer.echo(f"No replayer for {tour.arena or 'this arena'} yet.")
        raise typer.Exit(1)
    if not tour.games:
        typer.echo(f"No sim files found under {folder} (looked for {renderer.sim_glob}).")
        raise typer.Exit(1)

    def tour_payload(r: int, s: int) -> dict:
        return {
            "arena": tour.arena,
            "players": tour.players,
            "round": r,
            "sim": s,
            "round_winner": tour.round_winners.get(r),
        }

    def load(game):
        return renderer.parse(base.read_sim(game), tour.players)

    # ascii: a single game to the terminal, no files written.
    if ascii_out:
        g0 = tour.games[0]
        r = round if round is not None else g0.round
        s = sim if sim is not None else g0.sim
        game = next((g for g in tour.games if g.round == r and g.sim == s), None)
        if game is None:
            typer.echo(f"No game at round {r}, sim {s}.")
            raise typer.Exit(1)
        typer.echo(renderer.ascii(load(game)))
        return

    # Which games to generate as HTML pages:
    #   -r R -s S  -> that one game
    #   -r R       -> every sim in round R
    #   -s S       -> sim S across every round
    #   --all      -> every game (capped)
    #   (none)     -> index only
    if all_games:
        todo = tour.games[:ALL_CAP]
    elif round is not None and sim is not None:
        todo = [g for g in tour.games if g.round == round and g.sim == sim]
    elif round is not None:
        todo = [g for g in tour.games if g.round == round]
    elif sim is not None:
        todo = [g for g in tour.games if g.sim == sim]
    else:
        todo = []

    if (round is not None or sim is not None) and not todo:
        typer.echo(f"No games match round={round} sim={sim}. Run `codeclash replay {folder}` to list games.")
        raise typer.Exit(1)

    replay_dir = folder / "replay"
    base.write_assets(replay_dir, renderer)
    for g in todo:
        (replay_dir / f"r{g.round}_s{g.sim}.html").write_text(
            base.build_sim_stub(load(g), tour_payload(g.round, g.sim), renderer)
        )
    # Always (re)build the index so newly generated pages show as watchable.
    index = replay_dir / "index.html"
    index.write_text(base.build_index(tour, replay_dir))

    if todo:
        typer.echo(f"Generated {len(todo)} game page(s). Index: {index}")
        if all_games and len(tour.games) > ALL_CAP:
            typer.echo(f"Note: capped at {ALL_CAP} of {len(tour.games)} games — pass -r/-s for the rest.")
    else:
        typer.echo(f"Replay index: {index}")
        typer.echo(f"{len(tour.games)} games found. Open a specific one with `-r <round> [-s <sim>]`.")

    if open_browser:
        # Open the single generated page if there's exactly one, else the index.
        target = replay_dir / f"r{todo[0].round}_s{todo[0].sim}.html" if len(todo) == 1 else index
        try:
            webbrowser.open(target.resolve().as_uri())
        except Exception:
            typer.echo(f"(couldn't open a browser automatically — open {target} manually)")
