# broker_adapter.py
"""
Broker adapter — abstract interface + Moomoo stub.

Design
------
The agent currently runs in NOTIFICATION-ONLY mode (v3.1). When you're
ready to go live (v4), you fill in MoomooAdapter's methods using
moomoo OpenAPI (https://openapi.moomoo.com).

Until then, calling any method raises NotImplementedError. The interface
is fully defined so the rest of the system can be coded against it
without uncertainty about the contract.

To enable live execution later:
    1. pip install moomoo-api
    2. Set MOOMOO_HOST, MOOMOO_PORT env vars
    3. Implement the TODO methods below
    4. In live_trigger.py, set `broker_mode = "EXECUTE"`
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


# --------------------------------------------------------------------- #
# Domain types
# --------------------------------------------------------------------- #

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
OrderStatus = Literal["PENDING", "SUBMITTED", "FILLED", "PARTIAL",
                       "CANCELLED", "REJECTED", "ERROR"]


@dataclass
class OrderRequest:
    """A broker-agnostic order intent."""
    ticker: str               # e.g. "0166.KL"
    side: OrderSide
    quantity: int
    order_type: OrderType = "MARKET"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str | None = None    # for idempotency


@dataclass
class OrderResponse:
    broker_order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    error: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_cost: float
    current_price: float
    unrealized_pnl: float


# --------------------------------------------------------------------- #
# Abstract base
# --------------------------------------------------------------------- #

class BrokerAdapter(ABC):
    """Every concrete broker integration MUST implement these."""

    name: str = "abstract"

    @abstractmethod
    def connect(self) -> bool:
        """Establish session. Returns True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResponse:
        """Submit a single order."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> OrderResponse:
        ...

    @abstractmethod
    def list_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def get_cash_balance(self) -> float:
        ...


# --------------------------------------------------------------------- #
# Moomoo stub — fill in when going live
# --------------------------------------------------------------------- #

class MoomooAdapter(BrokerAdapter):
    """
    Moomoo OpenAPI adapter — STUB.

    Replace each NotImplementedError with real moomoo calls when ready.
    Reference: https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html

    Typical implementation skeleton:

        from moomoo import OpenSecTradeContext, TrdMarket, TrdEnv

        def connect(self):
            self.ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.MY,
                host=self._host, port=self._port,
            )
            ret, _ = self.ctx.unlock_trade(password=self._unlock_pwd)
            return ret == 0
    """
    name = "moomoo"

    def __init__(self, host: str = "127.0.0.1", port: int = 11111,
                 trd_env: str = "SIMULATE", unlock_pwd: str | None = None):
        self._host = host
        self._port = port
        self._env = trd_env
        self._unlock_pwd = unlock_pwd
        self._connected = False

    def connect(self) -> bool:
        raise NotImplementedError(
            "MoomooAdapter.connect() — fill in with OpenSecTradeContext init "
            "when ready to go live.")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def place_order(self, req: OrderRequest) -> OrderResponse:
        raise NotImplementedError(
            "MoomooAdapter.place_order() — wire moomoo place_order API.")

    def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError(
            "MoomooAdapter.cancel_order() — wire moomoo cancel API.")

    def get_order(self, broker_order_id: str) -> OrderResponse:
        raise NotImplementedError(
            "MoomooAdapter.get_order() — wire moomoo order_list_query API.")

    def list_positions(self) -> list[Position]:
        raise NotImplementedError(
            "MoomooAdapter.list_positions() — wire moomoo position_list_query.")

    def get_cash_balance(self) -> float:
        raise NotImplementedError(
            "MoomooAdapter.get_cash_balance() — wire moomoo accinfo_query.")


# --------------------------------------------------------------------- #
# No-op adapter — used in notification-only mode
# --------------------------------------------------------------------- #

class NoopAdapter(BrokerAdapter):
    """Safe default: pretends to be connected, does nothing."""
    name = "noop"

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def place_order(self, req: OrderRequest) -> OrderResponse:
        return OrderResponse(
            broker_order_id="noop-0", status="REJECTED",
            error="NoopAdapter — notification-only mode")

    def cancel_order(self, broker_order_id: str) -> bool:
        return False

    def get_order(self, broker_order_id: str) -> OrderResponse:
        return OrderResponse(broker_order_id=broker_order_id,
                              status="ERROR", error="noop")

    def list_positions(self) -> list[Position]:
        return []

    def get_cash_balance(self) -> float:
        return 0.0


def get_broker_adapter(mode: str = "NOOP") -> BrokerAdapter:
    """Factory. mode in {NOOP, MOOMOO}."""
    mode = (mode or "NOOP").upper()
    if mode == "MOOMOO":
        return MoomooAdapter()
    return NoopAdapter()
