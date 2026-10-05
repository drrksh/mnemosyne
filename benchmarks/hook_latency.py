"""
Hook latency: how long Claude Code waits on Mnemosyne before each prompt.

Runs the real `mnemosyne hook` command as Claude Code does (a new process
per event, JSON on stdin) against a home seeded with the 100-memory
evaluation persona, first cold (no daemon: every hook imports and opens
everything) and then with the warm hook daemon.

    python benchmarks/hook_latency.py [--runs 20]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mnemosyne.config import Settings
from mnemosyne.container import Mnemosyne
from mnemosyne.evaluation.harness import seed
from mnemosyne.integrations import hook_daemon

EVENTS = {
    "session-start": {"source": "startup"},
    "user-prompt": {"prompt": "add an idempotent refund endpoint and write tests for it"},
}
HOOK = [sys.executable, "-m", "mnemosyne.cli", "hook"]


def run_hook(name: str, env: dict[str, str], cwd: str) -> tuple[float, str]:
    payload = json.dumps({**EVENTS[name], "cwd": cwd, "session_id": "bench"})
    started = time.perf_counter()
    result = subprocess.run([*HOOK, name], input=payload, capture_output=True, text=True, env=env, check=True)
    return (time.perf_counter() - started) * 1000, result.stdout


def measure(name: str, runs: int, env: dict[str, str], cwd: str) -> tuple[list[float], str]:
    timings, output = [], ""
    for _ in range(runs):
        elapsed, output = run_hook(name, env, cwd)
        timings.append(elapsed)
    return timings, output


def _context(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"] if stdout.strip() else ""


def summary(timings: list[float]) -> str:
    ordered = sorted(timings)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return f"p50 {statistics.median(ordered):6.0f} ms   p95 {p95:6.0f} ms"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="mn-") as tmp:
        home = Path(tmp) / "home"
        settings = Settings(home=home, log_level="WARNING")
        settings.ensure_home()
        with Mnemosyne(settings) as container:
            count = len(seed(container))
        env = {**os.environ, "MNEMOSYNE_HOME": str(home), "MNEMOSYNE_HOOK_DAEMON": "0"}
        print(f"{count} memories; {args.runs} runs per hook\n")

        cold: dict[str, tuple[list[float], str]] = {name: measure(name, args.runs, env, tmp) for name in EVENTS}
        for name, (timings, _) in cold.items():
            print(f"cold  {name:<14} {summary(timings)}")

        if not hook_daemon.SUPPORTED:
            print("\nhook daemon: not supported on this platform")
            return 0
        daemon_env = {**env, "MNEMOSYNE_HOOK_DAEMON": "1"}
        daemon = subprocess.Popen([*HOOK[:-1], "daemon", "run"], env=daemon_env, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while hook_daemon.daemon_status(home) is None:
                if time.monotonic() > deadline or daemon.poll() is not None:
                    print("daemon did not start", file=sys.stderr)
                    return 1
                time.sleep(0.1)
            print()
            for name in EVENTS:
                timings, output = measure(name, args.runs, daemon_env, tmp)
                same = "identical output" if output == cold[name][1] else "OUTPUT DIFFERS"
                print(f"warm  {name:<14} {summary(timings)}   ({same})")
                if output != cold[name][1]:
                    import difflib

                    before, after = (_context(text).splitlines() for text in (cold[name][1], output))
                    sys.stdout.writelines(f"    {line}\n" for line in difflib.unified_diff(before, after, "cold",
                                                                                         "warm", lineterm=""))
                    return 1
        finally:
            daemon.terminate()
            daemon.wait(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
