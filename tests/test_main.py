"""Entry point: `docker stop` (SIGTERM) must close the DBs and exit 0."""
import os
import signal
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402


class _FakeOrchestrator:
    def __init__(self):
        self.closed = False

    def loop(self):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt the loop")

    def close(self):
        self.closed = True


@pytest.fixture
def restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_sigterm_closes_and_exits_zero(monkeypatch, restore_sigterm):
    orch = _FakeOrchestrator()
    monkeypatch.setattr(main.Orchestrator, "from_file", lambda path: orch)
    monkeypatch.setattr(sys, "argv", ["main.py", "--hunter", "hunter.yaml"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 0
    assert orch.closed
