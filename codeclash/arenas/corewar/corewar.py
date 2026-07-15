import re
import shlex
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from codeclash.agents.player import Player
from codeclash.arenas.arena import CodeArena, RoundStats
from codeclash.arenas.corewar.trace import distill_trace
from codeclash.constants import RESULT_TIE

COREWAR_LOG = "sim_{idx}.log"
TRACE_RAW = "trace_raw.txt"


class CoreWarArena(CodeArena):
    name: str = "CoreWar"
    description: str = """CoreWar is a programming battle where you write "warriors" in an assembly-like language called Redcode to compete within a virtual machine (MARS), aiming to eliminate your rivals by making their code self-terminate.
Victory comes from crafting clever tactics—replicators, scanners, bombers—that exploit memory layout and instruction timing to control the core.

Reading the logs: each round's score is computed over many simulated battles, but only a sample of them are saved as replay traces (`sim_*.jsonl`) in `/logs/` due to storage limits. The score reflects every battle played, not just the replayed subset, so treat the replays as representative examples rather than the full basis of the result."""
    submission: str = "warrior.red"

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        # Always run in brief mode (`-b`). Without it, pmars prints disassembled listing of
        # every warrior, which leaks opponent
        self.run_cmd_round: str = "./src/pmars -b"
        for arg, val in self.game_config.get("args", self.default_args).items():
            if isinstance(val, bool):
                if val:
                    self.run_cmd_round += f" -{arg}"
            else:
                self.run_cmd_round += f" -{arg} {val}"

    def _record_count(self) -> int:
        # Bounded subset of scored battles to record for replay (recording is the costly part).
        sims = self.game_config["sims_per_round"]
        return max(1, min(sims, self.game_config.get("record_battles", 100)))

    def _run_single_simulation(self, agents: list[Player], idx: int):
        # Shift agents by idx to vary starting positions
        agents = agents[idx:] + agents[:idx]
        args = [f"/{agent.name}/{self.submission}" for agent in agents]
        n = self.game_config["sims_per_round"]
        log = self.log_env / COREWAR_LOG.format(idx=idx)
        if idx == 0:
            # Record R scored battles with -T + the rest plain; both score blocks append to one
            # log (get_results sums them), so replays are genuine scored battles. i == agents[i].
            r = self._record_count()
            self._trace_agent_names = [agent.name for agent in agents]
            parts = [f"{self.run_cmd_round} {shlex.join(args)} -r {r} -T {self.log_env / TRACE_RAW} >> {log}"]
            if n - r > 0:
                parts.append(f"{self.run_cmd_round} {shlex.join(args)} -r {n - r} >> {log}")
            cmd = f"rm -f {log}; " + "; ".join(parts) + ";"
        else:
            cmd = f"{self.run_cmd_round} {shlex.join(args)} -r {n} > {log};"
        self.logger.info(f"Running game: {cmd}")
        response = self.environment.execute(cmd)
        assert response["returncode"] == 0, response

    def execute_round(self, agents: list[Player]):
        with ThreadPoolExecutor(self.game_config.get("sim_concurrency", 4)) as executor:
            futures = [executor.submit(self._run_single_simulation, agents, idx) for idx in range(len(agents))]
            for future in as_completed(futures):
                future.result()

    def copy_logs_from_env(self, round_num: int) -> None:
        # Distill the -T battles into sim_{i}.jsonl + trace.md on the host, then drop the raw stream.
        super().copy_logs_from_env(round_num)
        raw = self.log_round(round_num) / TRACE_RAW
        if not raw.exists():
            return
        try:
            battles = distill_trace(raw, self.log_round(round_num), getattr(self, "_trace_agent_names", []))
            if battles:
                dropped = sum(b.cells_dropped for b in battles)
                note = f" ({dropped} cell-changes sampled out for size)" if dropped else ""
                self.logger.info(f"CoreWar trace distilled: {len(battles)} replay(s) for round {round_num}{note}")
            else:
                self.logger.warning("CoreWar trace was empty; no replay for this round")
        except Exception as e:
            self.logger.warning(f"Failed to distill CoreWar trace: {e}")
        finally:
            raw.unlink(missing_ok=True)

    def get_results(self, agents: list[Player], round_num: int, stats: RoundStats):
        scores, wins = defaultdict(int), defaultdict(int)
        score_pat = re.compile(r".*\sby\s.*\sscores\s(\d+)")
        for idx in range(len(agents)):
            shift = agents[idx:] + agents[:idx]  # Match the command-line warrior order in _run_single_simulation
            with open(self.log_round(round_num) / COREWAR_LOG.format(idx=idx)) as f:
                result_output = f.read()

            lines = result_output.splitlines()
            # Sum across every "…scores…" + "Results:" block (the record shift writes two).
            pending: list[int] = []
            saw_results = False
            for line in lines:
                m = score_pat.search(line)
                if m:
                    pending.append(int(m.group(1)))
                elif line.strip().startswith("Results:"):
                    saw_results = True
                    for i, v in enumerate(pending[-len(shift) :]):  # the N score lines of this block
                        scores[shift[i].name] += v
                    for i, w in enumerate(line.strip()[len("Results:") :].split()[:-1]):  # omit ties
                        if i < len(shift):
                            wins[shift[i].name] += int(w)
                    pending = []
            if not saw_results:
                self.logger.error(f"No 'Results:' line in {COREWAR_LOG.format(idx=idx)} for round {round_num}")

        if len(wins) != len(agents):
            # Should not happen
            self.logger.error(f"Have {len(wins)} wins but {len(agents)} agents")

        # Bookkeeping
        stats.scores = {a.name: wins[a.name] for a in agents}
        for a in agents:
            stats.player_stats[a.name].score = wins[a.name]

        # Determine overall winner by highest wins, then highest score
        max_wins = max(wins.values(), default=0)
        potential_winners = [name for name, w in wins.items() if w == max_wins]
        if len(potential_winners) == 1:
            stats.winner = potential_winners[0]
        else:
            # Tie-break by score
            max_score = -1
            winner = RESULT_TIE
            for name in potential_winners:
                if scores[name] > max_score:
                    max_score = scores[name]
                    winner = name
                elif scores[name] == max_score:
                    winner = RESULT_TIE
            stats.winner = winner

    def validate_code(self, agent: Player) -> tuple[bool, str | None]:
        if self.submission not in agent.environment.execute("ls")["output"]:
            return False, f"There should be a `{self.submission}` file"
        # Play game against a simple default bot to ensure it runs. Pass -r with the real
        # round count so warriors that `;assert ROUNDS > 1` (a common idiom) validate the same
        # way they'll actually run, instead of failing on pmars's default single round.
        test_run_cmd = f"{self.run_cmd_round} {self.submission} /home/dwarf.red -r {self.game_config['sims_per_round']}"
        test_run = agent.environment.execute(test_run_cmd, timeout=60)["output"]
        if any([l.startswith("Error") for l in test_run.split("\n")]):
            return False, f"The `{self.submission}` file is malformed (Ran `{test_run_cmd}`):\n{test_run}"
        return True, None
