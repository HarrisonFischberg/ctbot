"""Robinhood live execution adapter. NOT IMPLEMENTED: submit() raises.

Constructing this adapter requires ALL of:
  - CTBOT_MODE=live
  - LIVE_TRADING_ENABLED=true
  - LIVE_TRADING_CONFIRM set to the exact phrase in ctbot.config.LIVE_CONFIRM_PHRASE
  - ROBINHOOD_ACCOUNT_NUMBER set and ROBINHOOD_ACCOUNT_NICKNAME set
  - not running inside a paper process (guard marker absent)

Every order must target self.account_number, and before trading the adapter must pass the
get_accounts result through verify_account(): the account must exist, carry ROBINHOOD_ACCOUNT_NICKNAME,
be an individual account, and be the one account open to this agent. Any other account
(default cash, IRAs, managed) is refused.

Planned wiring (to be built and reviewed before enabling):
  Robinhood order tools live on the Robinhood MCP server, which Claude can call.
  The adapter will call the Claude API with the MCP connector restricted to exactly
  ORDER_TOOLS below, always running review_equity_order before place_equity_order,
  and with Robinhood's own trade-approval setting left on so each order needs a tap.
"""

from __future__ import annotations

from ctbot.config import Settings
from ctbot.execution.isolation import in_paper_process
from ctbot.models import Fill, OrderRequest, Position

ORDER_TOOLS = frozenset({"review_equity_order", "place_equity_order", "cancel_equity_order"})

# Hard per-order ceiling independent of RiskLimits, as a last line of defense.
MAX_ORDER_NOTIONAL_USD = 60.0


class LiveTradingNotAuthorized(RuntimeError):
    pass


def verify_account(accounts: list[dict], account_number: str, nickname: str) -> dict:
    """Return the allowed account from a get_accounts payload, or raise.

    Matches the full account number, never the last 4 digits.
    """
    matches = [a for a in accounts if a.get("account_number") == account_number]
    if len(matches) != 1:
        raise LiveTradingNotAuthorized("configured account not found exactly once")
    acct = matches[0]
    problems = []
    if not nickname or acct.get("nickname") != nickname:
        problems.append(f"nickname is not {nickname!r}")
    if acct.get("brokerage_account_type") != "individual":
        problems.append("not an individual account")
    if acct.get("management_type") != "self_directed":
        problems.append("not self-directed")
    if acct.get("agentic_allowed") is not True:
        problems.append("not accessible to this agent")
    if acct.get("state") != "active" or acct.get("deactivated"):
        problems.append("not active")
    if problems:
        raise LiveTradingNotAuthorized("account refused: " + "; ".join(problems))
    return acct


class RobinhoodLiveExecutor:
    is_live = True

    def __init__(self, settings: Settings) -> None:
        if in_paper_process():
            raise LiveTradingNotAuthorized("live adapter constructed inside a paper process")
        if not settings.live_fully_authorized:
            raise LiveTradingNotAuthorized(
                "live trading requires CTBOT_MODE=live, LIVE_TRADING_ENABLED=true, "
                "LIVE_TRADING_CONFIRM set to the confirmation phrase, "
                "and ROBINHOOD_ACCOUNT_NUMBER set to the MCP account"
            )
        self.settings = settings
        self.account_number = settings.robinhood_account_number

    def submit(self, order: OrderRequest) -> Fill | None:
        if order.notional > MAX_ORDER_NOTIONAL_USD:
            raise LiveTradingNotAuthorized(
                f"order notional {order.notional} exceeds hard cap {MAX_ORDER_NOTIONAL_USD}"
            )
        raise NotImplementedError("live order submission is not implemented")

    def cancel(self, client_order_id: str) -> bool:
        raise NotImplementedError("live order cancel is not implemented")

    def positions(self) -> list[Position]:
        raise NotImplementedError

    def cash(self) -> float:
        raise NotImplementedError
