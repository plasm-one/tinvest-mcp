"""Thin, mode-aware wrapper over the T-Invest SDK.

One adapter exposes a uniform API regardless of ``sandbox`` vs ``prod`` mode; it
dispatches each call to the right service namespace (``client.sandbox.*`` for
sandbox accounts/portfolio/orders, ``client.operations/orders/users.*`` for
prod) and uses the right gRPC endpoint. A fresh sync ``Client`` is opened per
call (matches the other MCP servers in this repo). Tokens are read from
:class:`Settings` and never logged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from .config.env import Settings
from .errors import TInvestConfigurationError
from .money import decimal_to_quotation_parts
from .sdk import (
    INVEST_GRPC_API,
    INVEST_GRPC_API_SANDBOX,
    CandleInterval,
    Client,
    GetAssetFundamentalsRequest,
    GetBondEventsRequest,
    GetConsensusForecastsRequest,
    GetForecastRequest,
    GetMaxLotsRequest,
    GetOperationsByCursorRequest,
    GetOrderPriceRequest,
    InstrumentIdType,
    InstrumentStatus,
    InstrumentType,
    MoneyValue,
    OperationState,
    OperationType,
    OrderDirection,
    OrderType,
    Page,
    Quotation,
)
from .tls import ensure_grpc_roots

# Make grpc trust the Russian national CA before any channel is created.
ensure_grpc_roots()

_DIRECTION = {
    "BUY": OrderDirection.ORDER_DIRECTION_BUY,
    "SELL": OrderDirection.ORDER_DIRECTION_SELL,
}
_ORDER_TYPE = {
    "LIMIT": OrderType.ORDER_TYPE_LIMIT,
    "MARKET": OrderType.ORDER_TYPE_MARKET,
}
_FIND_KIND = {
    "share": InstrumentType.INSTRUMENT_TYPE_SHARE,
    "bond": InstrumentType.INSTRUMENT_TYPE_BOND,
    "etf": InstrumentType.INSTRUMENT_TYPE_ETF,
    "currency": InstrumentType.INSTRUMENT_TYPE_CURRENCY,
}


def to_quotation(value: Decimal) -> Quotation:
    units, nano = decimal_to_quotation_parts(value)
    return Quotation(units=units, nano=nano)


def to_money(value: Decimal, currency: str = "rub") -> MoneyValue:
    units, nano = decimal_to_quotation_parts(value)
    return MoneyValue(currency=currency, units=units, nano=nano)


class TInvestAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- low-level client context -------------------------------------------------
    @property
    def _target(self) -> str:
        return INVEST_GRPC_API_SANDBOX if self.settings.is_sandbox else INVEST_GRPC_API

    @contextmanager
    def _client(self, *, trade: bool = False) -> Iterator:
        token = self.settings.active_trade_token() if trade else self.settings.active_read_token()
        if not token:
            which = "trade" if trade else "read"
            raise TInvestConfigurationError(
                f"No {which} token configured for mode '{self.settings.mode}'. "
                "Set the appropriate TINVEST_*_TOKEN environment variable."
            )
        with Client(token, target=self._target) as client:
            yield client

    # -- accounts ----------------------------------------------------------------
    def get_accounts(self) -> list:
        with self._client() as client:
            if self.settings.is_sandbox:
                return list(client.sandbox.get_sandbox_accounts().accounts)
            return list(client.users.get_accounts().accounts)

    def get_trade_accounts(self) -> list:
        """List accounts through the execution token without performing a mutation.

        Normal reads deliberately use :meth:`get_accounts` and the least-privileged
        research token.  This method is only a capability probe that verifies the
        separately configured trade token sees the same account with full access.
        """
        with self._client(trade=True) as client:
            if self.settings.is_sandbox:
                return list(client.sandbox.get_sandbox_accounts().accounts)
            return list(client.users.get_accounts().accounts)

    # -- portfolio / positions ---------------------------------------------------
    def get_portfolio(self, account_id: str):
        with self._client() as client:
            if self.settings.is_sandbox:
                return client.sandbox.get_sandbox_portfolio(account_id=account_id)
            return client.operations.get_portfolio(account_id=account_id)

    def get_positions(self, account_id: str):
        with self._client() as client:
            if self.settings.is_sandbox:
                return client.sandbox.get_sandbox_positions(account_id=account_id)
            return client.operations.get_positions(account_id=account_id)

    def get_operations_by_cursor(
        self,
        account_id: str,
        *,
        from_: datetime,
        to: datetime,
        cursor: str = "",
        limit: int = 100,
        operation_types: list[OperationType] | None = None,
        state: OperationState | None = None,
        instrument_id: str | None = None,
        without_overnights: bool = False,
    ):
        request = GetOperationsByCursorRequest(
            account_id=account_id,
            from_=from_,
            to=to,
            cursor=cursor or "",
            limit=max(3, min(int(limit), 1000)),
            without_commissions=False,
            without_trades=False,
            without_overnights=without_overnights,
        )
        if instrument_id:
            request.instrument_id = instrument_id
        if operation_types:
            request.operation_types.extend(operation_types)
        if state is not None:
            request.state = state
        with self._client() as client:
            if self.settings.is_sandbox:
                return client.sandbox.get_sandbox_operations_by_cursor(request=request)
            return client.operations.get_operations_by_cursor(request=request)

    # -- instruments (work on both endpoints) ------------------------------------
    def find_instrument(
        self,
        query: str,
        *,
        instrument_type: str | None = None,
        api_trade_available: bool | None = None,
    ) -> list:
        kind = _FIND_KIND.get((instrument_type or "").lower())
        with self._client() as client:
            resp = client.instruments.find_instrument(
                query=query,
                instrument_kind=kind,
                api_trade_available_flag=api_trade_available,
            )
            return list(resp.instruments)

    def list_instruments(self, instrument_type: str) -> list:
        """Return the FULL tradeable catalogue for a type (share|bond|etf)."""
        kind = (instrument_type or "").lower()
        with self._client() as client:
            if kind == "share":
                return list(
                    client.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
                )
            if kind == "bond":
                return list(
                    client.instruments.bonds(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
                )
            if kind == "etf":
                return list(
                    client.instruments.etfs(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
                )
            raise TInvestConfigurationError(f"Unsupported instrument_type for listing: {instrument_type!r}")

    def list_currencies(self) -> list:
        """FX instruments (CNYRUB_TOM & co) — the source of ruble conversion rates."""
        with self._client() as client:
            return list(
                client.instruments.currencies(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
            )

    def get_bond_coupons(self, uid: str, from_, to) -> list:
        with self._client() as client:
            return list(client.instruments.get_bond_coupons(instrument_id=uid, from_=from_, to=to).events)

    def get_bond_events(self, uid: str, from_=None, to=None, event_type=None) -> list:
        """Bond lifecycle events: coupons (CPN), calls/offers (CALL), maturity (MTY)."""
        kwargs = {"instrument_id": uid}
        if from_ is not None:
            kwargs["from_"] = from_
        if to is not None:
            kwargs["to"] = to
        if event_type is not None:
            kwargs["type"] = event_type
        with self._client() as client:
            return list(client.instruments.get_bond_events(request=GetBondEventsRequest(**kwargs)).events)

    def get_dividends(self, uid: str, from_, to) -> list:
        with self._client() as client:
            return list(client.instruments.get_dividends(instrument_id=uid, from_=from_, to=to).dividends)

    def get_daily_candles(self, uid: str, from_, to) -> list:
        with self._client() as client:
            return list(
                client.market_data.get_candles(
                    instrument_id=uid,
                    from_=from_,
                    to=to,
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                ).candles
            )

    # -- analyst forecasts / ratings ---------------------------------------------
    def get_forecast(self, uid: str):
        """Analyst forecast for ONE instrument: consensus + per-analyst targets."""
        with self._client() as client:
            return client.instruments.get_forecast_by(request=GetForecastRequest(instrument_id=uid))

    def get_consensus_forecasts(self, *, page_limit: int = 100, max_pages: int = 60) -> list:
        """Bulk consensus forecasts for the whole catalogue (paged → one few-call sweep).

        Cheaper than one ``get_forecast`` per instrument, so it is the source for
        the ``list_shares`` consensus columns. Each item carries the ASSET uid
        (join key), consensus recommendation, target band and analyst counts.
        """
        items: list = []
        with self._client() as client:
            page_number = 0
            while page_number < max_pages:
                resp = client.instruments.get_consensus_forecasts(
                    request=GetConsensusForecastsRequest(paging=Page(limit=page_limit, page_number=page_number))
                )
                batch = list(getattr(resp, "items", []) or [])
                items.extend(batch)
                page = getattr(resp, "page", None)
                total = int(getattr(page, "total_count", 0) or 0)
                if not batch or (total and len(items) >= total) or len(batch) < page_limit:
                    break
                page_number += 1
        return items

    def get_asset_fundamentals(self, asset_uids: list[str], *, chunk: int = 100) -> list:
        """Financial statement metrics & ratios for the given ASSET uids.

        The API accepts a batch of asset uids per call (capped server-side), so
        we chunk. Returns ``StatisticResponse`` items keyed by ``asset_uid``.
        """
        asset_uids = [u for u in asset_uids if u]
        if not asset_uids:
            return []
        out: list = []
        with self._client() as client:
            for i in range(0, len(asset_uids), chunk):
                batch = asset_uids[i : i + chunk]
                resp = client.instruments.get_asset_fundamentals(request=GetAssetFundamentalsRequest(assets=batch))
                out.extend(list(getattr(resp, "fundamentals", []) or []))
        return out

    def get_instrument_by_uid(self, uid: str):
        with self._client() as client:
            return client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid
            ).instrument

    def get_bond_by_uid(self, uid: str):
        with self._client() as client:
            return client.instruments.bond_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid).instrument

    def get_share_by_uid(self, uid: str):
        with self._client() as client:
            return client.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid).instrument

    def get_etf_by_uid(self, uid: str):
        with self._client() as client:
            return client.instruments.etf_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid).instrument

    def get_asset_by(self, asset_uid: str):
        """Full asset metadata (AssetFull → AssetSecurity → AssetEtf for funds)."""
        with self._client() as client:
            return client.instruments.get_asset_by(id=asset_uid)

    # -- market data -------------------------------------------------------------
    def get_last_price(self, uid: str):
        with self._client() as client:
            prices = client.market_data.get_last_prices(instrument_id=[uid]).last_prices
            return prices[0] if prices else None

    def get_last_prices(self, uids: list[str]) -> list:
        """Batch last-price lookup (one call for many instruments)."""
        uids = [u for u in uids if u]
        if not uids:
            return []
        with self._client() as client:
            return list(client.market_data.get_last_prices(instrument_id=uids).last_prices)

    def get_order_book(self, uid: str, depth: int = 10):
        with self._client() as client:
            return client.market_data.get_order_book(instrument_id=uid, depth=depth)

    def get_trading_status(self, uid: str):
        with self._client() as client:
            return client.market_data.get_trading_status(instrument_id=uid)

    # -- order pricing -----------------------------------------------------------
    def get_order_price(self, account_id: str, uid: str, price: Decimal, direction: str, quantity: int):
        request = GetOrderPriceRequest(
            account_id=account_id,
            instrument_id=uid,
            price=to_quotation(price),
            direction=_DIRECTION[direction],
            quantity=quantity,
        )
        with self._client() as client:
            if self.settings.is_sandbox:
                return client.sandbox.get_sandbox_order_price(request=request)
            return client.orders.get_order_price(request=request)

    def get_max_lots(self, account_id: str, uid: str, price: Decimal):
        request = GetMaxLotsRequest(account_id=account_id, instrument_id=uid, price=to_quotation(price))
        with self._client() as client:
            if self.settings.is_sandbox:
                return client.sandbox.get_sandbox_max_lots(request=request)
            return client.orders.get_max_lots(request=request)

    # -- order execution ---------------------------------------------------------
    def post_order(
        self,
        *,
        account_id: str,
        uid: str,
        quantity: int,
        price: Decimal,
        direction: str,
        order_type: str,
        order_id: str,
    ):
        kwargs = {
            "instrument_id": uid,
            "quantity": quantity,
            "price": to_quotation(price),
            "direction": _DIRECTION[direction],
            "account_id": account_id,
            "order_type": _ORDER_TYPE[order_type],
            "order_id": order_id,
        }
        with self._client(trade=True) as client:
            if self.settings.is_sandbox:
                return client.sandbox.post_sandbox_order(**kwargs)
            return client.orders.post_order(**kwargs)

    def get_order_state(self, account_id: str, order_id: str):
        with self._client(trade=True) as client:
            if self.settings.is_sandbox:
                return client.sandbox.get_sandbox_order_state(account_id=account_id, order_id=order_id)
            return client.orders.get_order_state(account_id=account_id, order_id=order_id)

    def cancel_order(self, account_id: str, order_id: str):
        with self._client(trade=True) as client:
            if self.settings.is_sandbox:
                return client.sandbox.cancel_sandbox_order(account_id=account_id, order_id=order_id)
            return client.orders.cancel_order(account_id=account_id, order_id=order_id)

    # -- sandbox-only helpers ----------------------------------------------------
    def open_sandbox_account(self, name: str = "ai-treasury-sandbox") -> str:
        if not self.settings.is_sandbox:
            raise TInvestConfigurationError("open_sandbox_account is only available in sandbox mode")
        with self._client() as client:
            return client.sandbox.open_sandbox_account(name=name).account_id

    def sandbox_pay_in(self, account_id: str, amount: Decimal, currency: str = "rub"):
        if not self.settings.is_sandbox:
            raise TInvestConfigurationError("sandbox_pay_in is only available in sandbox mode")
        with self._client() as client:
            return client.sandbox.sandbox_pay_in(account_id=account_id, amount=to_money(amount, currency))
