"""ctbot-export: last-N-days window, dated files, and no keys in the zip."""

import io
import json
import zipfile
from datetime import UTC, datetime

from ctbot import export

NOW = datetime(2026, 11, 7, 15, tzinfo=UTC)


def test_export_window_and_no_keys(tmp_path):
    data = tmp_path / "data"
    (data / "learning" / "journal").mkdir(parents=True)
    (data / "variants" / "fast").mkdir(parents=True)
    (data / "bars").mkdir()
    (data / "trade_log.jsonl").write_text(
        json.dumps({"ts": "2026-10-01T15:00:00+00:00", "symbol": "OLD"}) + "\n"
        + json.dumps({"ts": "2026-11-05T15:00:00+00:00", "symbol": "NEW"}) + "\n")
    (data / "variants" / "fast" / "equity_history.jsonl").write_text(
        json.dumps({"ts": "2026-11-06T20:00:00+00:00", "equity": 1}) + "\n")
    (data / "learning" / "journal" / "2026-10-01.json").write_text("{}")
    (data / "learning" / "journal" / "2026-11-06.json").write_text("{}")
    (data / "lessons.json").write_text(json.dumps({"api_key": "abc123", "note": "sk-ant-" + "x" * 30}))
    (data / "robinhood_oauth.json").write_text("{}")
    (data / "bars" / "SPY.csv").write_text("cache")
    home = tmp_path / "home"
    (home / ".ctbot").mkdir(parents=True)
    (home / ".ctbot" / "tuning.json").write_text('{"RISK_PER_TRADE": 0.01}')
    (home / ".ctbot" / "settings.json").write_text('{"ANTHROPIC_API_KEY": "nope"}')

    z = zipfile.ZipFile(io.BytesIO(export.build(tmp_path, 14, home=home, now=NOW)))
    names = {n for n in z.namelist() if not n.startswith("config/")}  # conftest links the real config/
    assert "config/strategies.toml" in z.namelist()
    assert names == {"data/trade_log.jsonl", "data/variants/fast/equity_history.jsonl",
                     "data/learning/journal/2026-11-06.json", "data/lessons.json",
                     "home/.ctbot/tuning.json", "manifest.json"}
    assert "NEW" in z.read("data/trade_log.jsonl").decode() and "OLD" not in z.read("data/trade_log.jsonl").decode()
    lessons = z.read("data/lessons.json").decode()
    assert "abc123" not in lessons and "sk-ant-" not in lessons and "[redacted]" in lessons
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["since"] == "2026-10-24" and "data/robinhood_oauth.json" in manifest["skipped"]


def test_a_long_log_keeps_its_recent_days(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "MAX_BYTES", 200)
    data = tmp_path / "data"
    data.mkdir()
    old = json.dumps({"ts": "2026-01-01T15:00:00+00:00", "pad": "x" * 300}) + "\n"
    (data / "journal.jsonl").write_text(old + json.dumps({"ts": "2026-11-06T15:00:00+00:00"}) + "\n")
    z = zipfile.ZipFile(io.BytesIO(export.build(tmp_path, 14, home=tmp_path, now=NOW)))
    assert z.read("data/journal.jsonl").decode().count("\n") == 1
