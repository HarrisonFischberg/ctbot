"""Live entrypoint (`ctbot-live`). Separate process from paper. Currently refuses to trade."""

from __future__ import annotations

import sys

from ctbot.config import Settings
from ctbot.live.robinhood_adapter import LiveTradingNotAuthorized, RobinhoodLiveExecutor


def main() -> None:
    settings = Settings.from_env()
    try:
        RobinhoodLiveExecutor(settings)
    except LiveTradingNotAuthorized as e:
        sys.exit(f"Live trading disabled: {e}")
    sys.exit("Live execution is not implemented yet. Use ctbot-paper.")


if __name__ == "__main__":
    main()
