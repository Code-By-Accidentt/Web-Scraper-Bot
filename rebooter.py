"""Supervisor that keeps ``main.py`` alive.

It starts the bot if no instance is running, and restarts it when the process
dies or when the heartbeat file written by the bot goes stale (a hung process).

    python rebooter.py            # defaults: main.py next to this file, data/heartbeat
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import psutil


class Supervisor:
    def __init__(
        self,
        script: Path,
        *,
        python: str,
        heartbeat: Path,
        stale_after: float,
        check_interval: float,
    ) -> None:
        self._script = script.resolve()
        self._python = python
        self._heartbeat = heartbeat
        self._stale_after = stale_after
        self._check_interval = check_interval
        self._process: subprocess.Popen[bytes] | psutil.Process | None = None
        self._started_at = 0.0

    def run(self) -> None:
        self._log(f"supervising {self._script}")
        while True:
            if not self._alive():
                self._adopt_or_start()
            elif self._heartbeat_stale():
                self._log(f"no heartbeat for {self._stale_after:.0f}s; restarting")
                self._terminate()
                self._start()
            time.sleep(self._check_interval)

    def _alive(self) -> bool:
        proc = self._process
        if proc is None:
            return False
        if isinstance(proc, subprocess.Popen):
            return proc.poll() is None
        try:
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def _adopt_or_start(self) -> None:
        existing = self._find_running()
        if existing is not None:
            self._log(f"adopting running bot (pid {existing.pid})")
            self._process = existing
            self._started_at = time.time()
            return
        self._start()

    def _start(self) -> None:
        self._log("starting the bot")
        self._process = subprocess.Popen([self._python, str(self._script)], cwd=self._script.parent)
        self._started_at = time.time()

    def _terminate(self) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(10)
        except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
            proc.kill()
        except psutil.Error:
            pass

    def _heartbeat_stale(self) -> bool:
        if time.time() - self._started_at < self._stale_after:
            return False  # grace period while the bot boots
        try:
            age = time.time() - self._heartbeat.stat().st_mtime
        except OSError:
            return True
        return age > self._stale_after

    def _find_running(self) -> psutil.Process | None:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.pid == psutil.Process().pid:
                continue
            try:
                cmdline = proc.info["cmdline"] or []
                if any(self._is_our_script(arg) for arg in cmdline):
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None

    def _is_our_script(self, argument: str) -> bool:
        try:
            return Path(argument).resolve() == self._script
        except (OSError, ValueError):
            return False

    @staticmethod
    def _log(message: str) -> None:
        print(f"[rebooter {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", type=Path, default=here / "main.py")
    parser.add_argument("--heartbeat", type=Path, default=here / "data" / "heartbeat")
    parser.add_argument("--stale-after", type=float, default=300.0, help="seconds")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between checks")
    args = parser.parse_args()
    Supervisor(
        args.script,
        python=sys.executable,
        heartbeat=args.heartbeat,
        stale_after=args.stale_after,
        check_interval=args.interval,
    ).run()


if __name__ == "__main__":
    main()
