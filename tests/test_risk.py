from datetime import date, datetime, timezone

from ctbot.config import RiskLimits
from ctbot.models import Candidate, Decision, Direction, Position, Quote, Side, Signal
from ctbot.risk.manager import AccountState, RiskManager

QUOTE = Quote("KO", 60.0, 60.02, 60.01)
ADV = 1e9


def _state(**kw):
    base = dict(equity=250, cash=250, peak_equity=250, day_start_equity=250, open_positions=0)
    return AccountState(**{**base, **kw})


def _ok_decision(notional=100.0):
    return Decision(approve=True, confidence=0.8, rationale="", suggested_notional=notional)


def _cand(direction=Direction.LONG):
    sig = Signal("momentum", "KO", direction, 0.8, 20)
    return Candidate("KO", direction, 0.4, (sig,))


def test_position_capped_at_max_pct():
    v = RiskManager(RiskLimits()).check_entry(_cand(), _ok_decision(), _state(), QUOTE, ADV)
    assert v.allowed
    assert v.orders[0].side is Side.BUY
    assert v.orders[0].notional == 50.0  # 20% of 250


def test_short_rejected_in_cash_account():
    v = RiskManager(RiskLimits()).check_entry(
        _cand(Direction.SHORT), _ok_decision(), _state(), QUOTE, ADV
    )
    assert not v.allowed
    assert "short_not_allowed_in_cash_account" in v.reasons


def test_daily_loss_and_kill_switch():
    rm = RiskManager(RiskLimits())
    assert not rm.check_entry(_cand(), _ok_decision(), _state(equity=240), QUOTE, ADV).allowed
    assert not rm.check_entry(_cand(), _ok_decision(), _state(kill_switch=True), QUOTE, ADV).allowed


def test_claude_rejection_wins():
    d = Decision(approve=False, confidence=0.9, rationale="", suggested_notional=50)
    assert not RiskManager(RiskLimits()).check_entry(_cand(), d, _state(), QUOTE, ADV).allowed


def test_already_held_rejected():
    v = RiskManager(RiskLimits()).check_entry(
        _cand(), _ok_decision(), _state(), QUOTE, ADV, frozenset({"KO"})
    )
    assert "already_held" in v.reasons


def test_exit_sells_full_position_and_respects_pdt_when_enabled():
    rm = RiskManager(RiskLimits(pdt_rule=True))
    old = Position("KO", 0.5, 55.0, datetime(2026, 10, 1, tzinfo=timezone.utc))
    v = rm.check_exit(_cand(Direction.EXIT), old, _state(), QUOTE, date(2026, 10, 13))
    assert v.allowed and v.orders[0].side is Side.SELL and v.orders[0].notional == 30.0

    same_day = Position("KO", 0.5, 55.0, datetime(2026, 10, 13, 15, tzinfo=timezone.utc))
    busy = _state(day_trade_dates=[date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 12)])
    v = rm.check_exit(_cand(Direction.EXIT), same_day, busy, QUOTE, date(2026, 10, 13))
    assert "pdt_guard" in v.reasons


def test_same_day_exit_not_limited_by_day_trade_count_by_default():
    """FINRA's intraday margin standard replaced the PDT cap on 2026-06-04."""
    rm = RiskManager(RiskLimits())
    same_day = Position("KO", 0.5, 55.0, datetime(2026, 10, 13, 15, tzinfo=timezone.utc))
    busy = _state(day_trade_dates=[date(2026, 10, 9), date(2026, 10, 12)] + [date(2026, 10, 13)] * 5)
    v = rm.check_exit(_cand(Direction.EXIT), same_day, busy, QUOTE, date(2026, 10, 13))
    assert v.allowed and v.orders[0].side is Side.SELL
    assert not rm.pdt_limit_hit(busy, date(2026, 10, 13))


def test_entry_never_spends_more_than_cash():
    """Buying power is cash only: no margin borrowing, whatever the size cap allows."""
    v = RiskManager(RiskLimits()).check_entry(_cand(), _ok_decision(), _state(cash=12.0), QUOTE, ADV)
    assert v.allowed and v.orders[0].notional == 12.0
    v = RiskManager(RiskLimits()).check_entry(_cand(), _ok_decision(), _state(cash=0.5), QUOTE, ADV)
    assert not v.allowed


def test_refresh_rolls_day_tracks_peak_and_latches_kill_switch():
    rm = RiskManager(RiskLimits())
    s = _state(equity=260, day_start_equity=250, day_start_date=date(2026, 10, 12))
    rm.refresh(s, date(2026, 10, 13))
    assert (s.peak_equity, s.day_start_equity, s.day_start_date) == (260, 260, date(2026, 10, 13))

    s.equity = 255
    rm.refresh(s, date(2026, 10, 13))  # same day: day start unchanged
    assert s.day_start_equity == 260 and not s.kill_switch

    s.equity = 220  # -15.4% from peak 260
    rm.refresh(s, date(2026, 10, 13))
    assert s.kill_switch
    s.equity = 300
    rm.refresh(s, date(2026, 10, 14))
    assert s.kill_switch  # stays latched after recovery


def test_record_exit_fill_counts_only_same_day_round_trips():
    rm = RiskManager(RiskLimits())
    s = _state()
    today = date(2026, 10, 13)
    rm.record_exit_fill(s, Position("KO", 1, 60, datetime(2026, 10, 1, tzinfo=timezone.utc)), today)
    assert s.day_trade_dates == []
    opened = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    rm.record_exit_fill(s, Position("KO", 1, 60, opened), today)
    assert s.day_trade_dates == [today]


def test_account_state_round_trips_and_kill_switch_survives_restart():
    import tempfile
    from pathlib import Path

    from ctbot.risk.manager import load_account_state, save_account_state

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "risk.json"
        s = _state(
            peak_equity=300, kill_switch=True, day_start_date=date(2026, 10, 13),
            day_trade_dates=[date(2026, 10, 12)],
        )
        save_account_state(s, path)
        fresh = load_account_state(_state(), path)
    assert fresh.peak_equity == 300 and fresh.kill_switch
    assert fresh.day_start_date == date(2026, 10, 13)
    assert fresh.day_trade_dates == [date(2026, 10, 12)]


def test_strategy_overrides_only_loosen_liquidity_for_their_own_longs():
    cheap = Quote("XYZ", 2.0, 2.01, 2.0)
    adv = 2_000_000
    rm = RiskManager(
        RiskLimits(),
        {"copy_trading": {"min_share_price": 1.0, "min_avg_dollar_volume": 1_000_000},
         "strict": {"min_share_price": 50.0}},
    )

    def cand(*names):
        sigs = tuple(Signal(n, "XYZ", Direction.LONG, 0.8, 20) for n in names)
        return Candidate("XYZ", Direction.LONG, 0.4, sigs)

    assert rm.check_entry(cand("copy_trading"), _ok_decision(), _state(), cheap, adv).allowed
    v = rm.check_entry(cand("momentum"), _ok_decision(), _state(), cheap, adv)
    assert {"price_below_min", "illiquid"} <= set(v.reasons)
    assert rm.liquidity_limits(cand("strict")) == (5.0, 20_000_000)  # cannot tighten
    assert rm.check_entry(cand("copy_trading"), _ok_decision(), _state(), cheap, 500_000).reasons \
        == ("illiquid",)


def test_wide_spread_and_bad_quotes_rejected():
    rm = RiskManager(RiskLimits(max_spread_pct=0.01))
    wide = Quote("KO", 59.0, 60.0, 59.5)  # ~1.7%
    v = rm.check_entry(_cand(), _ok_decision(), _state(), wide, ADV)
    assert v.reasons == ("spread_too_wide",)
    crossed = Quote("KO", 60.1, 60.0, 60.0)
    assert "bad_quote" in rm.check_entry(_cand(), _ok_decision(), _state(), crossed, ADV).reasons


def test_bracket_uses_capped_claude_stop_and_optional_target():
    rm = RiskManager(RiskLimits(default_stop_loss_pct=0.05, max_stop_loss_pct=0.10))
    s = _state()
    wide = Decision(True, 0.8, "", 50, stop_loss_pct=0.30, take_profit_pct=0.08)
    rm.set_bracket(s, "KO", 100.0, wide)
    assert s.brackets["KO"] == {"stop": 90.0, "target": 108.0}
    pos = Position("KO", 1, 100.0)
    assert rm.bracket_hit(s, pos, Quote("KO", 95, 95.1, 95)) is None
    assert rm.bracket_hit(s, pos, Quote("KO", 89.9, 90, 90)) == "stop_loss"
    assert rm.bracket_hit(s, pos, Quote("KO", 108.5, 108.6, 108.5)) == "take_profit"


def test_trading_capital_cap_limits_total_deployment():
    rm = RiskManager(RiskLimits(max_trading_capital=250))
    # $1,000 equity after a deposit: still sized off $250 -> $50.
    v = rm.check_entry(_cand(), _ok_decision(), _state(equity=1000, cash=1000, peak_equity=1000,
                       day_start_equity=1000), QUOTE, ADV)
    assert v.orders[0].notional == 50.0
    # $230 already invested: only $20 of room left under the cap.
    s = _state(equity=1000, cash=770, peak_equity=1000, day_start_equity=1000)
    assert rm.check_entry(_cand(), _ok_decision(), s, QUOTE, ADV).orders[0].notional == 20.0
    s = _state(equity=1000, cash=740, peak_equity=1000, day_start_equity=1000)
    assert rm.check_entry(_cand(), _ok_decision(), s, QUOTE, ADV).reasons == ("trading_capital_cap",)


def test_size_by_risk_ignores_claude_suggestion():
    big = _state(equity=5000, cash=5000, peak_equity=5000, day_start_equity=5000)
    d = Decision(approve=True, confidence=0.8, rationale="", suggested_notional=40.0)
    by_claude = RiskManager(RiskLimits(max_trading_capital=5000, max_position_pct=0.25))
    by_risk = RiskManager(RiskLimits(max_trading_capital=5000, max_position_pct=0.25, size_by="risk"))
    assert by_claude.check_entry(_cand(), d, big, QUOTE, ADV).orders[0].notional == 40.0
    assert by_risk.check_entry(_cand(), d, big, QUOTE, ADV).orders[0].notional == 1250.0


def test_entry_limit_leaves_room_for_the_price_to_move():
    from ctbot.config import Settings
    from ctbot.models import Candidate, Decision, Direction, Quote, Signal
    from ctbot.risk.manager import AccountState, RiskManager

    s = Settings.from_env({"MAX_TRADING_CAPITAL": "5000", "ENTRY_LIMIT_SLACK_PCT": "0.002"})
    cand = Candidate("KO", Direction.LONG, 1.0, (Signal("orb", "KO", Direction.LONG, 1.0, 0.2),))
    v = RiskManager(s.risk).check_entry(cand, Decision(True, 0.9, "ok", 100.0), AccountState(5000, 5000, 5000, 5000, 0),
                                        Quote("KO", 99.99, 100.0, 100.0), 1e9)
    assert v.allowed and v.orders[0].limit_price == 100.2  # 0.2% above the ask it saw


def test_entry_sizes_off_margin_buying_power_and_stops_on_margin_call():
    rm = RiskManager(RiskLimits(max_trading_capital=20_000, max_position_pct=0.3, size_by="risk"))
    # $10k equity, all invested in cash terms (-$2k cash = $2k borrowed), $8k loan room left
    st = _state(equity=10_000, cash=-2_000, peak_equity=10_000, day_start_equity=10_000, buying_power=8_000)
    v = rm.check_entry(_cand(), _ok_decision(), st, QUOTE, ADV)
    assert v.allowed and v.orders[0].notional == 3_000.0  # 30% of $10k, paid with the loan
    st.margin_call = True
    assert "margin_maintenance" in rm.check_entry(_cand(), _ok_decision(), st, QUOTE, ADV).reasons


def test_account_state_save_is_atomic_and_round_trips(tmp_path):
    from ctbot.risk.manager import AccountState, load_account_state, save_account_state

    path = tmp_path / "risk.json"
    s = AccountState(equity=1000, cash=1000, peak_equity=1200, day_start_equity=1000, open_positions=0,
                     kill_switch=True)
    save_account_state(s, path)
    assert not path.with_suffix(".tmp").exists()
    back = load_account_state(AccountState(1000, 1000, 1000, 1000, 0), path)
    assert back.kill_switch and back.peak_equity == 1200
