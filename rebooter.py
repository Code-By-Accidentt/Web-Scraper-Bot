"""Supervisor that keeps ``main.py`` alive.

It starts the bot if no instance is running, and restarts it when the process
dies or when the heartbeat file written by the bot goes stale (a hung process).
A bot that dies soon after starting is restarted after a growing pause, so a broken
browser or configuration does not become a restart (and 🛑 alert) every few seconds.
A SIGTERM to the supervisor (systemd) is passed on to the bot, which gets time to send
the alerts already on their way.

    python rebooter.py            # defaults: main.py next to this file, data/heartbeat
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

# Seconds a stopping bot gets to send the alerts on their way and close the browser.
STOP_GRACE = 30.0
# A bot that exits sooner than this after starting is failing at start.
QUICK_EXIT = 60.0
# Pauses before restarting a bot that keeps failing at start (seconds); the last repeats.
RESTART_PAUSES = (5.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0)
# Exit codes a restart cannot cure: 2 configuration or rejected token, 3 no browser.
NEEDS_A_HUMAN = {2: "configuration error or rejected token", 3: "the browser could not start"}
# Interpreter options followed by a value of their own (python -X utf8 main.py), and the
# one-letter switches that may be combined in front of -m (python -um scraper_bot).
_OPTIONS_WITH_VALUE = frozenset({"-X", "-W", "--check-hash-based-pycs"})
_SWITCHES = frozenset("bBdEhiIOPqsSuvx")


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
        self._quick_exits = 0

    def run(self) -> None:
        self._log(f"supervising {self._script}")
        while True:
            if not self._alive():
                self._pause_before_restart()
                self._adopt_or_start()
            elif self._heartbeat_stale():
                self._log(f"no heartbeat for {self._stale_after:.0f}s; restarting")
                self._terminate()
                self._start()
            time.sleep(self._check_interval)

    def restart_pause(self, lived: float, code: int | None) -> float:
        """Seconds to wait before starting a bot that lived ``lived`` s and exited with ``code``."""
        if lived >= QUICK_EXIT:
            self._quick_exits = 0
            return 0.0
        self._quick_exits += 1
        step = self._quick_exits - 1
        if code in NEEDS_A_HUMAN:
            step += 2  # a restart will not fix it: start from a minute
        return RESTART_PAUSES[min(step, len(RESTART_PAUSES) - 1)]

    def _pause_before_restart(self) -> None:
        process = self._process
        if process is None:
            return  # nothing started yet
        lived = time.time() - self._started_at
        code = process.poll() if isinstance(process, subprocess.Popen) else None
        pause = self.restart_pause(lived, code)
        if pause <= 0:
            return
        reason = f" ({NEEDS_A_HUMAN[code]})" if code in NEEDS_A_HUMAN else ""
        self._log(
            f"the bot exited with code {code} after {lived:.0f}s{reason}; "
            f"restarting in {pause:.0f}s"
        )
        time.sleep(pause)

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

    def stop(self, _signum: int = 0, _frame: object = None) -> None:
        """SIGTERM handler: stop the bot gracefully, then exit."""
        self._log("stopping the bot")
        self._terminate(grace=STOP_GRACE)
        raise SystemExit(0)

    def _terminate(self, grace: float = 10) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(grace)
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
        me = psutil.Process()
        skip = {me.pid, *(parent.pid for parent in me.parents())}  # our own launchers
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            if proc.pid in skip:
                continue
            try:
                if self.is_our_bot(proc.info["cmdline"] or [], proc.info["cwd"]):
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None

    def is_our_bot(self, cmdline: list[str], cwd: str | None) -> bool:
        """``python [options] main.py`` or ``python -m scraper_bot`` of this checkout.

        An editor with main.py open, or another supervisor given ``--script main.py``,
        is not the bot.
        """
        if not cmdline or not Path(cmdline[0]).name.lower().startswith("python"):
            return False
        args = iter(cmdline[1:])
        for argument in args:
            switches, flag, attached = argument[1:].partition("m")
            short = argument.startswith("-") and not argument.startswith("--")
            if short and flag and set(switches) <= _SWITCHES:  # -m, -um, -mscraper_bot
                module = attached or next(args, "")
                wanted = module == "scraper_bot"
                return wanted and self._resolve(".", cwd) == self._script.parent
            if argument == "-c":
                return False  # a command string, not a script
            if argument in _OPTIONS_WITH_VALUE:
                next(args, None)  # -X utf8: the value is not the script
                continue
            if argument.startswith("-"):
                continue  # interpreter options such as -u or -Xutf8
            return self._resolve(argument, cwd) == self._script
        return False

    @staticmethod
    def _resolve(argument: str, cwd: str | None) -> Path | None:
        try:
            path = Path(argument)
            if not path.is_absolute():
                if cwd is None:
                    return None
                path = Path(cwd) / path
            return path.resolve()
        except (OSError, ValueError):
            return None

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
    supervisor = Supervisor(
        args.script,
        python=sys.executable,
        heartbeat=args.heartbeat,
        stale_after=args.stale_after,
        check_interval=args.interval,
    )
    signal.signal(signal.SIGTERM, supervisor.stop)
    supervisor.run()


if __name__ == "__main__":
    main()
