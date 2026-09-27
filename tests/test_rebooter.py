"""Tests for the supervisor in ``rebooter.py``; no bot is ever started."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

import rebooter
from rebooter import Supervisor


def make(tmp_path: Path, *, stale_after: float = 60.0) -> Supervisor:
    return Supervisor(
        tmp_path / "main.py",
        python=sys.executable,
        heartbeat=tmp_path / "data" / "heartbeat",
        stale_after=stale_after,
        check_interval=0.0,
    )


# ── heartbeat ───────────────────────────────────────────────────────────────
def test_heartbeat_is_not_stale_during_the_grace_period(tmp_path: Path) -> None:
    supervisor = make(tmp_path, stale_after=60.0)
    supervisor._started_at = time.time()
    assert not supervisor._heartbeat.exists()
    assert supervisor._heartbeat_stale() is False


def test_missing_heartbeat_is_stale_after_the_grace_period(tmp_path: Path) -> None:
    supervisor = make(tmp_path, stale_after=60.0)
    supervisor._started_at = time.time() - 1_000
    assert supervisor._heartbeat_stale() is True


def test_fresh_heartbeat_is_not_stale(tmp_path: Path) -> None:
    supervisor = make(tmp_path, stale_after=60.0)
    supervisor._started_at = time.time() - 1_000
    supervisor._heartbeat.parent.mkdir(parents=True)
    supervisor._heartbeat.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    assert supervisor._heartbeat_stale() is False


def test_aged_heartbeat_is_stale(tmp_path: Path) -> None:
    supervisor = make(tmp_path, stale_after=60.0)
    supervisor._started_at = time.time() - 1_000
    supervisor._heartbeat.parent.mkdir(parents=True)
    supervisor._heartbeat.write_text("0\n", encoding="utf-8")
    old = time.time() - 1_000
    os.utime(supervisor._heartbeat, (old, old))
    assert supervisor._heartbeat_stale() is True


# ── _alive ──────────────────────────────────────────────────────────────────
def test_alive_without_a_process(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    assert supervisor._process is None
    assert supervisor._alive() is False


def test_alive_with_a_finished_popen(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=60)
    supervisor._process = process
    assert supervisor._alive() is False


def test_alive_with_a_running_popen(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        supervisor._process = process
        assert supervisor._alive() is True
    finally:
        process.kill()
        process.wait(timeout=60)
    assert supervisor._alive() is False


class PsutilLike:
    """Duck-typed ``psutil.Process`` for the adopted-process branch of ``_alive``."""

    def __init__(self, *, running: bool = True, status: str = "running", error: Any = None):
        self.pid = 4242
        self._running = running
        self._status = status
        self._error = error

    def is_running(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._running

    def status(self) -> str:
        return self._status


@pytest.mark.parametrize(
    ("process", "expected"),
    [
        (PsutilLike(), True),
        (PsutilLike(running=False), False),
        (PsutilLike(status=psutil.STATUS_ZOMBIE), False),
        (PsutilLike(error=psutil.NoSuchProcess(4242)), False),
        (PsutilLike(error=psutil.AccessDenied(pid=4242)), False),
    ],
)
def test_alive_with_an_adopted_process(tmp_path: Path, process: PsutilLike, expected: bool) -> None:
    supervisor = make(tmp_path)
    supervisor._process = process  # type: ignore[assignment]
    assert supervisor._alive() is expected


def test_alive_with_the_supervisor_itself_adopted(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    supervisor._process = psutil.Process()
    assert supervisor._alive() is True


# ── _terminate ──────────────────────────────────────────────────────────────
class RecordingProcess:
    def __init__(
        self, *, wait_error: Exception | None = None, terminate_error: Exception | None = None
    ):
        self.calls: list[str] = []
        self._wait_error = wait_error
        self._terminate_error = terminate_error

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self._terminate_error is not None:
            raise self._terminate_error

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait({timeout})")
        if self._wait_error is not None:
            raise self._wait_error
        return 0

    def kill(self) -> None:
        self.calls.append("kill")


def test_terminate_kills_a_process_that_ignores_terminate(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = RecordingProcess(wait_error=subprocess.TimeoutExpired(cmd="main.py", timeout=10))
    supervisor._process = process  # type: ignore[assignment]
    supervisor._terminate()
    assert process.calls == ["terminate", "wait(10)", "kill"]
    assert supervisor._process is None


def test_terminate_kills_an_adopted_process_that_ignores_terminate(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = RecordingProcess(wait_error=psutil.TimeoutExpired(10, pid=4242))
    supervisor._process = process  # type: ignore[assignment]
    supervisor._terminate()
    assert process.calls == ["terminate", "wait(10)", "kill"]


def test_terminate_does_not_kill_a_cooperative_process(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = RecordingProcess()
    supervisor._process = process  # type: ignore[assignment]
    supervisor._terminate()
    assert process.calls == ["terminate", "wait(10)"]
    assert supervisor._process is None


def test_stop_gives_the_bot_time_to_shut_down_then_exits(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = RecordingProcess()
    supervisor._process = process  # type: ignore[assignment]
    with pytest.raises(SystemExit) as info:
        supervisor.stop()
    assert info.value.code == 0
    assert process.calls == ["terminate", f"wait({rebooter.STOP_GRACE})"]


def test_terminate_tolerates_a_vanished_process(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    process = RecordingProcess(terminate_error=psutil.NoSuchProcess(4242))
    supervisor._process = process  # type: ignore[assignment]
    supervisor._terminate()  # must not raise
    assert process.calls == ["terminate"]
    assert supervisor._process is None


def test_terminate_without_a_process_is_a_noop(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    supervisor._terminate()
    assert supervisor._process is None


# ── _find_running / _is_our_script ──────────────────────────────────────────
class FakeProcess:
    def __init__(
        self,
        pid: int,
        cmdline: list[str] | None,
        *,
        denied: bool = False,
        cwd: str | None = None,
    ) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._denied = denied
        self._cwd = cwd

    @property
    def info(self) -> dict[str, Any]:
        if self._denied:
            raise psutil.AccessDenied(pid=self.pid)
        return {"pid": self.pid, "cmdline": self._cmdline, "cwd": self._cwd}


def patch_process_iter(monkeypatch: pytest.MonkeyPatch, processes: list[FakeProcess]) -> None:
    def process_iter(attrs: Any = None, ad_value: Any = None) -> Any:
        assert "cmdline" in (attrs or [])
        return iter(processes)

    monkeypatch.setattr(rebooter.psutil, "process_iter", process_iter)


def test_the_bot_is_recognised_by_its_command_line(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    script = str(tmp_path / "main.py")
    here, elsewhere = str(tmp_path), str(tmp_path / "elsewhere")
    bot = supervisor.is_our_bot
    assert bot([sys.executable, script], None)
    assert bot(["python3", "-u", str(tmp_path / "sub" / ".." / "main.py")], None)
    assert bot(["python", "main.py"], here)  # relative to the process's working directory
    assert not bot(["python", "main.py"], elsewhere)
    assert bot(["python3.12", "-m", "scraper_bot"], here)  # the documented other way
    assert not bot(["python3.12", "-m", "scraper_bot"], elsewhere)
    assert not bot(["python", "-m", "pytest"], here)
    assert not bot(["vim", script], here)  # an editor with main.py open
    assert not bot(["python", "rebooter.py", "--script", script], here)  # another supervisor
    assert not bot(["python", str(tmp_path / "other.py")], None)
    assert not bot([], here) and not bot(["python"], here)
    # Interpreter options with a value of their own come before the script.
    assert bot(["python", "-X", "utf8", "main.py"], here)
    assert bot(["python", "-Xutf8", "-W", "ignore", "main.py"], here)
    assert bot(["python", "-X", "utf8", "-m", "scraper_bot"], here)
    assert bot(["python", "-mscraper_bot"], here)
    assert bot(["python", "-um", "scraper_bot"], here)
    assert not bot(["python", "-Wmodule", "other.py"], here)
    assert not bot(["python", "-c", "import main"], here)


def test_a_bot_failing_at_start_is_restarted_after_growing_pauses(tmp_path: Path) -> None:
    supervisor = make(tmp_path)
    assert [supervisor.restart_pause(3, 1) for _ in range(3)] == [5.0, 30.0, 60.0]
    assert supervisor.restart_pause(3600, 1) == 0.0  # it ran for an hour: at once, and reset
    assert supervisor.restart_pause(3, 1) == 5.0
    # A missing browser or a broken .env will not heal itself: a minute, then longer.
    fresh = make(tmp_path)
    assert [fresh.restart_pause(2, 3) for _ in range(7)] == [
        60.0,
        120.0,
        300.0,
        600.0,
        900.0,
        900.0,
        900.0,
    ]


def test_find_running_adopts_the_bot_and_skips_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = make(tmp_path)
    script = str(tmp_path / "main.py")
    me = psutil.Process()
    parent = me.parents()[0].pid if me.parents() else 1
    bot = FakeProcess(5001, [sys.executable, str(tmp_path / "sub" / ".." / "main.py")])
    patch_process_iter(
        monkeypatch,
        [
            FakeProcess(me.pid, [sys.executable, script]),  # the supervisor itself
            FakeProcess(parent, [sys.executable, script]),  # its launcher (a venv stub)
            FakeProcess(5000, None),  # kernel thread: no cmdline
            FakeProcess(5002, ["python", "other.py"], denied=True),  # info raises
            FakeProcess(5003, ["python", str(tmp_path / "other.py")]),
            bot,
            FakeProcess(5004, [sys.executable, script]),  # a second copy: first match wins
        ],
    )
    assert supervisor._find_running() is bot


def test_find_running_returns_none_when_only_itself_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = make(tmp_path)
    patch_process_iter(
        monkeypatch,
        [
            FakeProcess(psutil.Process().pid, [sys.executable, str(tmp_path / "main.py")]),
            FakeProcess(5000, ["python", "-m", "pytest"]),
        ],
    )
    assert supervisor._find_running() is None


def test_adopt_or_start_adopts_an_existing_bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = make(tmp_path)
    bot = FakeProcess(5001, [sys.executable, str(tmp_path / "main.py")])
    patch_process_iter(monkeypatch, [bot])
    started: list[str] = []
    monkeypatch.setattr(supervisor, "_start", lambda: started.append("start"))

    before = time.time()
    supervisor._adopt_or_start()

    assert supervisor._process is bot
    assert supervisor._started_at >= before
    assert started == []


def test_adopt_or_start_starts_when_nothing_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = make(tmp_path)
    patch_process_iter(monkeypatch, [])
    started: list[str] = []
    monkeypatch.setattr(supervisor, "_start", lambda: started.append("start"))
    supervisor._adopt_or_start()
    assert started == ["start"]
    assert supervisor._process is None  # the fake _start did not set one


# ── run() loop ──────────────────────────────────────────────────────────────
class LoopExit(Exception):
    """Raised by the fake sleep to leave ``Supervisor.run``'s endless loop."""


def test_run_starts_then_restarts_on_a_stale_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    supervisor = make(tmp_path, stale_after=300.0)
    supervisor._check_interval = 5.0
    events: list[str] = []
    alive_answers = iter([False, True, True])
    stale_answers = iter([False, True])

    def sleep(seconds: float) -> None:
        events.append(f"sleep({seconds:g})")
        if len([e for e in events if e.startswith("sleep")]) == 3:
            raise LoopExit

    monkeypatch.setattr(
        rebooter, "time", SimpleNamespace(time=time.time, strftime=time.strftime, sleep=sleep)
    )
    monkeypatch.setattr(supervisor, "_alive", lambda: next(alive_answers))
    monkeypatch.setattr(supervisor, "_heartbeat_stale", lambda: next(stale_answers))
    monkeypatch.setattr(supervisor, "_find_running", lambda: None)
    monkeypatch.setattr(supervisor, "_start", lambda: events.append("start"))
    monkeypatch.setattr(supervisor, "_terminate", lambda: events.append("terminate"))

    with pytest.raises(LoopExit):
        supervisor.run()

    assert events == ["start", "sleep(5)", "sleep(5)", "terminate", "start", "sleep(5)"]
    out = capsys.readouterr().out
    assert f"supervising {tmp_path / 'main.py'}" in out
    assert "no heartbeat for 300s; restarting" in out


def test_run_adopts_a_running_bot_instead_of_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    supervisor = make(tmp_path)
    bot = FakeProcess(5001, [sys.executable, str(tmp_path / "main.py")])
    patch_process_iter(monkeypatch, [bot])

    def sleep(seconds: float) -> None:
        raise LoopExit

    monkeypatch.setattr(
        rebooter, "time", SimpleNamespace(time=time.time, strftime=time.strftime, sleep=sleep)
    )
    monkeypatch.setattr(supervisor, "_start", lambda: pytest.fail("must adopt, not start"))

    with pytest.raises(LoopExit):
        supervisor.run()

    assert supervisor._process is bot
    assert "adopting running bot (pid 5001)" in capsys.readouterr().out


# ── main() ──────────────────────────────────────────────────────────────────
def capture_supervisor(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def run(self: Supervisor) -> None:
        captured.update(
            script=self._script,
            python=self._python,
            heartbeat=self._heartbeat,
            stale_after=self._stale_after,
            check_interval=self._check_interval,
        )

    monkeypatch.setattr(Supervisor, "run", run)
    return captured


def test_main_uses_defaults_next_to_the_script(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture_supervisor(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["rebooter.py"])

    rebooter.main()

    here = Path(rebooter.__file__).resolve().parent
    assert captured == {
        "script": here / "main.py",
        "python": sys.executable,
        "heartbeat": here / "data" / "heartbeat",
        "stale_after": 300.0,
        "check_interval": 5.0,
    }


def test_main_accepts_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = capture_supervisor(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebooter.py",
            "--script",
            str(tmp_path / "bot.py"),
            "--heartbeat",
            str(tmp_path / "hb"),
            "--stale-after",
            "42",
            "--interval",
            "1.5",
        ],
    )

    rebooter.main()

    assert captured == {
        "script": (tmp_path / "bot.py").resolve(),
        "python": sys.executable,
        "heartbeat": tmp_path / "hb",
        "stale_after": 42.0,
        "check_interval": 1.5,
    }


def test_main_rejects_unknown_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_supervisor(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["rebooter.py", "--bogus"])
    with pytest.raises(SystemExit) as exc_info:
        rebooter.main()
    assert exc_info.value.code == 2
