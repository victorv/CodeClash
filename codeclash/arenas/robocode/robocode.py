import random
import re
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm.auto import tqdm

from codeclash.agents.player import Player
from codeclash.arenas.arena import CodeArena, RoundStats
from codeclash.arenas.robocode.trace import process_record, write_aggregate_trace
from codeclash.utils.environment import create_file_in_container

RC_FILE = Path("MyTank.java")
SIMS_PER_RUN = 10


def _java_pkg(name: str) -> str:
    """Robocode robots are Java classes and the agent name becomes their package.
    Map any non-identifier char to ``_`` and avoid a leading digit."""
    pkg = re.sub(r"[^0-9A-Za-z_]", "_", name)
    return pkg if not pkg[:1].isdigit() else f"b_{pkg}"


class RoboCodeArena(CodeArena):
    name: str = "RoboCode"
    description: str = f"""Robocode is a programming game where your code IS the tank. This is classic
Robocode (the `robocode.*` API compiled against robocode.jar) — NOT Robocode Tank Royale.
Your bot is a Java class that `extends robocode.Robot` (or `robocode.AdvancedRobot` for non-blocking
control): its `run()` method drives the tank in a loop (e.g. `ahead(100)`, `turnGunRight(90)`,
`fire(3)`), and it reacts to events like `onScannedRobot(ScannedRobotEvent)`, `onHitByBullet(...)`,
and `onHitWall(...)`. Move, aim the gun, sweep the radar, and fire to outlast other bots.
Your bot logic must be written in Java and located in the `robots/custom/` directory.
Keep the main bot class named `{str(RC_FILE)}`, but you can include additional Java files if you'd like."""
    default_args: dict = {
        "nodisplay": True,
        "nosound": True,
    }
    submission: str = "robots/custom/"

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.run_cmd_round: str = "./robocode.sh"
        for arg, val in self.game_config.get("args", self.default_args).items():
            if isinstance(val, bool):
                if val:
                    self.run_cmd_round += f" -{arg}"
            else:
                self.run_cmd_round += f" -{arg} {val}"

    def _get_battle_config(self) -> str:
        default_battle_config = {
            "battle": {
                "numRounds": SIMS_PER_RUN,
                "gunCoolingRate": 0.1,
                "rules": {"inactivityTime": 450, "hideEnemyNames": True},
            },
            "battleField": {"width": 800, "height": 600},
        }
        user_battle_config = self.game_config.get("battle", {})

        def merge_dicts(default, user):
            for key, value in user.items():
                if isinstance(value, dict) and key in default:
                    merge_dicts(default[key], value)
                else:
                    default[key] = value

        merge_dicts(default_battle_config, user_battle_config)

        # Turn battle config dict into strings
        battle_lines = ["#Battle Properties"]

        def dict_to_lines(d, prefix=""):
            for key, value in d.items():
                if isinstance(value, dict):
                    dict_to_lines(value, prefix + key + ".")
                else:
                    battle_lines.append(f"robocode.{prefix}{key}={value}")

        dict_to_lines(default_battle_config)
        return "\n".join(battle_lines)

    def _run_single_simulation(self, agents: list[Player], idx: int, cmd: str) -> str:
        rc_results = self.log_env / f"results_{idx}.txt"
        rc_record = self.log_env / f"record_{idx}.xml"
        cmd = f"{cmd} -results {rc_results}"
        # Always record one representative battle (idx 0) per round so the model and the replay
        # viewer get behavioral traces; record extra battles only if record_ratio is raised.
        # Each recorded battle's raw XML is later parsed into compact sim/trace files and deleted.
        if idx == 0 or random.random() < self.game_config.get("record_ratio", 0):
            cmd = f"{cmd} -recordXML {rc_record}"
        try:
            output = self.environment.execute(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            self.logger.warning(f"RoboCode simulation {idx} timed out: {cmd}")
            return ""
        if output["returncode"] != 0:
            self.logger.warning(
                f"RoboCode simulation {idx} failed with exit code {output['returncode']}:\n{output['output']}"
            )
        return output["output"]

    def execute_round(self, agents: list[Player]):
        for agent in agents:
            pkg = _java_pkg(agent.name)  # valid Java package (agent.name may contain hyphens)
            # Copy the agent codebase into the game codebase and compile it
            for cmd in [
                f"mkdir -p robots/{pkg}",
                f"cp -r /{agent.name}/robots/custom/* robots/{pkg}/",
                f"find robots/{pkg}/ -name '*.java' -exec sed -i 's/custom/{pkg}/g' {{}} +",
                f'javac -cp "libs/robocode.jar" robots/{pkg}/*.java',
            ]:
                self.environment.execute(cmd)

        # Create .battle file (robot fully-qualified name uses the sanitized package)
        selected_robots = ",".join([f"{_java_pkg(agent.name)}.{RC_FILE.stem}*" for agent in agents])
        # Use timestamp for unique battle file name since rounds are managed by tournament
        battle_file = f"{self.game_id}-battle{int(time.time())}.battle"
        battle_content = f"""#Battle Properties
{self._get_battle_config()}
robocode.battle.selectedRobots={selected_robots}
"""
        create_file_in_container(self.environment, content=battle_content, dest_path=f"battles/{battle_file}")

        # Run battle with results output to file
        cmd = f"{self.run_cmd_round} -battle {battle_file}"
        self.logger.info(f"Running game: {cmd}")
        with ThreadPoolExecutor(self.game_config.get("sim_concurrency", 5)) as executor:
            # Submit all simulations to the thread pool
            futures = [
                executor.submit(self._run_single_simulation, agents, idx, cmd)
                for idx in range(self.game_config.get("sims_per_round", 100) // SIMS_PER_RUN)
            ]

            # Collect results as they complete
            for future in tqdm(as_completed(futures), total=len(futures)):
                future.result()

    def copy_logs_from_env(self, round_num: int) -> None:
        """Copy the round's raw logs to the host, then distill each recorded battle's
        (enormous, ~30 MB) ``record_{idx}.xml`` into compact per-round ``sim_{n}.jsonl`` files
        (the single source for both the agent-facing behavioral trace and the replay viewer),
        and pool every recorded game into one readable ``trace.md``. The raw XML is deleted
        afterwards so it never bloats the logs shipped to the competing agents."""
        super().copy_logs_from_env(round_num)
        round_dir = self.log_round(round_num)
        summaries = []
        for xml in sorted(round_dir.glob("record_*.xml")):
            match = re.search(r"record_(\d+)\.xml", xml.name)
            if not match:
                continue
            try:
                summaries.extend(process_record(xml, round_dir, int(match.group(1)), SIMS_PER_RUN))
            except Exception as e:
                self.logger.warning(f"Failed to distill RoboCode sims from {xml.name}: {e}")
            finally:
                xml.unlink(missing_ok=True)
        if summaries:
            try:
                write_aggregate_trace(round_dir, summaries, self.game_config.get("sims_per_round", 100))
            except Exception as e:
                self.logger.warning(f"Failed to write aggregate RoboCode trace: {e}")

    def get_results(self, agents: list[Player], round_num: int, stats: RoundStats):
        scores = defaultdict(int)
        pkg_to_name = {_java_pkg(a.name): a.name for a in agents}  # map sanitized package back to agent.name
        for idx in range(self.game_config.get("sims_per_round", 100) // SIMS_PER_RUN):
            with open(self.log_round(round_num) / f"results_{idx}.txt") as f:
                result_output = f.read()
            lines = result_output.strip().split("\n")

            for line in lines:
                line = line.strip()
                if not re.match(r"^\d", line):
                    continue
                match = re.search(r"(\d+)\S+\:\s(\S+)\s+(\d+)", line)
                if match:
                    parsed = match.group(2).rsplit(".", 1)[0]
                    player = pkg_to_name.get(parsed, parsed)  # back to agent.name (player_stats key)
                    scores[player] += int(match.group(3))

        stats.winner = max(scores, key=scores.get)
        stats.scores = scores
        for player, score in scores.items():
            stats.player_stats[player].score = score

    def validate_code(self, agent: Player) -> tuple[bool, str | None]:
        if "robots" not in agent.environment.execute("ls")["output"]:
            return False, "There should be a `robots/` directory"
        if "custom" not in agent.environment.execute("ls robots")["output"]:
            return False, "There should be a `robots/custom/` directory"
        if str(RC_FILE) not in agent.environment.execute("ls robots/custom")["output"]:
            return False, (
                f"There should be a `robots/custom/{RC_FILE}` file. "
                f"You can include additional files, but the primary tank logic must be in `robots/custom/{RC_FILE}`"
            )
        response = agent.environment.execute('javac -cp "libs/robocode.jar" robots/custom/*.java')
        if response["returncode"] != 0:
            return False, f"Compilation error:\n{response['output']}"
        if f"{RC_FILE.stem}.class" not in agent.environment.execute("ls robots/custom")["output"]:
            return False, f"`{RC_FILE.stem}.class` not found after compilation"
        return True, None
