"""Currency awareness: settlement currency vs denomination currency.

A MOEX bond can **settle in rubles while its economics are foreign**. The
CNY-linked ("юаневые") issues are the common case: the same ISIN is listed twice,
once on a CNY board and once on a RUB-settled board, and only the settlement
currency differs::

    Bond.currency          = 'rub'   # what you pay with
    Bond.nominal.currency  = 'cny'   # what the bond actually pays you
    Bond.aci_value.currency= 'cny'
    coupon.pay_one_bond    = MoneyValue(currency='cny', …)

Two consequences, both of which used to be invisible to callers:

1. **Yields are denomination-currency yields.** YTM / current yield are solved
   from the CNY coupon schedule over a CNY dirty price, so an 8.7% figure is
   8.7% *in yuan*. Ranking it next to a 15.7% ruble OFZ is comparing a currency
   bet with a rate bet — see :func:`fx_breakeven_annual_pct`.
2. **Every money figure derived from the nominal is in the denomination
   currency**, because bond prices are a percent of face value. Turnover,
   order value and plan cash all come out in CNY and must be converted before
   they meet a ruble limit (``max_order_rub``, ``min_avg_daily_turnover``,
   available cash).

Rates come from the exchange's own FX instruments (``CNYRUB_TOM`` and friends,
all quoted in rubles per ``nominal`` units of the foreign currency), cached for
a few minutes because a stale-by-minutes rate is irrelevant at these magnitudes.

The pure helpers here take no SDK objects, so they unit-test without gRPC.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .money import money_to_decimal, quotation_to_decimal

BASE_CURRENCY = "rub"

# How long a rate stays usable. FX moves far slower than the errors this module
# prevents (an unconverted CNY figure is off by ~11x), so minutes are fine.
_RATE_TTL_SECONDS = 300

_ZERO = Decimal("0")


def normalize_currency(value: Any) -> str | None:
    """Lower-cased ISO code, or ``None`` for a missing/blank value."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def currency_of(money: Any) -> str | None:
    """Currency code carried by a ``MoneyValue``-shaped object."""
    if money is None:
        return None
    return normalize_currency(getattr(money, "currency", None))


def denomination_currency(
    settlement_currency: str | None,
    nominal_currency: str | None = None,
) -> str | None:
    """Currency the instrument's cash flows are actually denominated in.

    The bond nominal wins when the two disagree: coupons, redemption and the
    percent-of-nominal price all follow the nominal, not the settlement leg.
    """
    return normalize_currency(nominal_currency) or normalize_currency(settlement_currency)


def is_fx_linked(currency: str | None, *, base: str = BASE_CURRENCY) -> bool:
    """True when cash flows are denominated in something other than *base*."""
    code = normalize_currency(currency)
    return bool(code) and code != normalize_currency(base)


def fx_breakeven_annual_pct(
    foreign_yield_pct: Decimal | None,
    base_yield_pct: Decimal | None,
) -> Decimal | None:
    """Annual FX move that would make a foreign-currency yield match a base one.

    A bond yielding ``y_f`` in CNY returns ``(1 + y_f)(1 + fx) - 1`` in rubles,
    where ``fx`` is the annual CNY/RUB change. Setting that equal to the ruble
    alternative ``y_b`` gives ``fx = (1 + y_b) / (1 + y_f) - 1`` — the yearly
    appreciation the currency has to deliver just to break even. Positive means
    the foreign currency must strengthen for the trade to be worth it.
    """
    if foreign_yield_pct is None or base_yield_pct is None:
        return None
    hundred = Decimal(100)
    foreign = Decimal(foreign_yield_pct) / hundred
    base = Decimal(base_yield_pct) / hundred
    if foreign <= Decimal("-1"):
        return None
    return (((Decimal(1) + base) / (Decimal(1) + foreign)) - Decimal(1)) * hundred


@dataclass(frozen=True)
class FxRate:
    """Rubles per ONE unit of *currency*, with its provenance."""

    currency: str
    rate: Decimal
    as_of: datetime | None = None
    source_ticker: str | None = None
    source_uid: str | None = None

    def to_rub(self, amount: Decimal | None) -> Decimal | None:
        if amount is None:
            return None
        return (Decimal(amount) * self.rate).quantize(Decimal("0.01"))

    def from_rub(self, amount: Decimal | None) -> Decimal | None:
        if amount is None or self.rate <= 0:
            return None
        return (Decimal(amount) / self.rate).quantize(Decimal("0.01"))


def rate_from_instrument(obj: Any, last_price: Decimal | None) -> Decimal | None:
    """Rubles per one foreign unit, from an FX catalogue row and its last price.

    Exchange FX instruments quote rubles per ``nominal`` units, and the nominal
    is NOT always 1 (``KZTRUB_TOM`` is per 100 tenge, ``UZSRUB_TOM`` per 10 000
    sum), so the price alone is not a rate.
    """
    if last_price is None or last_price <= 0:
        return None
    nominal = money_to_decimal(getattr(obj, "nominal", None))
    if nominal is None or nominal <= 0:
        nominal = Decimal(1)
    try:
        return Decimal(last_price) / nominal
    except (InvalidOperation, ZeroDivisionError):
        return None


class _RateCache:
    """Thread-safe TTL cache of resolved rates, keyed by currency code."""

    def __init__(self, ttl_seconds: int = _RATE_TTL_SECONDS) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.Lock()
        self._items: dict[str, tuple[datetime, FxRate | None]] = {}

    def get(self, currency: str) -> tuple[bool, FxRate | None]:
        """``(hit, rate)`` — a cached ``None`` still counts as a hit."""
        with self._lock:
            entry = self._items.get(currency)
        if entry is None:
            return False, None
        stored_at, rate = entry
        if datetime.now(UTC) - stored_at > self._ttl:
            return False, None
        return True, rate

    def put(self, currency: str, rate: FxRate | None) -> None:
        with self._lock:
            self._items[currency] = (datetime.now(UTC), rate)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_cache = _RateCache()


def clear_cache() -> None:
    """Drop cached rates (tests, and a manual refresh path)."""
    _cache.clear()


def get_fx_rate(adapter, currency: str | None) -> FxRate | None:
    """Rubles per one unit of *currency*, or ``None`` when it cannot be priced.

    The base currency resolves to a free 1.0 without touching the network.
    Failures are cached as ``None`` for the same TTL so a broken FX board does
    not turn every screener row into a retry storm — callers must treat a
    missing rate as "cannot compare in rubles", never as 1:1.
    """
    code = normalize_currency(currency)
    if code is None:
        return None
    if code == BASE_CURRENCY:
        return FxRate(currency=BASE_CURRENCY, rate=Decimal(1), source_ticker=None)

    hit, cached = _cache.get(code)
    if hit:
        return cached

    resolved: FxRate | None = None
    try:
        for obj in adapter.list_currencies():
            iso = normalize_currency(getattr(obj, "iso_currency_name", None)) or currency_of(
                getattr(obj, "nominal", None)
            )
            if iso != code:
                continue
            last = adapter.get_last_price(obj.uid)
            price = quotation_to_decimal(getattr(last, "price", None))
            rate = rate_from_instrument(obj, price)
            if rate is None or rate <= 0:
                continue
            resolved = FxRate(
                currency=code,
                rate=rate,
                as_of=getattr(last, "time", None),
                source_ticker=getattr(obj, "ticker", None) or None,
                source_uid=getattr(obj, "uid", None) or None,
            )
            break
    except Exception:
        resolved = None

    _cache.put(code, resolved)
    return resolved


def to_rub(
    amount: Decimal | None,
    currency: str | None,
    rate: FxRate | None,
) -> Decimal | None:
    """Convert *amount* to rubles; ``None`` when the rate is missing."""
    if amount is None:
        return None
    code = normalize_currency(currency)
    if code is None or code == BASE_CURRENCY:
        return Decimal(amount)
    if rate is None or rate.currency != code:
        return None
    return rate.to_rub(amount)


def fx_note(currency: str | None, rate: FxRate | None) -> str | None:
    """One-line explanation attached to FX-linked instrument output."""
    code = normalize_currency(currency)
    if not is_fx_linked(code):
        return None
    upper = (code or "").upper()
    head = (
        f"FX-linked: nominal, coupons and redemption are in {upper}, so every yield "
        f"below is a {upper} yield — do NOT rank it against ruble instruments. "
        f"The ruble return also depends on {upper}/RUB."
    )
    if rate is None:
        return head + f" {upper}/RUB rate unavailable — ruble equivalents are not computed."
    return head + f" Converted at {upper}/RUB = {rate.rate.quantize(Decimal('0.0001'))}."
