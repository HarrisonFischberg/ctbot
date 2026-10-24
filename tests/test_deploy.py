"""Server deployment files (deploy/): paper only, live pinned off, dashboard never exposed."""

import configparser
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string((DEPLOY / name).read_text())
    return cp


def test_bot_and_dashboard_units_run_paper_with_live_pinned_off_and_restart():
    for name, exe in [("ctbot-paper.service", "ctbot-paper --loop"), ("ctbot-dashboard.service", "ctbot-dashboard")]:
        svc = _unit(name)["Service"]
        assert exe in svc["ExecStart"] and svc["User"] == "ctbot"
        env = svc["Environment"].split()
        assert "LIVE_TRADING_ENABLED=false" in env and "CTBOT_MODE=paper" in env
        assert svc["Restart"] == "always"


def test_no_deploy_file_starts_live_or_exposes_the_dashboard():
    for path in DEPLOY.iterdir():
        text = path.read_text()
        assert "ctbot-live" not in text and "LIVE_TRADING_ENABLED=true" not in text
        assert "0.0.0.0" not in text and not re.search(r"--host\b", text) and "tailscale funnel " not in text
    assert "ufw allow 8787" not in (DEPLOY / "setup-server.sh").read_text()


def test_watchdog_timer_runs_the_watchdog_every_minute():
    assert "ctbot-watchdog" in _unit("ctbot-watchdog.service")["Service"]["ExecStart"]
    assert _unit("ctbot-watchdog.timer")["Timer"]["OnUnitActiveSec"] == "1min"


def test_setup_script_parses():
    subprocess.run(["bash", "-n", str(DEPLOY / "setup-server.sh")], check=True)


def test_update_timer_runs_on_weekdays_outside_market_hours_and_update_skips_an_open_market():
    text = (DEPLOY / "ctbot-update.timer").read_text()
    assert "OnCalendar=Mon..Fri 08:45 America/New_York" in text
    assert "OnCalendar=Mon..Fri 16:20 America/New_York" in text
    script = (DEPLOY / "update.sh").read_text()
    assert "market_hours.session_open" in script and "--ff-only" in script
    subprocess.run(["bash", "-n", str(DEPLOY / "update.sh")], check=True)
    setup = (DEPLOY / "setup-server.sh").read_text()
    assert "ctbot-update.timer" in setup
