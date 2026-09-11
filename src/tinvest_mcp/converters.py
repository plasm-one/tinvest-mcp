"""Convert raw T-Invest SDK objects into normalized domain models.

Kept separate from the adapter so the mapping is unit-testable and the rest of
the code never touches SDK field names directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .money import money_to_decimal, quotation_to_decimal
from .schemas import (
    BrokerAccount,
    BrokerOperation,
    InstrumentSearchHit,
    InvestmentInstrument,
    MarketSnapshot,
    OperationTradeItem,
    PortfolioPosition,
    price_quote_unit_for,
)


def enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", str(value))


def _currency_of(money: Any) -> str | None:
    cur = getattr(money, "currency", None)
    return cur.lower() if isinstance(cur, str) else cur


def account_to_model(acc: Any) -> BrokerAccount:
    return BrokerAccount(
        id=acc.id,
        name=getattr(acc, "name", None) or None,
        status=enum_name(getattr(acc, "status", None)) or "UNKNOWN",
        account_type=enum_name(getattr(acc, "type", None)) or "UNKNOWN",
        opened_at=getattr(acc, "opened_date", None),
        closed_at=getattr(acc, "closed_date", None),
        research_access_level=enum_name(getattr(acc, "access_level", None)),
    )


def position_to_model(pos: Any) -> PortfolioPosition:
    quantity = quotation_to_decimal(getattr(pos, "quantity", None))
    avg_price = money_to_decimal(getattr(pos, "average_position_price", None))
    cur_price = money_to_decimal(getattr(pos, "current_price", None))
    current_value: Decimal | None = None
    yield_abs: Decimal | None = None
    if quantity is not None and cur_price is not None:
        current_value = quantity * cur_price
        if avg_price is not None:
            yield_abs = (cur_price - avg_price) * quantity
    return PortfolioPosition(
        instrument_uid=getattr(pos, "instrument_uid", "") or "",
        figi=getattr(pos, "figi", None) or None,
        ticker=getattr(pos, "ticker", None) or None,
        name=None,
        instrument_type=getattr(pos, "instrument_type", "") or "",
        quantity=quantity,
        quantity_lots=quotation_to_decimal(getattr(pos, "quantity_lots", None)),
        average_price=avg_price,
        current_price=cur_price,
        current_value=current_value,
        expected_yield_absolute=yield_abs,
        expected_yield_percent=quotation_to_decimal(getattr(pos, "expected_yield", None)),
        currency=_currency_of(getattr(pos, "current_price", None))
        or _currency_of(getattr(pos, "average_position_price", None)),
        blocked=None,
    )


def _operation_type_short(raw: str) -> str:
    name = (raw or "").removeprefix("OPERATION_TYPE_").lower()
    return name or "unknown"


def operation_to_model(item: Any) -> BrokerOperation:
    type_raw = enum_name(getattr(item, "type", None)) or "OPERATION_TYPE_UNSPECIFIED"
    payment = money_to_decimal(getattr(item, "payment", None))
    commission = money_to_decimal(getattr(item, "commission", None))
    price = money_to_decimal(getattr(item, "price", None))
    currency = (
        _currency_of(getattr(item, "payment", None))
        or _currency_of(getattr(item, "price", None))
        or _currency_of(getattr(item, "commission", None))
    )
    trades: list[OperationTradeItem] = []
    trades_info = getattr(item, "trades_info", None)
    for trade in getattr(trades_info, "trades", None) or []:
        trade_price = money_to_decimal(getattr(trade, "price", None))
        trades.append(
            OperationTradeItem(
                trade_id=getattr(trade, "num", None) or None,
                date_time=getattr(trade, "date", None),
                quantity=int(getattr(trade, "quantity", 0) or 0) or None,
                price=trade_price,
                currency=_currency_of(getattr(trade, "price", None)),
            )
        )
    return BrokerOperation(
        id=getattr(item, "id", "") or "",
        date=getattr(item, "date", None),
        type=_operation_type_short(type_raw),
        type_raw=type_raw,
        state=enum_name(getattr(item, "state", None)) or "UNKNOWN",
        name=getattr(item, "name", None) or None,
        description=getattr(item, "description", None) or None,
        instrument_uid=getattr(item, "instrument_uid", None) or None,
        ticker=getattr(item, "ticker", None) or None,
        figi=getattr(item, "figi", None) or None,
        instrument_type=getattr(item, "instrument_type", None) or None,
        payment=payment,
        price=price,
        commission=commission,
        quantity=quotation_to_decimal(getattr(item, "quantity", None)),
        accrued_int=money_to_decimal(getattr(item, "accrued_int", None)),
        currency=currency,
        trades=trades,
    )


def instrument_to_model(obj: Any, instrument_type: str) -> InvestmentInstrument:
    return InvestmentInstrument(
        uid=obj.uid,
        figi=getattr(obj, "figi", None) or None,
        ticker=getattr(obj, "ticker", "") or "",
        name=getattr(obj, "name", "") or "",
        instrument_type=instrument_type,
        currency=(getattr(obj, "currency", "") or "").lower(),
        # The nominal carries its OWN currency: a yuan bond listed on a ruble
        # board reports currency='rub' with nominal.currency='cny'.
        nominal_currency=_currency_of(getattr(obj, "nominal", None)),
        lot=int(getattr(obj, "lot", 0) or 0),
        min_price_increment=quotation_to_decimal(getattr(obj, "min_price_increment", None)),
        price_quote_unit=price_quote_unit_for(instrument_type),
        api_trade_available=bool(getattr(obj, "api_trade_available_flag", False)),
        buy_available=bool(getattr(obj, "buy_available_flag", False)),
        sell_available=bool(getattr(obj, "sell_available_flag", False)),
        short_enabled=bool(getattr(obj, "short_enabled_flag", False)),
        qualified_investor_only=bool(getattr(obj, "for_qual_investor_flag", False)),
        exchange=getattr(obj, "exchange", None) or None,
        class_code=getattr(obj, "class_code", None) or None,
        nominal=money_to_decimal(getattr(obj, "nominal", None)),
        maturity_date=getattr(obj, "maturity_date", None),
        coupon_rate=None,
        isin=getattr(obj, "isin", None) or None,
        sector=getattr(obj, "sector", None) or None,
        country_of_risk=getattr(obj, "country_of_risk", None) or None,
        trading_status=enum_name(getattr(obj, "trading_status", None)),
    )


def search_hit_to_model(short: Any) -> InstrumentSearchHit:
    itype = getattr(short, "instrument_type", "") or ""
    return InstrumentSearchHit(
        uid=short.uid,
        figi=getattr(short, "figi", None) or None,
        ticker=getattr(short, "ticker", "") or "",
        name=getattr(short, "name", "") or "",
        instrument_type=itype,
        currency=None,
        lot=int(getattr(short, "lot", 0) or 0) or None,
        api_trade_available=bool(getattr(short, "api_trade_available_flag", False)),
        qualified_investor_only=bool(getattr(short, "for_qual_investor_flag", False)),
    )


def snapshot_to_model(
    uid: str,
    last_price: Any,
    order_book: Any,
    trading_status: Any,
    *,
    age_seconds: float | None,
    is_fresh: bool,
) -> MarketSnapshot:
    best_bid = None
    best_ask = None
    limit_up = None
    limit_down = None
    if order_book is not None:
        bids = getattr(order_book, "bids", None) or []
        asks = getattr(order_book, "asks", None) or []
        if bids:
            best_bid = quotation_to_decimal(getattr(bids[0], "price", None))
        if asks:
            best_ask = quotation_to_decimal(getattr(asks[0], "price", None))
        limit_up = quotation_to_decimal(getattr(order_book, "limit_up", None))
        limit_down = quotation_to_decimal(getattr(order_book, "limit_down", None))
    return MarketSnapshot(
        instrument_uid=uid,
        last_price=quotation_to_decimal(getattr(last_price, "price", None)) if last_price else None,
        last_price_time=getattr(last_price, "time", None) if last_price else None,
        best_bid=best_bid,
        best_ask=best_ask,
        limit_up=limit_up,
        limit_down=limit_down,
        trading_status=enum_name(getattr(trading_status, "trading_status", None)) if trading_status else None,
        api_trade_available=bool(getattr(trading_status, "api_trade_available_flag", False))
        if trading_status
        else False,
        age_seconds=age_seconds,
        is_fresh=is_fresh,
    )
