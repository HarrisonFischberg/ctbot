"""Premarket plan and end-of-day reconciliation (read-only records)."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from ctbot.config import Settings
from ctbot.dayplan import plan, reconcile


def _settings(tmp: Path, **env) -> Settings:
    return Settings.from_env({"PAPER_LEDGER_PATH": str(tmp / "l.json"),
                              "PAPER_RISK_STATE_PATH": str(tmp / "r.json"), **env})


def test_plan_says_no_trade_with_reasons(tmp_path):
    s = _settings(tmp_path)
    holiday = plan(s, date(2026, 12, 25))
    assert holiday["verdict"] == "no trade" and not holiday["session"]
    ok = plan(s, date(2026, 10, 21))
    assert ok["session"] and ok["verdict"] == "entries possible", ok["reasons"]
    assert "ranker" in [m["model"] for m in ok["models"]]
    s.halt_path.write_text("x")
    assert any("HALT" in r for r in plan(s, date(2026, 10, 21))["reasons"])


def test_reconcile_finds_mismatches(tmp_path):
    s = _settings(tmp_path)
    fills = [
        {"client_order_id": "a", "symbol": "KO", "side": "buy", "quantity": 1.0, "price": 50.0,
         "ts": "2026-10-20T14:00:00+00:00", "tag": "rsi2_etf", "simulated": True},
        {"client_order_id": "b", "symbol": "KO", "side": "sell", "quantity": 1.0, "price": 52.0,
         "ts": "2026-10-20T19:00:00+00:00", "tag": "time_exit", "simulated": True},
        {"client_order_id": "c", "symbol": "QQQ", "side": "buy", "quantity": 0.1, "price": 700.0,
         "ts": "2026-10-20T19:30:00+00:00", "tag": "trend_core", "simulated": True},
    ]
    s.paper_ledger_path.write_text(json.dumps({"cash": 100, "fills": fills,
                                               "positions": [{"symbol": "QQQ", "quantity": 0.1}]}))
    s.paper_risk_state_path.write_text(json.dumps({"brackets": {"QQQ": {"stop": 600}}}))
    r = reconcile(s, date(2026, 10, 20))
    assert r["ok"], r["issues"]
    assert r["realized_pnl_by_model"] == {"rsi2_etf": 2.0}
    s.paper_risk_state_path.write_text(json.dumps({"brackets": {"KO": {"stop": 40}}}))
    s.journal_path.write_text(json.dumps({"kind": "execution_outcome", "status": "filled", "client_order_id": "zzz",
                                          "symbol": "MSFT", "ts": "2026-10-20T15:00:00+00:00"}) + "\n")
    bad = reconcile(s, date(2026, 10, 20))
    assert not bad["ok"] and len(bad["issues"]) == 3
