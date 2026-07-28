from __future__ import annotations


class AuriferousError(Exception):
    pass


class ConfigurationError(AuriferousError):
    pass


class BrokerError(AuriferousError):
    pass


class BrokerConnectionError(BrokerError):
    pass


class ContractResolutionError(BrokerError):
    pass


class LiquidityRejectedError(BrokerError):
    pass


class OrderError(BrokerError):
    pass


class MarginError(BrokerError):
    pass
