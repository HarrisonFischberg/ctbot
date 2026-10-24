from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_launcher(tmp_path, monkeypatch):
    """Tests never read the real data/allocations.json written by the dashboard."""
    from ctbot.config import Settings

    monkeypatch.setattr(Settings, "allocations_path", property(lambda self: tmp_path / "allocations.json"))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Tests never read the real .env or ~/.ctbot, and never leak env changes into each other."""
    import os

    from ctbot import secret_store

    (tmp_path / "config").symlink_to(Path(__file__).parents[1] / "config")
    monkeypatch.chdir(tmp_path)  # find_dotenv(usecwd=True) finds no .env; relative data/ paths stay here
    monkeypatch.setattr(secret_store, "STORE_PATH", tmp_path / "home" / "settings.json")
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)
