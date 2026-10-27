"""Dashboard: state snapshot from local files, no secrets, loopback-only read-only server."""

import json
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from ctbot.config import Settings
from ctbot.dashboard.server import make_handler
from ctbot.dashboard.state import build_state
from ctbot.models import Quote

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)  # Tue 11:00 ET


def _settings(d: Path, **env) -> Settings:
    base = {
        "PAPER_LEDGER_PATH": str(d / "ledger.json"),
        "PAPER_RISK_STATE_PATH": str(d / "risk.json"),
        "LOG_DIR": str(d / "logs"),
        "UNIVERSE_FILE": str(ROOT / "config" / "universe.txt"),
        "STRATEGIES_FILE": str(ROOT / "config" / "strategies.toml"),
        "ROBINHOOD_ACCOUNT_NUMBER": "123459999",
        "ROBINHOOD_ACCOUNT_NICKNAME": "Bot",
        "ANTHROPIC_API_KEY": "sk-ant-api-SECRET",
    }
    return Settings.from_env({**base, **env})


def _write_fixture(d: Path) -> None:
    (d / "ledger.json").write_text(json.dumps({
        "cash": 150.0,
        "unsettled": [{"settles": "2026-10-14", "amount": 25.0}],
        "positions": [{"symbol": "KO", "quantity": 1.0, "avg_cost": 50.0,
                       "opened_at": "2026-10-13T14:00:00+00:00"}],
        "fills": [{"client_order_id": "a", "symbol": "KO", "side": "buy", "quantity": 1.0,
                   "price": 50.0, "ts": "2026-10-13T14:00:00+00:00", "simulated": True}],
    }))
    (d / "risk.json").write_text(json.dumps({
        "peak_equity": 260.0, "day_start_equity": 250.0, "day_start_date": "2026-10-13",
        "day_trade_dates": ["2026-10-12", "2026-09-01"], "kill_switch": False,
        "brackets": {"KO": {"stop": 47.5, "target": None}},
    }))
    (d / "equity_history.jsonl").write_text(
        '{"ts": "2026-10-13T13:45:00+00:00", "equity": 250.0, "cash": 250.0}\nnot json\n'
    )
    (d / "logs").mkdir()
    (d / "logs" / "ctbot-paper.log").write_text(
        "2026-10-13 09:45:00,001 INFO ctbot.pipeline rejected long MSFT: claude_rejected\n"
        "2026-10-13 09:46:00,001 INFO ctbot.pipeline submitted OrderRequest(KO) live=False fill=Fill(...)\n"
        "2026-10-13 09:47:00,001 WARNING ctbot.pipeline stop_loss exit blocked for KO: pdt_guard\n"
    )


def test_state_from_files_is_correct_and_has_no_secrets():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _write_fixture(d)
        quotes = lambda syms: {s: Quote(s, 52.0, 52.0, 52.0) for s in syms if s == "KO"}  # noqa: E731
        s = build_state(_settings(d), quotes, NOW)
    a = s["account_summary"]
    assert a["equity"] == 150 + 25 + 52 and round(a["total_pnl"], 2) == -23.0
    assert s["positions"][0]["stop"] == 47.5 and s["positions"][0]["pnl"] == 2.0
    assert a["day_trades_5d"] == 1  # 2026-09-01 is outside the window
    assert len(s["equity_history"]) == 1  # bad line skipped
    assert [r["kind"] for r in s["log"]] == ["reject", "fill", "risk"]
    assert s["account"] == {"nickname": "Bot", "last4": "9999"}
    blob = json.dumps(s, default=str)
    assert "123459999" not in blob and "SECRET" not in blob and "sk-ant" not in blob


def test_state_survives_missing_files_and_quote_errors():
    def boom(_):
        raise RuntimeError("Robinhood sign-in needed")

    with tempfile.TemporaryDirectory() as t:
        s = build_state(_settings(Path(t)), boom, NOW)
    assert s["account_summary"]["equity"] == 250.0
    assert "sign-in needed" in s["market"]["data_error"]


def test_server_is_read_only_and_rejects_foreign_hosts():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _write_fixture(d)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_settings(d, MARKET_DATA="csv"), 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = make_handler(_settings(d, MARKET_DATA="csv"), port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{port}"
            assert b"ctbot" in urllib.request.urlopen(base + "/").read()
            state = json.loads(urllib.request.urlopen(base + "/api/state").read())
            assert state["mode"] == "paper"

            def status(req):
                try:
                    return urllib.request.urlopen(req).status
                except urllib.error.HTTPError as e:
                    return e.code

            evil = urllib.request.Request(base + "/api/state", headers={"Host": "evil.test"})
            assert status(evil) == 403
            # Bundled fonts are served by exact name only.
            assert status(base + "/fonts/barlow-latin-400-normal.woff2") == 200
            assert status(base + "/fonts/nope.woff2") == 404
            assert status(base + "/fonts/..%2Fserver.py") == 404
            # Without the page's token, nothing can be queued.
            post = urllib.request.Request(base + "/api/paper/order", data=b'{"side":"buy","symbol":"KO"}',
                                          headers={"Content-Type": "application/json"}, method="POST")
            assert status(post) == 403
            page = urllib.request.urlopen(base + "/").read().decode()
            token = page.split('name="ctbot-token" content="', 1)[1].split('"', 1)[0]
            assert token and "__CTBOT_TOKEN__" not in page

            def order(body, **hdr):
                return urllib.request.Request(
                    base + "/api/paper/order", data=json.dumps(body).encode(), method="POST",
                    headers={"Content-Type": "application/json", "X-Ctbot-Token": token, **hdr})

            assert status(order({"side": "buy", "symbol": "KO", "notional": 50})) == 202
            assert status(order({"side": "buy", "symbol": "KO", "notional": 5000})) == 400  # > $250 cap
            assert status(order({"side": "short", "symbol": "KO"})) == 400
            assert status(order({"side": "buy", "symbol": "KO", "idea_id": "nope"})) == 400
            assert status(order({"side": "sell", "symbol": "KO"}, Origin="https://evil.test")) == 403
            # Pause all: the HALT file stops new buys; resume removes it
            assert status(urllib.request.Request(base + "/api/pause", data=b'{"paused": true}', method="POST",
                          headers={"Content-Type": "application/json", "X-Ctbot-Token": token})) == 200
            assert (d / "HALT").exists() and json.loads(urllib.request.urlopen(base + "/api/state").read())["halted"]
            assert status(urllib.request.Request(base + "/api/pause", data=b'{"paused": false}', method="POST",
                          headers={"Content-Type": "application/json", "X-Ctbot-Token": token})) == 200
            assert not (d / "HALT").exists()
            # The new Observatory view is served beside the classic page, with the same token
            assert token in urllib.request.urlopen(base + "/observatory").read().decode()
            queued = (d / "paper_manual_orders.jsonl").read_text().splitlines()
            assert len(queued) == 1 and json.loads(queued[0])["notional"] == 50.0
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_c2_section_merges_ranking_watchlist_and_tracking():
    from ctbot.dashboard.state import _c2

    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        (d / "ranking_20261014.json").write_text(json.dumps({
            "generated_at": "2026-10-14T07:00:00+00:00", "counts": {"pass_filters": 2},
            "candidates": [{"strategy_id": 1, "name": "A", "r12m": 0.5, "monthly_cost": 20, "fails": []},
                           {"strategy_id": 2, "name": "B", "r12m": 0.3, "fails": []},
                           {"strategy_id": 3, "name": "C", "r12m": 0.9, "fails": ["shorts"]}]}))
        (d / "watchlist.json").write_text(json.dumps({"strategies": [{"strategy_id": 1, "name": "A"}]}))
        (d / "track.json").write_text(json.dumps({"updated": "x", "rows": [
            {"strategy_id": 1, "added": "2026-10-14", "forward_return": 0.10, "spy_return": 0.04,
             "fee_drag_on_250": 0.08}]}))
        c = _c2(d)
    w = c["watchlist"][0]
    assert w["r12m"] == 0.5 and round(w["edge_after_fees"], 4) == -0.02
    assert [r["strategy_id"] for r in c["runners_up"]] == [2]  # 3 failed a filter


def test_model_stats_pairs_fills_fifo_per_model():
    from ctbot.dashboard.state import model_stats

    f = lambda side, sym, q, px, ts, tag: {"side": side, "symbol": sym, "quantity": q, "price": px, "ts": ts, "tag": tag}  # noqa: E731
    fills = [f("buy", "KO", 1, 100, "1", "orb"), f("sell", "KO", 1, 102, "2", "take_profit"),
             f("buy", "XLE", 2, 50, "3", "overnight"), f("sell", "XLE", 2, 49, "4", "time_exit"),
             f("buy", "MA", 0.1, 600, "5", "orb")]
    m = {s["model"]: s for s in model_stats(fills, {"MA": 610})}
    assert m["orb"]["closed"] == 1 and m["orb"]["pnl"] == 2 and m["orb"]["open"] == 1
    assert round(m["orb"]["open_pnl"], 6) == 1.0 and m["orb"]["exits"] == {"take_profit": 1}
    assert m["overnight"]["pnl"] == -2 and m["overnight"]["win_rate"] == 0


def test_fills_with_pnl_prices_each_sell_against_its_buys():
    from ctbot.dashboard.state import fills_with_pnl

    f = lambda side, sym, q, px, ts: {"side": side, "symbol": sym, "quantity": q, "price": px, "ts": ts}  # noqa: E731
    fills = [f("buy", "KO", 1, 100, "1"), f("buy", "KO", 1, 110, "2"), f("sell", "KO", 1.5, 120, "3"),
             f("sell", "XLE", 1, 50, "4")]  # a sell with no buy on record gets no profit figure
    out = fills_with_pnl(fills)
    assert "pnl" not in out[0] and "pnl" not in fills[2]  # buys untouched, inputs not mutated
    assert round(out[2]["pnl"], 6) == 25.0  # 1 @100 + 0.5 @110 sold at 120
    assert round(out[2]["pnl_pct"], 6) == round(120 / (155 / 1.5) - 1, 6)
    assert "pnl" not in out[3]


def test_tailnet_hosts_accepts_only_tailscale_names():
    import pytest

    from ctbot.dashboard.server import tailnet_hosts

    assert tailnet_hosts({}) == []
    assert tailnet_hosts({"CTBOT_DASHBOARD_HOSTS": " CTbot.tail1a2b.ts.net "}) == ["ctbot.tail1a2b.ts.net"]
    for bad in ("example.com", "ctbot.ts.net.evil.com", "0.0.0.0", "ctbot.tail1a2b.ts.net:8787"):
        with pytest.raises(SystemExit):
            tailnet_hosts({"CTBOT_DASHBOARD_HOSTS": bad})


def test_server_accepts_its_tailnet_name_and_still_rejects_others(monkeypatch):
    import urllib.error
    import urllib.request

    monkeypatch.setenv("CTBOT_DASHBOARD_HOSTS", "ctbot.tail1a2b.ts.net")
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _write_fixture(d)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_settings(d, MARKET_DATA="csv"), 0))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{port}"

            def status(path, **hdr):
                try:
                    return urllib.request.urlopen(urllib.request.Request(base + path, headers=hdr)).status
                except urllib.error.HTTPError as e:
                    return e.code

            assert status("/api/state", Host="ctbot.tail1a2b.ts.net") == 200
            assert status("/api/state", Host="other.tail1a2b.ts.net") == 403
            assert status("/api/state", Host="evil.test") == 403
        finally:
            httpd.shutdown()


def test_what_you_own_shows_on_home_and_autopilot():
    html = (ROOT / "src" / "ctbot" / "dashboard" / "index.html").read_text()
    assert '<div data-tab="home trading" class="card span-12 own">' in html
    new = (ROOT / "src" / "ctbot" / "dashboard" / "observatory.html").read_text()
    assert '<div data-tab="home trading" data-sub="overview" class="card span-12 own">' in new



def test_hidden_topup_row_stays_hidden():
    """A display rule on the row must not beat [hidden], or "Add -$236 paper cash" shows above the limit."""
    html = (ROOT / "src" / "ctbot" / "dashboard" / "index.html").read_text()
    assert 'class="empty-row" id="topup" hidden' in html and ".empty-row[hidden] { display: none; }" in html
    new = (ROOT / "src" / "ctbot" / "dashboard" / "observatory.html").read_text()
    assert 'class="ap-side" id="topup" hidden' in new and "[hidden], .tab-off, .sub-off { display: none !important; }" in new
    for page in (html, new):
        assert '$("topup").hidden = !(short >= 1);' in page


def test_prev_closes_needs_the_exact_previous_session(tmp_path):
    from ctbot.dashboard.state import prev_closes
    from ctbot.models import Bar
    from ctbot.research import bars as bar_cache

    day = lambda d, c: Bar("KO", datetime(2026, 10, d, tzinfo=timezone.utc), c, c, c, c, 1)  # noqa: E731
    bar_cache.save("day", "KO", [day(8, 49.0), day(9, 50.0)], tmp_path)  # Thu, Fri
    mon = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)  # Mon 11:00 ET: yesterday's session is Fri
    assert prev_closes(["KO", "PEP"], mon, tmp_path) == {"KO": 50.0}
    assert prev_closes(["KO"], NOW, tmp_path) == {}  # Tue: Monday's close isn't saved, so no guess


def test_observatory_refresh_only_rewrites_changed_cards():
    # The 10s refresh used to rebuild every card, which froze the page and reset scroll and hover.
    html = (ROOT / "src" / "ctbot" / "dashboard" / "observatory.html").read_text()
    assert not re.search(r'\$\("\w+"\)\.innerHTML = ', html), "write card HTML with put()"
    assert "setInterval(() => document.hidden || load(), 10000)" in html
