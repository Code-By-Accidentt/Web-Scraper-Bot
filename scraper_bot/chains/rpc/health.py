"""Which RPC endpoints are failing, since when, and which optional ones to leave alone."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)


# We only report an outage with a failure this recent. An endpoint nobody asked lately isn't down.
_RECENT_FAILURE = 60.0
# Failures further apart start a new outage. RPC_RETRY_DELAY can put minutes between checks.
_NEW_OUTAGE_AFTER = 1800.0
# An optional endpoint rests after this many failures in a row. Probe gaps grow from 2 s to 30 s.
_REST_AFTER = 2
_FIRST_PROBE_GAP = 2.0
_PROBE_EVERY = 30.0


class RpcUnavailable(Exception):
    """No verdict is possible: an endpoint failed."""


@dataclass(frozen=True, slots=True)
class FailingEndpoint:
    """An RPC endpoint that has stopped answering."""

    name: str
    seconds: float  # failing for this long
    error: str  # its latest failure
    optional: bool = False  # its failure doesn't hold up any verdict


# Gets the endpoints failing for minutes, and RPC_RETRY_DELAY. The Notifier sends a 🛑.
StuckReporter = Callable[[Sequence[FailingEndpoint], float], None]


class EndpointHealth:
    """Remembers since when each RPC endpoint has been failing; its next answer clears that."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, optional: Collection[str] = ()
    ) -> None:
        """Track endpoints by name; those named in ``optional`` may rest while they fail."""
        self._clock = clock
        self._optional = frozenset(optional)
        self._since: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._streak: dict[str, int] = {}  # failures in a row
        self._probe_at: dict[str, float] = {}
        self._probe_gap: dict[str, float] = {}

    def failed(self, name: str, error: BaseException) -> None:
        """Record a failure of endpoint ``name``; the start of an outage is logged."""
        now = self._clock()
        last = self._last.get(name)
        self._last[name] = now
        self._errors[name] = str(error)
        self._streak[name] = self._streak.get(name, 0) + 1
        if name not in self._since or (last is not None and now - last > _NEW_OUTAGE_AFTER):
            self._since[name] = now
            log.warning("RPC endpoint %s is failing: %s", name, error)

    def answered(self, name: str) -> None:
        """Record an answer from endpoint ``name``: its outage, if any, ends and is logged."""
        self._last.pop(name, None)
        self._errors.pop(name, None)
        self._streak.pop(name, None)
        self._probe_at.pop(name, None)
        self._probe_gap.pop(name, None)
        if self._since.pop(name, None) is not None:
            log.info("RPC endpoint %s recovered", name)

    def resting(self, name: str) -> bool:
        """Return True while an optional endpoint that keeps failing should not be asked.

        Now and then it returns False to let a probe through: at once, then 2 s later, the
        gap doubling up to ``_PROBE_EVERY``, so a short hiccup costs seconds, not half a
        minute. A probe's answer ends the rest; required endpoints never rest.
        """
        if name not in self._optional or self._streak.get(name, 0) < _REST_AFTER:
            return False
        now = self._clock()
        probed = self._probe_at.get(name)
        gap = self._probe_gap.get(name, _FIRST_PROBE_GAP)
        if probed is None or now - probed >= gap:
            if probed is not None:
                self._probe_gap[name] = min(gap * 2, _PROBE_EVERY)
            self._probe_at[name] = now
            return False
        return True

    def down(self) -> set[str]:
        """Return the endpoints in an outage: those that failed since their last answer."""
        return set(self._since)

    def failing(self, seconds: float = 0.0) -> list[FailingEndpoint]:
        """Return the endpoints down for ``seconds`` or more that also failed lately."""
        now = self._clock()
        return [
            FailingEndpoint(name, now - since, self._errors[name], name in self._optional)
            for name, since in self._since.items()
            if now - since >= seconds and now - self._last[name] <= _RECENT_FAILURE
        ]
