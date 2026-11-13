from ctbot.config import LIVE_CONFIRM_PHRASE, Mode, Settings


def test_defaults_are_paper_and_live_disabled():
    s = Settings.from_env({})
    assert s.mode is Mode.PAPER
    assert s.live_trading_enabled is False
    assert not s.live_fully_authorized


def test_live_needs_all_three_flags():
    base = {"CTBOT_MODE": "live", "LIVE_TRADING_ENABLED": "true"}
    assert not Settings.from_env(base).live_fully_authorized
    assert not Settings.from_env({**base, "LIVE_TRADING_ENABLED": "1"}).live_fully_authorized
    full = {**base, "LIVE_TRADING_CONFIRM": LIVE_CONFIRM_PHRASE, "ROBINHOOD_ACCOUNT_NUMBER": "111",
            "ROBINHOOD_ACCOUNT_NICKNAME": "Bot"}
    assert Settings.from_env(full).live_fully_authorized


def test_live_also_needs_the_pinned_account():
    full = {"CTBOT_MODE": "live", "LIVE_TRADING_ENABLED": "true",
            "LIVE_TRADING_CONFIRM": LIVE_CONFIRM_PHRASE, "ROBINHOOD_ACCOUNT_NICKNAME": "Bot"}
    assert not Settings.from_env({**full, "ROBINHOOD_ACCOUNT_NUMBER": ""}).live_fully_authorized
    assert not Settings.from_env({**full, "ROBINHOOD_ACCOUNT_NUMBER": "111", "ROBINHOOD_ACCOUNT_NICKNAME": ""}).live_fully_authorized
    s = Settings.from_env({**full, "ROBINHOOD_ACCOUNT_NUMBER": "111"})
    assert s.live_fully_authorized
    assert "111" not in repr(s)  # account number kept out of logs


def test_pct_settings_outside_0_to_1_refuse_to_start():
    """B5: MAX_DAILY_LOSS_PCT=3 (meaning 3%) would turn the daily halt off; refuse it, by name."""
    from pathlib import Path

    import pytest

    from ctbot import autopilot, variants

    for key in ("MAX_DAILY_LOSS_PCT", "MAX_TOTAL_DRAWDOWN_PCT", "MAX_POSITION_PCT", "MAX_STOP_LOSS_PCT"):
        for bad in ("3", "15", "-0.01", "nan"):
            with pytest.raises(ValueError, match=key):
                Settings.from_env({key: bad})
    assert Settings.from_env({"MAX_DAILY_LOSS_PCT": "0", "MAX_POSITION_PCT": "1"}).risk.max_position_pct == 1.0
    # a side-by-side book's env override is checked the same way
    book = variants.Variant("loose", "loose", autopilot.validate({"enabled": True}), {"MAX_DAILY_LOSS_PCT": "3"})
    with pytest.raises(ValueError, match="MAX_DAILY_LOSS_PCT"):
        Settings.from_env(variants.book_env({}, Path("data/paper_ledger.json"), book))
