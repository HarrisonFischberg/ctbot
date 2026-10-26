"""End-of-day readout: every book's day P&L, trades, halts and log events; sent once after the close."""

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ctbot import readout
from ctbot.config import Settings

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 21)  # a Wednesday


def _at(h, m, day=21):
    return datetime(2026, 10, day, h, m, tzinfo=ET).astimezone(UTC)


def _book(d, equity, fills, risk=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "equity_history.jsonl").write_text("".join(
        json.dumps({"ts": ts.isoformat(), "equity": e, "positions": 1}) + "\n" for ts, e in equity))
    (d / "paper_ledger.json").write_text(json.dumps({"fills": [
        {"ts": ts.isoformat(), "symbol": s, "side": side, "quantity": q, "price": p, "tag": t}
        for ts, s, side, q, p, t in fills]}))
    if risk:
        (d / "paper_risk_state.json").write_text(json.dumps(risk))


def _settings(tmp_path):
    data = tmp_path / "data"
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "variants.toml").write_text('[tight]\ntitle = "Tight trail"\n')
    _book(data, [(_at(15, 59, 20), 10000.0), (_at(10, 0), 10010.0), (_at(15, 59), 10050.0)],
          [(_at(10, 0, 20), "AAPL", "buy", 10, 100.0, "orb"), (_at(11, 0), "AAPL", "sell", 10, 105.0, "trail"),
           (_at(12, 0), "MSFT", "buy", 5, 50.0, "orb")])
    _book(data / "variants" / "tight", [(_at(15, 59, 20), 10000.0), (_at(15, 59), 9980.0)],
          [(_at(10, 0), "NVDA", "buy", 4, 50.0, "orb"), (_at(10, 30), "NVDA", "sell", 4, 45.0, "stop")],
          risk={"kill_switch": False, "risk_off": ["SPY below its trend"]})
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ctbot-paper.log").write_text(
        "2026-10-21 10:00:00,1 WARNING ctbot.x quote late from 95.217.169.154 key sk-ant-abcdefghijklmnopqrstuvwxyz\n"
        "2026-10-21 10:05:00,1 ERROR ctbot.variants book tight cycle failed; continuing\n"
        "2026-10-20 10:05:00,1 ERROR ctbot.x yesterday's error\n")
    return Settings.from_env({"PAPER_LEDGER_PATH": str(data / "paper_ledger.json"), "LOG_DIR": str(logs),
                              "PAPER_RISK_STATE_PATH": str(data / "paper_risk_state.json"),
                              "STRATEGIES_FILE": str(tmp_path / "cfg" / "strategies.toml")})


def test_readout_ranks_books_and_lists_trades_halts_and_masked_events(tmp_path):
    text = readout.build(_settings(tmp_path), DAY)
    main_row = next(ln for ln in text.splitlines() if ln.startswith("| 1 |"))
    assert "main" in main_row and "+50.00" in main_row and "+0.50%" in main_row and "1 (1/0)" in main_row
    assert "| 2 | tight | -20.00 |" in text
    assert "closed AAPL +50.00 (trail)" in text and "bought: MSFT" in text and "closed NVDA -20.00 (stop)" in text
    assert "pulled back: SPY below its trend" in text
    assert "1 errors, 1 warnings" in text and "yesterday" not in text
    assert "95.217" not in text and "sk-ant" not in text and "<ip>" in text and "<token>" in text


def test_sent_once_15_minutes_after_the_close(tmp_path, monkeypatch):
    s, state = _settings(tmp_path), tmp_path / "readout_state.json"
    sent = []
    monkeypatch.setattr(readout, "send", lambda url, day, text: sent.append(day))
    assert not readout.run("u", s, state, _at(16, 10))
    assert readout.run("u", s, state, _at(16, 16)) and sent == [DAY]
    assert not readout.run("u", s, state, _at(17, 0)) and sent == [DAY]
    assert not readout.run("u", s, state, _at(16, 30, 24))  # Saturday
