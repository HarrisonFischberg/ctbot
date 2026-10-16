import pytest


@pytest.fixture(autouse=True)
def _isolated_launcher(tmp_path, monkeypatch):
    """Tests never read the real data/allocations.json written by the dashboard."""
    from ctbot.config import Settings

    monkeypatch.setattr(Settings, "allocations_path", property(lambda self: tmp_path / "allocations.json"))
