"""Single import surface for the T-Invest Python SDK.

The official package is ``t-tech-investments`` (``t_tech.invest``). The legacy
``tinkoff-investments`` package (``tinkoff.invest``) exposes the same API and is
kept as a drop-in fallback, so the server runs in environments where only one
of the two is installable. Import everything SDK-related from
**this** module, never from ``t_tech.invest`` directly, so the fallback stays in
one place.
"""

from __future__ import annotations

try:  # pragma: no cover - exercised by whichever package is installed
    from t_tech.invest import (  # type: ignore
        CandleInterval,
        Client,
        InstrumentIdType,
        InstrumentType,
        MoneyValue,
        OrderDirection,
        OrderType,
        Quotation,
        RequestError,
    )
    from t_tech.invest.constants import (  # type: ignore
        INVEST_GRPC_API,
        INVEST_GRPC_API_SANDBOX,
    )
    from t_tech.invest.schemas import (  # type: ignore
        EventType,
        GetAssetFundamentalsRequest,
        GetBondEventsRequest,
        GetConsensusForecastsRequest,
        GetForecastRequest,
        GetMaxLotsRequest,
        GetOperationsByCursorRequest,
        GetOrderPriceRequest,
        InstrumentStatus,
        OperationState,
        OperationType,
        OrderExecutionReportStatus,
        Page,
    )

    SDK_PACKAGE = "t_tech.invest"
except ImportError:  # pragma: no cover - fallback path
    from tinkoff.invest import (  # type: ignore
        CandleInterval,
        Client,
        InstrumentIdType,
        InstrumentType,
        MoneyValue,
        OrderDirection,
        OrderType,
        Quotation,
        RequestError,
    )
    from tinkoff.invest.constants import (  # type: ignore
        INVEST_GRPC_API,
        INVEST_GRPC_API_SANDBOX,
    )
    from tinkoff.invest.schemas import (  # type: ignore
        EventType,
        GetAssetFundamentalsRequest,
        GetBondEventsRequest,
        GetConsensusForecastsRequest,
        GetForecastRequest,
        GetMaxLotsRequest,
        GetOperationsByCursorRequest,
        GetOrderPriceRequest,
        InstrumentStatus,
        OperationState,
        OperationType,
        OrderExecutionReportStatus,
        Page,
    )

    SDK_PACKAGE = "tinkoff.invest"


__all__ = [
    "CandleInterval",
    "Client",
    "InstrumentIdType",
    "InstrumentType",
    "InstrumentStatus",
    "MoneyValue",
    "OrderDirection",
    "OrderType",
    "Quotation",
    "RequestError",
    "INVEST_GRPC_API",
    "INVEST_GRPC_API_SANDBOX",
    "GetMaxLotsRequest",
    "GetOperationsByCursorRequest",
    "GetOrderPriceRequest",
    "OperationState",
    "OperationType",
    "GetForecastRequest",
    "GetConsensusForecastsRequest",
    "GetAssetFundamentalsRequest",
    "GetBondEventsRequest",
    "EventType",
    "Page",
    "OrderExecutionReportStatus",
    "SDK_PACKAGE",
]
