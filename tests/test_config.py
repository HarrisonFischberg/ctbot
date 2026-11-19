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
