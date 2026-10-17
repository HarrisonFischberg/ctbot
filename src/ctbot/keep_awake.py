"""Keep a Mac awake while the market is open and let it sleep while it's closed.

Wraps macOS `caffeinate -dis` in a child process; a no-op where `caffeinate` doesn't exist.
`-d` and `-i` stop display and idle sleep, but Apple's IOPMLib says neither has any effect in a
dark wake (the display-off wake a closed-lid Mac gets from a `pmset` schedule) or stops lid-close
sleep. `-s` prevents system sleep and is honored on AC power only. `-w <pid>` ends the assertion
with this process, even on a crash.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

# Held awake from this long before the open, so a Mac woken by the weekday `pmset` schedule
# (06:55 Mountain = 08:55 ET, 35 minutes early) doesn't idle-sleep again before 9:30 ET.
PRE_OPEN = timedelta(minutes=45)

log = logging.getLogger(__name__)


class KeepAwake:
    def __init__(self, exe: str | None = None) -> None:
        self._exe = exe or shutil.which("caffeinate")
        self._proc: subprocess.Popen | None = None

    @property
    def held(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def hold(self) -> None:
        if self._exe and not self.held:
            self._proc = subprocess.Popen([self._exe, "-dis", "-w", str(os.getpid())])

    def release(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


def idle_until_open(opens_at: datetime, keep_awake: KeepAwake, tick: Callable[[], None],
                    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                    sleep: Callable[[float], None] = time.sleep) -> None:
    """Return at `opens_at`, calling `tick` every ~2 s meanwhile. The Mac may sleep until
    PRE_OPEN before the open and is held awake from then on. Checks the wall clock, so it returns
    on time after the Mac sleeps."""
    awake_from = opens_at - PRE_OPEN
    holding = False
    while (now := clock()) < opens_at:
        if now >= awake_from:
            if not holding:
                log.info("%d minutes to the open: keeping the Mac awake",
                         round((opens_at - now).total_seconds() / 60))
            holding = True
            keep_awake.hold()  # also re-takes it if caffeinate was killed
        else:
            keep_awake.release()
        tick()
        sleep(2)
