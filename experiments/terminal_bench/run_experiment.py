"""Experiment driver for the Terminal-Bench evaluation sweep.

Phases (docs/launch-plan.md):
  oracle   - validate environment + verifier with the oracle agent (no model)
  pilot    - one attempt per agent on the pilot task (Gate A)
  sweep    - tasks x agents x attempts (Phase 3; requires config tasks list)
  ingest   - ingest harbor job dirs into AGR (skips oracle jobs)
  status   - summarise the ledger
  import   - adopt pre-existing job dirs in jobs_dir into the ledger

Outcomes: PASS / FAIL (verifier), INFRA (environment/harness failure -
retried, never counted toward the 36), AUTH_ERROR / RATE_LIMIT / ERROR.

Everything is stdlib. State lives in <jobs_dir>/ledger.jsonl (gitignored).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

INFRA_PATTERNS = (
    "docker compose", "docker-compose", "environment build", "environment make-",
    "return code: 3221", "connectionerror", "no such container",
)
AUTH_PATTERNS = ("401", "authentication", "invalid api key", "unauthorized")
RATE_LIMIT_PATTERNS = ("429", "rate limit", "rate-limit", "too many requests")


# ---------------------------------------------------------------- config / env

def load_config() -> dict:
    with open(HERE / "config.toml", "rb") as f:
        return tomllib.load(f)


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    path = path or REPO_ROOT / ".env"
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def agent_env(agent_id: str, dotenv: dict[str, str]) -> dict[str, str]:
    """Subprocess env for one agent, built from the .env OpenRouter pair.

    mini-swe-agent's connection spec (api_key_envs=("MSWEA_API_KEY",)) resolves
    MSWEA_API_KEY BEFORE the provider's OPENAI_API_KEY, and passthrough only
    forwards the provider's own env names into the container. Setting
    MSWEA_API_KEY therefore shadows the key and strips OPENAI_API_KEY from the
    container -> litellm "Missing credentials" retry loop. Never set it.
    """
    env = {**os.environ, **dotenv}
    key = dotenv.get("OPENAI_API_KEY", "")
    if agent_id == "miniswe":
        env.pop("MSWEA_API_KEY", None)  # OPENAI_BASE_URL already points at OpenRouter
    elif agent_id == "terminus2":
        env["OPENROUTER_API_KEY"] = key
    return env


def require_key(dotenv: dict[str, str]) -> None:
    if not dotenv.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY missing from .env - set the OpenRouter key first.")


# ---------------------------------------------------------------- ledger

def ledger_path(cfg: dict) -> Path:
    return REPO_ROOT / cfg["jobs_dir"] / "ledger.jsonl"


def append_ledger(cfg: dict, record: dict) -> None:
    path = ledger_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_ledger(cfg: dict) -> list[dict]:
    path = ledger_path(cfg)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------- job inspection

def classify_trial(trial_result: dict) -> tuple[str, str]:
    """(outcome, detail) from one trial's result.json."""
    exc = trial_result.get("exception_info")
    reward = trial_result.get("reward")
    if exc:
        msg = (exc.get("exception_message") or "").lower()
        if any(p in msg for p in INFRA_PATTERNS):
            return "INFRA", exc.get("exception_type", "")
        if any(p in msg for p in AUTH_PATTERNS):
            return "AUTH_ERROR", exc.get("exception_type", "")
        if any(p in msg for p in RATE_LIMIT_PATTERNS):
            return "RATE_LIMIT", exc.get("exception_type", "")
        return "ERROR", exc.get("exception_type", "")
    if reward == 1.0 or reward == 1:
        return "PASS", ""
    if reward == 0.0 or reward == 0:
        return "FAIL", ""
    return "UNVERIFIED", ""


def inspect_job(job_dir: Path) -> dict:
    """Aggregate a harbor job dir into ledger-friendly fields."""
    try:
        job_result = json.loads((job_dir / "result.json").read_text(
            encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        job_result = {}
    trials = []
    for trial_dir in sorted(job_dir.iterdir()):
        tr = trial_dir / "result.json"
        if not tr.exists():
            continue
        try:
            d = json.loads(tr.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            # Harbor on Windows can die writing the trial record (charmap
            # cannot encode \u2502 etc.) -> 0-byte result.json. Record it as a
            # write-off instead of crashing the driver.
            trials.append({
                "trial": trial_dir.name,
                "task": "",
                "outcome": "ERROR",
                "exception_type": "unparseable_result.json",
                "reward": None,
                "has_trajectory": (trial_dir / "agent" / "trajectory.json").exists(),
            })
            continue
        if d.get("reward") is None and not d.get("exception_info"):
            # harbor leaves trial reward null; the verifier's own record wins
            reward_txt = trial_dir / "verifier" / "reward.txt"
            if reward_txt.exists():
                try:
                    d["reward"] = float(reward_txt.read_text().strip())
                except ValueError:
                    pass
        outcome, exc_type = classify_trial(d)
        trials.append({
            "trial": trial_dir.name,
            "task": (d.get("task_name") or ""),
            "outcome": outcome,
            "exception_type": exc_type,
            "reward": d.get("reward"),
            "has_trajectory": (trial_dir / "agent" / "trajectory.json").exists(),
        })
    stats = job_result.get("stats") or {}
    started = job_result.get("started_at")
    finished = job_result.get("finished_at")
    duration = None
    if started and finished:
        duration = round((
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds())
    return {
        "n_trials": job_result.get("n_total_trials", len(trials)),
        "trials": trials,
        "duration_s": duration,
        "n_input_tokens": stats.get("n_input_tokens"),
        "n_output_tokens": stats.get("n_output_tokens"),
        "cost_usd": stats.get("cost_usd"),
    }


def count_rate_limits(log_file: Path) -> int:
    if not log_file.exists():
        return 0
    return len(re.findall(r"\b429\b|rate.?limit", log_file.read_text(errors="replace"), re.I))


# ------------------------------------------------------------------- running

def run_harbor(cfg: dict, args: list[str], env: dict[str, str], log_file: Path) -> int:
    cmd = ["harbor", "run", *args, "-o", cfg["jobs_dir"]]
    print(f"$ {' '.join(cmd)}")
    # Windows default (charmap) breaks harbor's file writes on agents that emit
    # box-drawing chars (\u2502). Force UTF-8 in the harbor subprocess.
    env = {**env, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n\n")
        lf.flush()
        proc = subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT)
    return proc.returncode


def record_job(cfg: dict, *, phase: str, job_name: str, agent: dict | None,
               attempt: int, source: str) -> dict:
    job_dir = REPO_ROOT / cfg["jobs_dir"] / job_name
    log_file = job_dir.parent / f"{job_name}.log"
    info = inspect_job(job_dir)
    # A harbor run that produced no readable trial record is infra, not a
    # successful-but-empty run; this lets cmd_pilot's INFRA retry handle it.
    outcomes = [t["outcome"] for t in info["trials"]] or ["INFRA"]
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": phase,
        "job_name": job_name,
        "agent_id": (agent or {}).get("id", "oracle"),
        "model": (agent or {}).get("model"),
        "attempt": attempt,
        "outcomes": outcomes,
        "tasks": sorted({t["task"] for t in info["trials"] if t["task"]}),
        "n_trajectories": sum(1 for t in info["trials"] if t["has_trajectory"]),
        "duration_s": info["duration_s"],
        "n_input_tokens": info["n_input_tokens"],
        "n_output_tokens": info["n_output_tokens"],
        "cost_usd": info["cost_usd"],
        "rate_limit_errors_in_log": count_rate_limits(log_file),
        "source": source,
    }
    append_ledger(cfg, record)
    return record


def worst_outcome(record: dict) -> str:
    for o in ("AUTH_ERROR", "RATE_LIMIT", "ERROR", "INFRA", "UNVERIFIED", "FAIL"):
        if o in record["outcomes"]:
            return o
    return "PASS" if record["outcomes"] else "EMPTY"


def cmd_oracle(cfg: dict, dry_run: bool) -> None:
    existing = [r for r in read_ledger(cfg)
                if r["agent_id"] == "oracle" and "PASS" in r["outcomes"]]
    if existing:
        print(f"Oracle already green (job {existing[-1]['job_name']}) - skipping.")
        return
    task = cfg["pilot_task"]
    job_name = "oracle-smoke"
    args = ["-t", task, "-a", "oracle", "--job-name", job_name]
    if dry_run:
        print(f"$ harbor run {' '.join(args)} -o {cfg['jobs_dir']}")
        return
    log = REPO_ROOT / cfg["jobs_dir"] / f"{job_name}.log"
    t0 = time.time()
    rc = run_harbor(cfg, args, {**os.environ}, log)
    rec = record_job(cfg, phase="oracle", job_name=job_name, agent=None,
                     attempt=1, source="run")
    print(f"oracle: rc={rc} outcomes={rec['outcomes']} "
          f"duration={time.time() - t0:.0f}s")
    if "PASS" not in rec["outcomes"]:
        sys.exit("Gate A blocked: oracle did not pass. Fix environment/verifier first.")


def next_job_name(cfg: dict, base: str) -> str:
    """First non-existent of base, base-r1, base-r2, ... (harbor refuses to
    reuse a job dir)."""
    jobs = REPO_ROOT / cfg["jobs_dir"]
    if not (jobs / base).exists():
        return base
    n = 1
    while (jobs / f"{base}-r{n}").exists():
        n += 1
    return f"{base}-r{n}"


def cmd_pilot(cfg: dict, only_agent: str | None, dry_run: bool) -> None:
    require_key(load_dotenv())
    dotenv = load_dotenv()
    if dry_run:
        for agent in cfg["agents"]:
            if only_agent and agent["id"] != only_agent:
                continue
            args = ["-t", cfg["pilot_task"], "-a", agent["harbor_agent"],
                    "-m", agent["model"], "--job-name", f"pilot-{agent['id']}"]
            keys = sorted(k for k in agent_env(agent["id"], dotenv)
                          if k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_BASE_URL"))
            print(f"$ env[{', '.join(keys)}] harbor run {' '.join(args)} -o {cfg['jobs_dir']}")
        return
    # single-runner lock: two concurrent drivers corrupt job dirs and ledger
    lock = REPO_ROOT / cfg["jobs_dir"] / "pilot.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        sys.exit(f"Another pilot is running (lock: {lock}). "
                 "If it crashed, delete the lock file.")
    try:
        for agent in cfg["agents"]:
            if only_agent and agent["id"] != only_agent:
                continue
            for infra_retry in range(cfg["max_infra_retries"] + 1):
                job_name = next_job_name(cfg, f"pilot-{agent['id']}")
                args = ["-t", cfg["pilot_task"], "-a", agent["harbor_agent"],
                        "-m", agent["model"], "--job-name", job_name]
                log = REPO_ROOT / cfg["jobs_dir"] / f"{job_name}.log"
                run_harbor(cfg, args, agent_env(agent["id"], dotenv), log)
                rec = record_job(cfg, phase="pilot", job_name=job_name,
                                 agent=agent, attempt=1, source="run")
                outcome = worst_outcome(rec)
                print(f"{agent['id']}: job={job_name} outcomes={rec['outcomes']} "
                      f"trajectories={rec['n_trajectories']} "
                      f"duration={rec['duration_s']}s "
                      f"429s={rec['rate_limit_errors_in_log']}", flush=True)
                if outcome == "INFRA" and infra_retry < cfg["max_infra_retries"]:
                    print(f"  INFRA failure - retry "
                          f"{infra_retry + 1}/{cfg['max_infra_retries']}", flush=True)
                    continue
                if outcome == "AUTH_ERROR":
                    sys.exit("Auth failed - check OPENAI_API_KEY in .env.")
                break
    finally:
        lock.unlink(missing_ok=True)


def cmd_sweep(cfg: dict, dry_run: bool) -> None:
    tasks = cfg.get("tasks") or []
    if len(tasks) < 6:
        sys.exit(f"Sweep needs >=6 piloted tasks in config.toml (have {len(tasks)}). "
                 "Finish Phase 2 first.")
    require_key(load_dotenv())
    n = len(tasks) * len(cfg["agents"]) * cfg["attempts"]
    print(f"Sweep: {len(tasks)} tasks x {len(cfg['agents'])} agents "
          f"x {cfg['attempts']} attempts = {n} runs")
    if dry_run:
        for t in tasks:
            for a in cfg["agents"]:
                print(f"  {t} x {a['id']} x{cfg['attempts']}")
        return
    # Phase 3 implementation lands after Gate A; structure mirrors cmd_pilot.
    sys.exit("Sweep execution not enabled until Gate A passes and Phase 2 tasks are set.")


def cmd_ingest(cfg: dict) -> None:
    for rec in read_ledger(cfg):
        if rec["agent_id"] == "oracle" or rec["n_trajectories"] == 0:
            continue
        job_dir = REPO_ROOT / cfg["jobs_dir"] / rec["job_name"]
        if not job_dir.exists():
            continue
        print(f"ingesting {rec['job_name']} ...")
        proc = subprocess.run(
            [sys.executable, "-m", "agr", "ingest-harbor", str(job_dir)],
            capture_output=True, text=True, cwd=REPO_ROOT)
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        print("  " + (tail[-1] if tail else "(no output)"))


def cmd_import(cfg: dict) -> None:
    known = {r["job_name"] for r in read_ledger(cfg)}
    jobs_dir = REPO_ROOT / cfg["jobs_dir"]
    for result in sorted(jobs_dir.glob("*/result.json")):
        job_name = result.parent.name
        if job_name in known:
            continue
        agent_id = "oracle" if "oracle" in job_name else (
            "miniswe" if "miniswe" in job_name else (
                "terminus2" if "terminus" in job_name else "unknown"))
        phase = "oracle" if agent_id == "oracle" else "pilot"
        rec = record_job(cfg, phase=phase, job_name=job_name,
                         agent={"id": agent_id}, attempt=1, source="import")
        print(f"imported {job_name}: {rec['outcomes']}")


def cmd_status(cfg: dict) -> None:
    records = read_ledger(cfg)
    if not records:
        print("Ledger empty. Run: import, oracle, or pilot.")
        return
    print(f"{'job':<28} {'agent':<10} {'outcomes':<18} {'traj':>4} "
          f"{'dur(s)':>7} {'429s':>4}")
    print("-" * 78)
    totals: dict[str, int] = {}
    for r in records:
        oc = ",".join(r["outcomes"]) or "-"
        print(f"{r['job_name']:<28} {r['agent_id']:<10} {oc:<18} "
              f"{r['n_trajectories']:>4} {str(r['duration_s']):>7} "
              f"{r['rate_limit_errors_in_log']:>4}")
        if r["agent_id"] == "oracle":
            continue  # oracle validation runs are reported separately
        for o in r["outcomes"]:
            totals[o] = totals.get(o, 0) + 1
    print("-" * 78)
    evaluated = totals.get("PASS", 0) + totals.get("FAIL", 0)
    writeoffs = totals.get("INFRA", 0) + totals.get("RATE_LIMIT", 0) + totals.get("ERROR", 0)
    print(f"evaluated trials (count toward 36): {evaluated}   "
          f"infra/write-off trials: {writeoffs}   "
          f"totals: {totals}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="print commands without executing")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("oracle")
    pp = sub.add_parser("pilot")
    pp.add_argument("--agent", choices=["miniswe", "terminus2"])
    sub.add_parser("sweep")
    sub.add_parser("ingest")
    sub.add_parser("import")
    sub.add_parser("status")
    args = p.parse_args()

    cfg = load_config()
    {"oracle": lambda: cmd_oracle(cfg, args.dry_run),
     "pilot": lambda: cmd_pilot(cfg, args.agent, args.dry_run),
     "sweep": lambda: cmd_sweep(cfg, args.dry_run),
     "ingest": lambda: cmd_ingest(cfg),
     "import": lambda: cmd_import(cfg),
     "status": lambda: cmd_status(cfg)}[args.cmd]()


if __name__ == "__main__":
    main()
