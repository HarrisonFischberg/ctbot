import json
import os
import stat
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ctbot import secret_store
from ctbot.config import Settings
from ctbot.dashboard.server import make_handler

KEY = "sk-ant-api03-" + "x" * 40


def test_store_is_private_validated_and_removable(tmp_path):
    f = tmp_path / "rh" / "settings.json"
    secret_store.save("ANTHROPIC_API_KEY", KEY, f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert secret_store.load(f) == {"ANTHROPIC_API_KEY": KEY}
    with pytest.raises(ValueError):
        secret_store.save("LIVE_TRADING_ENABLED", "true", f)  # only the listed keys
    with pytest.raises(ValueError):
        secret_store.save("ROBINHOOD_ACCOUNT_NUMBER", "12 34\n", f)
    secret_store.save("ANTHROPIC_API_KEY", "", f)
    assert secret_store.load(f) == {}


def test_status_reports_source_never_values(tmp_path):
    f = tmp_path / "settings.json"
    secret_store.save("C2_API_KEY", "c2secretvalue", f)
    st = secret_store.status(f, env={"ANTHROPIC_API_KEY": KEY, "C2_API_KEY": "fromenv"})
    assert st["C2_API_KEY"]["source"] == "settings"
    assert st["ANTHROPIC_API_KEY"]["source"] == ".env"
    assert st["ROBINHOOD_ACCOUNT_NUMBER"] == {"label": "Robinhood account number", "set": False, "source": None}
    blob = json.dumps(st)
    assert KEY not in blob and "c2secretvalue" not in blob and "fromenv" not in blob


def test_settings_endpoints_need_the_token_and_never_echo_values(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        monkeypatch.setattr(secret_store, "STORE_PATH", d / "settings.json")
        monkeypatch.delenv("C2_API_KEY", raising=False)
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(d / "ledger.json"), "LOG_DIR": str(d / "logs"),
                               "MARKET_DATA": "csv"})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = make_handler(s, port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def post(body, token=None):
            req = urllib.request.Request(base + "/api/settings", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **({"X-Ctbot-Token": token} if token else {})})
            try:
                r = urllib.request.urlopen(req)
                return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        try:
            assert post({"key": "C2_API_KEY", "value": "c2secretvalue"})[0] == 403
            page = urllib.request.urlopen(base + "/").read().decode()
            token = page.split('name="ctbot-token" content="', 1)[1].split('"', 1)[0]
            code, body = post({"key": "C2_API_KEY", "value": "c2secretvalue"}, token)
            assert code == 200 and "c2secretvalue" not in body
            assert json.loads(body)["keys"]["C2_API_KEY"]["source"] == "settings"
            in_use = os.environ.get("C2_API_KEY") == "c2secretvalue"
            assert in_use  # the dashboard process uses it now (no value in failure output)
            assert post({"key": "CTBOT_MODE", "value": "live"}, token)[0] == 400
            got = urllib.request.urlopen(base + "/api/settings").read().decode()
            assert "c2secretvalue" not in got
        finally:
            httpd.shutdown()
            httpd.server_close()
            os.environ.pop("C2_API_KEY", None)


def test_running_process_picks_up_saved_keys_without_restart(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.delenv("C2_API_KEY", raising=False)
    monkeypatch.setattr(secret_store, "_seen", {"stamp": None, "keys": set()})
    monkeypatch.setattr("dotenv.find_dotenv", lambda **kw: str(tmp_path / "no.env"))  # never the real .env
    secret_store.apply_to_environ(f)              # process start: nothing saved yet
    assert not secret_store.reload_if_changed(f)  # nothing changed
    secret_store.save("C2_API_KEY", "c2secretvalue", f)
    changed, in_use = secret_store.reload_if_changed(f), os.environ.get("C2_API_KEY") == "c2secretvalue"
    assert changed and in_use  # booleans only: a failure must never print a key
    assert not secret_store.reload_if_changed(f)
    secret_store.save("C2_API_KEY", "", f)        # removed on the dashboard
    changed, gone = secret_store.reload_if_changed(f), "C2_API_KEY" not in os.environ
    assert changed and gone


def test_disconnect_deletes_only_the_saved_robinhood_sign_in(tmp_path, monkeypatch):
    from ctbot.market_data import robinhood_mcp_client as rmc

    token = tmp_path / "robinhood_oauth.json"
    token.write_text("{}")
    monkeypatch.setattr(rmc, "TOKEN_PATH", token)
    monkeypatch.setattr(secret_store, "STORE_PATH", tmp_path / "settings.json")
    s = Settings.from_env({"PAPER_LEDGER_PATH": str(tmp_path / "ledger.json"), "LOG_DIR": str(tmp_path / "logs"),
                           "MARKET_DATA": "csv"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(s, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        tok = page.split('name="ctbot-token" content="', 1)[1].split('"', 1)[0]

        def post(t):
            req = urllib.request.Request(base + "/api/robinhood/disconnect", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "X-Ctbot-Token": t})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code

        assert post("wrong") == 403 and token.exists()
        assert post(tok) == 200 and not token.exists()
        assert post(tok) == 200  # already gone: still fine
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_preflight_names_the_key_source_never_the_value(tmp_path, monkeypatch):
    from ctbot.preflight import _key_source

    store = tmp_path / "settings.json"
    monkeypatch.setattr(secret_store, "STORE_PATH", store)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _key_source("ANTHROPIC_API_KEY") == "nowhere: not set"
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test-from-dotenv-0000000000\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-from-dotenv-0000000000")
    assert _key_source("ANTHROPIC_API_KEY") == ".env"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-exported-in-shell-000000")
    assert _key_source("ANTHROPIC_API_KEY") == "the shell environment"
    secret_store.save("ANTHROPIC_API_KEY", "sk-test-saved-in-settings-00000", store)
    src = _key_source("ANTHROPIC_API_KEY")
    assert src == "the Settings tab" and "sk-" not in src
