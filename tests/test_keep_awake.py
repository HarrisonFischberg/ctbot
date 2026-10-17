import os
from datetime import datetime, timedelta, timezone

from ctbot import keep_awake


class FakeProc:
    def __init__(self, argv):
        self.argv, self.running = argv, True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False

    def wait(self, timeout=None):
        return 0


def test_holds_once_and_releases(monkeypatch):
    started = []
    monkeypatch.setattr(keep_awake.subprocess, "Popen", lambda argv: started.append(FakeProc(argv)) or started[-1])
    k = keep_awake.KeepAwake(exe="/usr/bin/caffeinate")
    k.hold()
    k.hold()
    assert len(started) == 1 and k.held
    assert started[0].argv == ["/usr/bin/caffeinate", "-dis", "-w", str(os.getpid())]
    k.release()
    assert not k.held and not started[0].running
    k.hold()  # re-held at the next open
    assert len(started) == 2


def test_noop_without_caffeinate(monkeypatch):
    monkeypatch.setattr(keep_awake.shutil, "which", lambda name: None)
    monkeypatch.setattr(keep_awake.subprocess, "Popen", lambda argv: (_ for _ in ()).throw(AssertionError))
    k = keep_awake.KeepAwake()
    k.hold()
    k.release()
    assert not k.held


def test_idle_until_open_holds_the_mac_awake_shortly_before_the_open(monkeypatch):
    started = []
    monkeypatch.setattr(keep_awake.subprocess, "Popen", lambda argv: started.append(FakeProc(argv)) or started[-1])
    k = keep_awake.KeepAwake(exe="/usr/bin/caffeinate")
    k.hold()  # still held from the session that just closed
    opens = datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)  # Monday 9:30 ET
    now = [opens - timedelta(hours=2)]
    seen = []

    def sleep(_seconds):
        now[0] += timedelta(minutes=10)

    keep_awake.idle_until_open(opens, k, lambda: seen.append((now[0], k.held)),
                               clock=lambda: now[0], sleep=sleep)
    assert now[0] == opens
    assert [held for _, held in seen] == [t >= opens - keep_awake.PRE_OPEN for t, _ in seen]
    assert seen[-1][1] and not seen[0][1]
    assert len(started) == 2 and not started[0].running and k.held  # released overnight, one hold


def test_idle_until_open_retakes_a_killed_hold(monkeypatch):
    started = []
    monkeypatch.setattr(keep_awake.subprocess, "Popen", lambda argv: started.append(FakeProc(argv)) or started[-1])
    k = keep_awake.KeepAwake(exe="/usr/bin/caffeinate")
    opens = datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)
    now = [opens - timedelta(minutes=10)]

    def tick():
        started[-1].running = False  # someone ends caffeinate

    def sleep(_seconds):
        now[0] += timedelta(minutes=5)

    keep_awake.idle_until_open(opens, k, tick, clock=lambda: now[0], sleep=sleep)
    assert len(started) == 2


def test_idle_until_open_returns_at_once_when_already_open(monkeypatch):
    monkeypatch.setattr(keep_awake.shutil, "which", lambda name: None)
    calls = []
    k = keep_awake.KeepAwake()
    opens = datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)
    keep_awake.idle_until_open(opens, k, lambda: calls.append(1), clock=lambda: opens,
                               sleep=lambda s: calls.append(s))
    assert calls == []
