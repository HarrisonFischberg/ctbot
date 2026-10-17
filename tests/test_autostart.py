"""launchd autostart agent: valid plist, runs the paper launcher, keeps it alive, live stays off."""

import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plist_template_runs_the_paper_launcher_and_restarts_it():
    text = (ROOT / "scripts/com.ctbot.bot.plist.example").read_text().replace("/path/to/ctbot", "/Users/me/ctbot")
    p = plistlib.loads(text.encode())
    assert p["Label"] == "com.ctbot.bot"
    assert p["ProgramArguments"] == ["/bin/bash", "/Users/me/ctbot/run_ctbot.sh"]
    assert p["WorkingDirectory"] == "/Users/me/ctbot"
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True and p["ThrottleInterval"] >= 10
    env = p["EnvironmentVariables"]
    assert env["LIVE_TRADING_ENABLED"] == "false" and env["CTBOT_NO_BROWSER"] == "1"
    assert p["StandardOutPath"] == p["StandardErrorPath"] == "/Users/me/ctbot/logs/bot.log"


def test_autostart_script_parses():
    subprocess.run(["bash", "-n", str(ROOT / "scripts/autostart.sh")], check=True)
