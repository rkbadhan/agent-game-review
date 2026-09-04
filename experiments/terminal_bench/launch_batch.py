"""Launch a batch of miniswe harbor runs in parallel (detached, staggered).

Mirrors run_experiment.agent_env("miniswe") + run_harbor env, so the routing
matches the working pilot-miniswe (stealth/ox-alpha via Fireworks openai-compat).

Usage:
    python launch_batch.py [--agent miniswe|terminus2] [--temp T] [--attempt N] <job_prefix> <task1> [<task2> ...]
Default --agent = miniswe, --temp = None (agent default), --attempt = 1.
Per-attempt temperature variance is baked via the right agent kwarg:
  miniswe   : --agent-kwarg 'config={"model":{"temperature":T}}'
  terminus2 : --agent-kwarg temperature=T
Job name = <prefix>-<task>-a<attempt>. Log -> eval-runs/<job>.log.
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
JOBS = REPO_ROOT / "eval-runs"

def load_dotenv() -> dict[str, str]:
    p = REPO_ROOT / ".env"
    env = {}
    for line in p.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

AGENTS = {
    "miniswe":   {"harbor": "mini-swe-agent", "model": "openai/stealth/ox-alpha"},
    "terminus2": {"harbor": "terminus-2",      "model": "openrouter/stealth/ox-alpha"},
}

def main() -> None:
    args = sys.argv[1:]
    agent_id = "miniswe"
    temp: float | None = None
    attempt = 1
    # parse flags (any order)
    if "--agent" in args:
        i = args.index("--agent"); agent_id = args[i + 1]; args = args[:i] + args[i + 2:]
    if "--temp" in args:
        i = args.index("--temp"); temp = float(args[i + 1]); args = args[:i] + args[i + 2:]
    if "--attempt" in args:
        i = args.index("--attempt"); attempt = int(args[i + 1]); args = args[:i] + args[i + 2:]
    if agent_id not in AGENTS:
        sys.exit("unknown agent %r (use miniswe|terminus2)" % agent_id)
    if len(args) < 2:
        sys.exit("usage: launch_batch.py [--agent ...] [--temp T] [--attempt N] <job_prefix> <task1> [...]")
    prefix, tasks = args[0], args[1:]
    dotenv = load_dotenv()
    if not dotenv.get("OPENAI_API_KEY") or not dotenv.get("OPENAI_BASE_URL"):
        sys.exit("OPENAI_API_KEY / OPENAI_BASE_URL missing from .env")
    base_env = {**os.environ, **dotenv}
    if agent_id == "miniswe":
        base_env.pop("MSWEA_API_KEY", None)       # shadows the key in-container
    elif agent_id == "terminus2":
        base_env["OPENROUTER_API_KEY"] = dotenv["OPENAI_API_KEY"]  # openrouter/ provider
    base_env["PYTHONUTF8"] = "1"                  # Windows charmap fix
    base_env["PYTHONIOENCODING"] = "utf-8"
    agent = AGENTS[agent_id]
    # per-attempt temperature kwarg (agent-specific)
    temp_kwargs: list[str] = []
    if temp is not None:
        if agent_id == "miniswe":
            temp_kwargs = ["--agent-kwarg", 'config={"model":{"temperature":%s}}' % temp]
        else:  # terminus2
            temp_kwargs = ["--agent-kwarg", "temperature=%s" % temp]
    JOBS.mkdir(parents=True, exist_ok=True)
    is_win = os.name == "nt"
    cflags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if is_win else 0
    manifest = []
    for i, task in enumerate(tasks):
        job = f"{prefix}-{task}-a{attempt}"
        log = JOBS / f"{job}.log"
        cmd = ["harbor", "run", "-t", f"terminal-bench/{task}",
               "-a", agent["harbor"], "-m", agent["model"],
               *temp_kwargs, "--job-name", job, "-o", "eval-runs"]
        log.write_text(f"$ {' '.join(cmd)}\n\n", encoding="utf-8")
        lf = log.open("a", encoding="utf-8")
        proc = subprocess.Popen(cmd, env=base_env, cwd=str(REPO_ROOT),
                                stdout=lf, stderr=subprocess.STDOUT,
                                creationflags=cflags,
                                stdin=subprocess.DEVNULL)
        manifest.append({"task": task, "agent": agent_id, "attempt": attempt,
                         "temp": temp, "job_name": job, "pid": proc.pid,
                         "log": str(log)})
        print(f"launched {job} (task={task}) pid={proc.pid} -> {log.name}", flush=True)
        if i < len(tasks) - 1:
            time.sleep(15)   # stagger to dodge docker-compose DLL-init race
    (JOBS / f"{prefix}-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest -> {JOBS/(prefix+'-manifest.json')}")
    print("runs continue in background. Poll with: docker ps ; tail logs")

if __name__ == "__main__":
    main()
