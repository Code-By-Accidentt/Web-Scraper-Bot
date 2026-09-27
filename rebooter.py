"""Supervisor that keeps ``main.py`` alive.

It starts the bot unless one is already running, and restarts it when the process dies
or its heartbeat file goes stale (a hung bot). A bot that keeps failing at start waits
longer before each restart, so a broken setup does not send a 🛑 alert every few seconds.
A SIGTERM from systemd is passed on, and the bot gets time to send its last alerts.

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

STOP_GRACE = 30.0  # seconds a stopping bot gets to send its last alerts and close the browser
QUICK_EXIT = 60.0  # seconds; a bot that exits sooner after starting is failing at start
# Pauses in seconds before restarting a bot that keeps failing at start; the last repeats.
RESTART_PAUSES = (5.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0)
# Exit codes a restart cannot cure (see scraper_bot/app/cli.py).
NEEDS_A_HUMAN = {2: "configuration error or rejected token", 3: "the browser could not start"}
# Interpreter options that take a separate value (python -X utf8 main.py).
_OPTIONS_WITH_VALUE = frozenset({"-X", "-W", "--check-hash-based-pycs"})
# One-letter switches that may be combined in front of -m (python -um scraper_bot).
_SWITCHES = frozenset("bBdEhiIOPqsSuvx")


class Supervisor:
    """Watches one bot process and restarts it when it dies or stops writing its heartbeat."""

    def __init__(
        self,
        script: Path,
        *,
        python: str,
        heartbeat: Path,
        stale_after: float,
        check_interval: float,
    ) -> None:
        """Supervise ``script``, run with the ``python`` interpreter.

        The bot counts as hung when ``heartbeat`` is older than ``stale_after`` seconds; the
        process is checked every ``check_interval`` seconds.
        """
        self._script = script.resolve()
        self._python = python
        self._heartbeat = heartbeat
        self._stale_after = stale_after
        self._check_interval = check_interval
        self._process: subprocess.Popen[bytes] | psutil.Process | None = None
        self._started_at = 0.0
        self._quick_exits = 0

    def run(self) -> None:
        """Watch the bot forever, adopting, starting or restarting it as needed."""
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
        """Return the seconds to wait before restarting a bot that ran ``lived`` seconds.

        After a run of a minute or more the restart is immediate. Each quick exit in a row
        waits longer, and an exit ``code`` a restart cannot cure starts from a minute.
        """
        if lived >= QUICK_EXIT:
            self._quick_exits = 0
            return 0.0
        self._quick_exits += 1
        step = self._quick_exits - 1
        if code in NEEDS_A_HUMAN:
            step += 2
        return RESTART_PAUSES[min(step, len(RESTART_PAUSES) - 1)]

    def _pause_before_restart(self) -> None:
        """Sleep before a restart when the last bot exited quickly, and log why."""
        process = self._process
        if process is None:
            return
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
        """Return True while the bot process runs; a zombie counts as dead."""
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
        """Take over a bot of this checkout that is already running, or start a new one."""
        existing = self._find_running()
        if existing is not None:
            self._log(f"adopting running bot (pid {existing.pid})")
            self._process = existing
            self._started_at = time.time()
            return
        self._start()

    def _start(self) -> None:
        """Start the bot in the script's directory."""
        self._log("starting the bot")
        self._process = subprocess.Popen([self._python, str(self._script)], cwd=self._script.parent)
        self._started_at = time.time()

    def stop(self, _signum: int = 0, _frame: object = None) -> None:
        """Stop the bot gracefully, then exit; installed as the SIGTERM handler."""
        self._log("stopping the bot")
        self._terminate(grace=STOP_GRACE)
        raise SystemExit(0)

    def _terminate(self, grace: float = 10) -> None:
        """Ask the bot to stop, and kill it if it still runs after ``grace`` seconds."""
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
        """Return True when the heartbeat file is missing or older than ``stale_after``.

        A bot started less than ``stale_after`` seconds ago is never stale.
        """
        if time.time() - self._started_at < self._stale_after:
            return False
        try:
            age = time.time() - self._heartbeat.stat().st_mtime
        except OSError:
            return True
        return age > self._stale_after

    def _find_running(self) -> psutil.Process | None:
        """Return a bot process of this checkout that is already running, or None."""
        me = psutil.Process()
        skip = {me.pid, *(parent.pid for parent in me.parents())}  # ourselves and our launchers
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
        """Return True when ``cmdline`` runs the bot of this checkout.

        That is ``python [options] main.py`` or ``python -m scraper_bot``. An editor with
        main.py open, or another supervisor given ``--script main.py``, is not the bot.
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
                return False
            if argument in _OPTIONS_WITH_VALUE:
                next(args, None)
                continue
            if argument.startswith("-"):
                continue
            return self._resolve(argument, cwd) == self._script
        return False

    @staticmethod
    def _resolve(argument: str, cwd: str | None) -> Path | None:
        """Return ``argument`` as an absolute path, relative to ``cwd``; None if that fails."""
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
        """Print ``message`` with the time, flushed at once so the journal gets it."""
        print(f"[rebooter {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> None:
    """Read the command line and supervise the bot until a SIGTERM."""
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
