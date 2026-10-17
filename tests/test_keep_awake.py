import os

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
    assert started[0].argv == ["/usr/bin/caffeinate", "-di", "-w", str(os.getpid())]
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
