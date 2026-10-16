"""Paper/live isolation: static import rules plus runtime guard behavior."""

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ctbot"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_module_outside_live_imports_live():
    for py in SRC.rglob("*.py"):
        if "live" in py.relative_to(SRC).parts:
            continue
        bad = {m for m in _imports(py) if m.startswith("ctbot.live")}
        assert not bad, f"{py} imports {bad}"


def test_order_tool_names_only_in_live_package():
    for py in SRC.rglob("*.py"):
        if "live" in py.relative_to(SRC).parts:
            continue
        text = py.read_text()
        for tool in ("place_equity_order", "place_option_order", "place_crypto_order"):
            assert tool not in text, f"{py} references {tool}"


def _run(code: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(SRC.parent), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def test_guard_blocks_live_import_in_fresh_process():
    r = _run(
        "from ctbot.execution.isolation import install_paper_guard\n"
        "install_paper_guard()\n"
        "import ctbot.live.robinhood_adapter\n"
    )
    assert r.returncode != 0
    assert "cannot be imported in a paper-trading process" in r.stderr


def test_live_adapter_refuses_by_default():
    r = _run(
        "from ctbot.config import Settings\n"
        "from ctbot.live.robinhood_adapter import RobinhoodLiveExecutor\n"
        "RobinhoodLiveExecutor(Settings.from_env({}))\n"
    )
    assert r.returncode != 0
    assert "LiveTradingNotAuthorized" in r.stderr


def test_verify_account_only_accepts_the_configured_account():
    r = _run(
        "from ctbot.live.robinhood_adapter import verify_account, LiveTradingNotAuthorized\n"
        "ok = dict(account_number='900009999', nickname='Bot', brokerage_account_type='individual',"
        " management_type='self_directed', agentic_allowed=True, state='active', deactivated=False)\n"
        "roth = dict(ok, account_number='100009999', nickname=None,"
        " brokerage_account_type='ira_roth', agentic_allowed=False)\n"
        "accts = [roth, ok]\n"
        "assert verify_account(accts, '900009999', 'Bot') is ok\n"
        "for num in ('100009999', '9999', ''):\n"
        "    try:\n"
        "        verify_account(accts, num, 'Bot')\n"
        "    except LiveTradingNotAuthorized:\n"
        "        continue\n"
        "    raise SystemExit('accepted ' + num)\n"
        "renamed = [dict(ok, nickname='Other')]\n"
        "try:\n"
        "    verify_account(renamed, '900009999', 'Bot')\n"
        "    raise SystemExit('accepted renamed')\n"
        "except LiveTradingNotAuthorized:\n"
        "    pass\n"
    )
    assert r.returncode == 0, r.stderr
