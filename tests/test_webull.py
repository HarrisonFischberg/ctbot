import hashlib
import json
import stat
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from ctbot.market_data import webull_client as wbc
from ctbot.market_data.failover import FirstBackup, WebullDailyBars
from ctbot.models import Bar
from ctbot.net import SourceUnavailable

KEY, SECRET = "k" * 32, "s" * 32  # fake test values


def test_signature_matches_webulls_published_example():
    # the worked example on developer.webull.com/apis/docs/authentication/signature
    params = {"a1": "webull", "a2": "123", "a3": "xxx", "q1": "yyy", "host": "api.webull.com",
              "x-app-key": "776da210ab4a452795d74e726ebd74b6", "x-timestamp": "2022-01-04T03:55:31Z",
              "x-signature-version": "1.0", "x-signature-algorithm": "HMAC-SHA1",
              "x-signature-nonce": "48ef5afed43d4d91ae514aaeafbc29ba"}
    body = '{"k1":123,"k2":"this is the api request body","k3":true,"k4":{"foo":[1,2]}}'
    enc = wbc.string_to_sign("/trade/place_order", params, body, hashlib.md5)
    assert enc.startswith("%2Ftrade%2Fplace_order%26a1%3Dwebull") and enc.endswith("%26E296C96787E1A309691CEF3692F5EEDD")
    assert wbc.sign("0f50a2e853334a9aae1a783bee120c1f", enc, "HMAC-SHA1") == "kvlS6opdZDhEBo5jq40nHYXaLvM="


def test_only_market_data_and_sign_in_paths_are_allowed():
    for method, path in wbc.READ_ONLY:
        assert path.startswith(("/market-data/", "/auth/tokens/", "/openapi/config")), path
        assert not any(w in path for w in ("order", "trade", "account", "position")), path

    def never(_req):
        raise AssertionError("a refused path reached the network")

    c = wbc.WebullClient(KEY, SECRET, http=httpx.Client(transport=httpx.MockTransport(never)))
    for method, path in [("POST", "/openapi/trade/order/place"), ("POST", "/trade/order/place"),
                         ("GET", "/openapi/account/list"), ("GET", "/market-data/stocks/bars/list")]:
        with pytest.raises(wbc.PathNotAllowed):
            c.request(method, path)
    with pytest.raises(SourceUnavailable):  # the signed key only goes to Webull's own hosts
        wbc.WebullClient(KEY, SECRET, host="evil.example.com").request("GET", "/openapi/config")


def _server(seen, reply, status=200):
    """A fake Webull that checks each request's signature the way Webull does."""
    def handler(req: httpx.Request) -> httpx.Response:
        h = req.headers
        assert SECRET not in str(req.headers) and SECRET.encode() not in req.content
        signed = {k: h[k] for k in ("x-app-key", "x-signature-algorithm", "x-signature-version",
                                    "x-signature-nonce", "x-timestamp")}
        query = dict(parse_qsl(urlsplit(str(req.url)).query))
        body = req.content.decode() or None
        enc = wbc.string_to_sign(req.url.path, {**query, **signed, "host": req.url.host}, body, hashlib.sha256)
        assert h["x-signature"] == wbc.sign(SECRET, enc, "HMAC-SHA256") and h["x-version"] == "v3"
        seen.append(req)
        return httpx.Response(status, json=reply(req) if callable(reply) else reply)
    return httpx.Client(transport=httpx.MockTransport(handler))


BARS_REPLY = {"result": [{"symbol": "SPY", "instrument_id": "1", "result": [
    {"time": "2026-10-15T04:00:00.000+0000", "open": "601", "high": "605", "low": "600", "close": "604", "volume": "10"},
    {"time": "2026-10-14T04:00:00.000+0000", "open": "598", "high": "602", "low": "597", "close": "601", "volume": "9"},
    {"time": "2026-10-16T04:00:00.000+0000", "open": "604", "high": "606", "low": "603", "close": "605", "volume": "3"},
]}]}


def test_bars_are_signed_parsed_oldest_first_and_carry_the_2fa_token(tmp_path):
    seen = []
    tok = tmp_path / "webull_token.json"
    c = wbc.WebullClient(KEY, SECRET, token_path=tok, http=_server(seen, BARS_REPLY))
    bars = c.bars(["spy"], "day", 5)["SPY"]
    assert [b.close for b in bars] == [601, 604, 605] and bars[0].ts == datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
    sent = json.loads(seen[0].content)
    assert sent == {"symbols": ["SPY"], "category": "US_STOCK", "timespan": "D", "count": 5}
    assert "x-access-token" not in seen[0].headers
    tok.write_text(json.dumps({"token": "t0k3n", "status": "NORMAL"}))
    c.bars(["SPY"], "day", 5)
    assert seen[1].headers["x-access-token"] == "t0k3n"


def test_two_factor_token_is_saved_privately_and_checked(tmp_path):
    seen = []
    tok = tmp_path / "ctbot" / "webull_token.json"

    def reply(req):
        if req.url.path == "/openapi/config":
            return {"token_check_enabled": True}
        if req.url.path == "/auth/tokens/create":
            return {"token": "t0k3n", "expires": 1, "status": "PENDING"}
        return {"token": "t0k3n", "expires": 2, "status": "NORMAL"}

    c = wbc.WebullClient(KEY, SECRET, token_path=tok, http=_server(seen, reply))
    assert c.token_required() and c.check_token() is None
    assert c.create_token() == "PENDING"
    assert stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert c.check_token() == "NORMAL" and c.saved_token()["status"] == "NORMAL"
    assert json.loads(seen[-1].content) == {"token": "t0k3n"} and "x-access-token" not in seen[-1].headers
    c.forget_token()
    assert c.saved_token() is None


def test_errors_never_show_the_keys_and_repeated_failures_pause_webull(tmp_path):
    t = [0.0]
    c = wbc.WebullClient(KEY, SECRET, token_path=tmp_path / "t.json", clock=lambda: t[0], failure_threshold=2,
                         http=_server([], {"message": f"bad app key {KEY}"}, status=401))
    for _ in range(2):
        with pytest.raises(SourceUnavailable) as e:
            c.bars(["SPY"], "day", 5)
        assert KEY not in str(e.value) and "HTTP 401" in str(e.value)
    with pytest.raises(SourceUnavailable, match="cooling down"):
        c.bars(["SPY"], "day", 5)
    t[0] = 601.0
    with pytest.raises(SourceUnavailable, match="HTTP 401"):
        c.bars(["SPY"], "day", 5)


def test_keys_are_read_at_call_time(monkeypatch):
    monkeypatch.delenv(wbc.KEY_ENV, raising=False)
    monkeypatch.delenv(wbc.SECRET_ENV, raising=False)
    c = wbc.WebullClient()
    assert not c.configured
    with pytest.raises(SourceUnavailable, match="not set"):
        c.bars(["SPY"], "day", 5)
    monkeypatch.setenv(wbc.KEY_ENV, KEY)
    monkeypatch.setenv(wbc.SECRET_ENV, SECRET)
    assert c.configured


class _FakeWebull:
    def __init__(self, configured=True, fail=False):
        self.configured, self.fail, self.calls = configured, fail, 0

    def bars(self, symbols, interval, count):
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("webull: HTTP 417 no market data permission")
        return wbc.parse_bars(BARS_REPLY)


class _Other:
    def __init__(self):
        self.warmed = set()

    def warm(self, symbols):
        self.warmed |= set(symbols)

    def daily_bars(self, symbol, lookback_days):
        return [Bar(symbol, datetime(2026, 10, 1, tzinfo=timezone.utc), 1, 1, 1, 1, 1)]


def test_webull_daily_bars_back_up_robinhood_before_tradingflow():
    now = lambda: datetime(2026, 10, 16, 15, tzinfo=timezone.utc)  # noqa: E731 - during Oct 16's session
    wb, other = _FakeWebull(), _Other()
    chain = FirstBackup(WebullDailyBars(wb, clock=now), other)
    chain.warm(["SPY"])
    bars = chain.daily_bars("SPY", 10)
    assert [b.ts.date().isoformat() for b in bars] == ["2026-10-14", "2026-10-15"]  # today's bar still forming
    assert bars[0].ts == datetime(2026, 10, 14, tzinfo=timezone.utc) and other.warmed == {"SPY"}
    chain.daily_bars("SPY", 10)
    assert wb.calls == 1  # cached for the trading day
    for webull in (_FakeWebull(configured=False), _FakeWebull(fail=True)):
        assert FirstBackup(WebullDailyBars(webull, clock=now), other).daily_bars("SPY", 10)[0].close == 1


def test_candles_fall_back_from_robinhood_to_webull_to_saved_bars(tmp_path):
    from ctbot.dashboard.candles import CandleSource
    from ctbot.research import bars as bar_cache

    class _RH:
        def intraday_bars(self, symbols, start, interval="5minute"):
            raise RuntimeError("Robinhood sign-in needed")

    src = CandleSource(_RH(), _FakeWebull(), cache_root=tmp_path)
    out = src.get("spy", "3m")
    assert out["source"] == "Webull" and [b["c"] for b in out["bars"]] == [601, 604, 605]
    # daily candles are stamped at noon UTC so the browser shows the same date in New York
    assert datetime.fromtimestamp(out["bars"][0]["t"] / 1000, timezone.utc) == datetime(2026, 10, 14, 12, tzinfo=timezone.utc)

    week = [Bar("KO", datetime(2026, 10, 12, tzinfo=timezone.utc) + timedelta(days=i), 10 + i, 12 + i, 9 + i, 11 + i, 5)
            for i in range(5)] + [Bar("KO", datetime(2026, 10, 19, tzinfo=timezone.utc), 20, 21, 19, 20.5, 1)]
    bar_cache.save("day", "KO", week, root=tmp_path)
    out = CandleSource(_RH(), _FakeWebull(fail=True), cache_root=tmp_path).get("KO", "1y")
    assert out["source"] == "saved bars"
    assert [(b["o"], b["h"], b["l"], b["c"], b["v"]) for b in out["bars"]] == [(10, 16, 9, 15, 25), (20, 21, 19, 20.5, 1)]

    nothing = CandleSource(None, _FakeWebull(fail=True), cache_root=tmp_path).get("ZZZ", "1d")
    assert nothing["bars"] == [] and nothing["source"] is None
    assert any("no market data permission" in n for n in nothing["notes"])


def test_intraday_candles_keep_the_latest_sessions_and_build_hours(tmp_path):
    from ctbot.dashboard.candles import CandleSource
    from ctbot.research import bars as bar_cache

    def five(day, n):  # n five-minute bars from 9:30 New York (13:30 UTC in October)
        t0 = datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc)
        return [Bar("KO", t0 + timedelta(minutes=5 * i), 50 + i, 51 + i, 49 + i, 50.5 + i, 100) for i in range(n)]

    bar_cache.save("5minute", "KO", five(15, 3) + five(16, 14), root=tmp_path)
    src = CandleSource(None, None, cache_root=tmp_path)
    day = src.get("KO", "1d")["bars"]
    assert len(day) == 14 and day[0]["o"] == 50  # only Oct 16
    hours = src.get("KO", "1w")["bars"]
    assert [len(hours), hours[1]["o"], hours[1]["c"], hours[1]["v"]] == [3, 50, 61.5, 1200]  # 9:30-10:30 on Oct 16


def test_dashboard_bars_and_webull_routes(tmp_path, monkeypatch):
    from ctbot import secret_store
    from ctbot.config import Settings
    from ctbot.dashboard.server import make_handler

    monkeypatch.setattr(secret_store, "STORE_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(wbc, "TOKEN_PATH", tmp_path / "webull_token.json")
    monkeypatch.delenv(wbc.KEY_ENV, raising=False)
    monkeypatch.delenv(wbc.SECRET_ENV, raising=False)
    (tmp_path / "webull_token.json").write_text(json.dumps({"token": "t0k3n", "status": "NORMAL"}))
    s = Settings.from_env({"PAPER_LEDGER_PATH": str(tmp_path / "ledger.json"), "LOG_DIR": str(tmp_path / "logs"),
                           "MARKET_DATA": "csv"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(s, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/bars?symbol=../../etc&span=1d")
        assert e.value.code == 400
        got = json.loads(urllib.request.urlopen(base + "/api/bars?symbol=ZZZQ&span=1y").read())
        assert got["symbol"] == "ZZZQ" and got["bars"] == [] and got["interval"] == "1 week"
        st = urllib.request.urlopen(base + "/api/settings").read().decode()
        assert "t0k3n" not in st and json.loads(st)["webull"]["token"] == "NORMAL"
        assert {"WEBULL_APP_KEY", "WEBULL_APP_SECRET"} <= set(json.loads(st)["keys"])
        page = urllib.request.urlopen(base + "/").read().decode()
        token = page.split('name="ctbot-token" content="', 1)[1].split('"', 1)[0]

        def post(path):
            req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "X-Ctbot-Token": token})
            try:
                r = urllib.request.urlopen(req)
                return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        assert post("/api/webull/connect")[0] == 400  # no keys saved yet
        code, body = post("/api/webull/disconnect")
        assert code == 200 and body["webull"]["token"] is None and not (tmp_path / "webull_token.json").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_settings_accept_webull_keys(tmp_path):
    from ctbot import secret_store

    f = tmp_path / "settings.json"
    secret_store.save("WEBULL_APP_KEY", KEY, f)
    secret_store.save("WEBULL_APP_SECRET", SECRET, f)
    assert set(secret_store.load(f)) == {"WEBULL_APP_KEY", "WEBULL_APP_SECRET"}
    with pytest.raises(ValueError):
        secret_store.save("WEBULL_APP_SECRET", "short", f)


def test_daily_candles_include_today_built_from_five_minute_bars(tmp_path):
    from ctbot.dashboard.candles import CandleSource

    class _RH:  # Robinhood holds back today's daily bar until it is final
        def intraday_bars(self, symbols, start, interval="5minute"):
            return {"QQQ": [Bar("QQQ", datetime(2026, 10, 15, tzinfo=timezone.utc), 720, 730, 719, 728, 9)]}

        def today_bars(self, symbols):
            t0 = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
            return {"QQQ": [Bar("QQQ", t0, 725, 726, 715, 716, 3), Bar("QQQ", t0 + timedelta(minutes=5), 716, 718, 708, 709.86, 4)]}

    now = lambda: datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)  # noqa: E731
    bars = CandleSource(_RH(), None, cache_root=tmp_path, clock=now).get("QQQ", "3m")["bars"]
    assert [(b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars] == [(720, 730, 719, 728, 9), (725, 726, 708, 709.86, 7)]
