from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from ..models import OptionContract, Side, OrderType, Order


class BrokerError(Exception):
    pass


class Broker(ABC):
    is_paper = False

    @abstractmethod
    def place_order(self, contract: OptionContract, side: Side, qty: int, order_type: OrderType,
                    price: float = 0.0, trigger: Optional[float] = None, tag: str = "") -> str: ...
    @abstractmethod
    def modify_order(self, order_id: str, price: Optional[float] = None, trigger: Optional[float] = None): ...
    @abstractmethod
    def cancel_order(self, order_id: str): ...
    @abstractmethod
    def get_order(self, order_id: str) -> Order: ...
    @abstractmethod
    def positions(self) -> dict[str, int]: ...

    def process(self, now: datetime):
        """Called every engine step (paper broker uses it to match orders)."""
