"""Keep a Mac awake while the market is open and let it sleep while it's closed.

Wraps macOS `caffeinate -di` (display and idle sleep) in a child process; a no-op where
`caffeinate` doesn't exist. `-w <pid>` ends the assertion with this process, even on a crash.
"""

from __future__ import annotations

import os
import shutil
import subprocess


class KeepAwake:
    def __init__(self, exe: str | None = None) -> None:
        self._exe = exe or shutil.which("caffeinate")
        self._proc: subprocess.Popen | None = None

    @property
    def held(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def hold(self) -> None:
        if self._exe and not self.held:
            self._proc = subprocess.Popen([self._exe, "-di", "-w", str(os.getpid())])

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
