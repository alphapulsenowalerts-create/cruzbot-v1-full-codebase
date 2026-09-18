"""Broker adapters: Alpaca, Coinbase Advanced Trade, Kraken, IB stub, Mock."""

from trading_bot.brokers.base import BrokerAdapter
from trading_bot.brokers.mock import MockBroker
from trading_bot.brokers.alpaca import AlpacaBroker
from trading_bot.brokers.coinbase import CoinbaseBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.ib_stub import IBBrokerStub

__all__ = [
    "BrokerAdapter",
    "MockBroker",
    "AlpacaBroker",
    "CoinbaseBroker",
    "KrakenBroker",
    "IBBrokerStub",
]
