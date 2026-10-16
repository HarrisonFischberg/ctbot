"""Common executor interface implemented by the paper simulator and the live adapter."""

from __future__ import annotations

from typing import Protocol

from ctbot.models import Fill, OrderRequest, Position


class OrderExecutor(Protocol):
    is_live: bool

    def submit(self, order: OrderRequest) -> Fill | None:
        ...

    def cancel(self, client_order_id: str) -> bool:
        ...

    def positions(self) -> list[Position]:
        ...

    def cash(self) -> float:
        ...
