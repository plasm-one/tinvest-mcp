"""MOEX equity/bond session calendar — is continuous matching happening *now*?

Pure, dependency-light, timezone-fixed. Complements the broker's
``trading_status`` flag, which does **not** flip during the intraday clearing
pauses (notably the 18:40–19:05 evening clearing break): a LIMIT order submitted
into a pause just rests in the book unmatched until the session resumes, which is
exactly how a plan step "hangs". From the wall clock alone this module answers
whether the exchange is matching right now, whether a pause/close is imminent,
and when matching next resumes.

Schedule (Московская биржа, фондовый рынок; all times MSK, weekdays only):

    morning session   07:00–09:50
    (pre-open auction  09:50–10:00)   ← no continuous matching
    main session       10:00–18:40
    (daytime clearing  14:00–14:05)   ← no continuous matching
    (evening clearing  18:40–19:05)   ← no continuous matching
    evening session    19:05–23:50

Weekends are closed. Exchange holidays are **not** modelled here — the broker
``trading_status`` / ``api_trade_available`` checks remain the backstop for those.

Moscow has observed a fixed UTC+3 with no DST since 2014, so MSK is a plain fixed
offset (no ``zoneinfo``/tzdata dependency, no DST edge cases).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone

# Moscow is permanently UTC+3 (no daylight saving since 2014).
MSK = timezone(timedelta(hours=3))

# Continuous-matching windows on a normal trading day, in MSK, as
# (start, end, phase). Any moment outside these windows on a weekday is a pause
# (14:00–14:05, 09:50–10:00, 18:40–19:05) or the overnight close.
_TRADEABLE_WINDOWS: tuple[tuple[time, time, str], ...] = (
    (time(7, 0), time(9, 50), "MORNING"),
    (time(10, 0), time(14, 0), "MAIN"),
    (time(14, 5), time(18, 40), "MAIN"),
    (time(19, 5), time(23, 50), "EVENING"),
)

# Human label for the pause that begins when each window closes (keyed by the
# window's end time), used to make "closing soon" warnings specific.
_PAUSE_AFTER: dict[time, str] = {
    time(9, 50): "pre-open auction (09:50–10:00 MSK)",
    time(14, 0): "daytime clearing pause (14:00–14:05 MSK)",
    time(18, 40): "evening clearing pause (18:40–19:05 MSK)",
    time(23, 50): "session close (23:50 MSK)",
}

_MORNING_OPEN = _TRADEABLE_WINDOWS[0][0]  # 07:00
_EVENING_CLOSE = _TRADEABLE_WINDOWS[-1][1]  # 23:50


@dataclass(frozen=True)
class MoexSessionState:
    """Whether MOEX is matching orders right now, and if not, when it will."""

    tradeable_now: bool  # continuous matching is happening at ``now``
    closing_soon: bool  # tradeable, but a pause/close begins within the buffer
    phase: str  # MORNING | MAIN | EVENING | PAUSE | CLOSED | WEEKEND
    resumes_at: datetime | None  # UTC; next moment matching resumes (None while tradeable)
    seconds_until_pause: int | None  # while tradeable: seconds to the next matching stop
    message: str  # human-readable explanation for warnings / paused reasons

    @property
    def should_block(self) -> bool:
        """A limit order placed now would rest unmatched — do not submit."""
        return not self.tradeable_now


def _fmt(dt: datetime) -> str:
    """Format a resume time in MSK, e.g. ``Mon 07:00 MSK`` / ``19:05 MSK``."""
    return dt.astimezone(MSK).strftime("%a %H:%M MSK")


def _next_window_start(msk: datetime) -> datetime:
    """First window open strictly after ``msk`` (MSK-aware), skipping weekends."""
    if msk.weekday() < 5:
        for start, _end, _phase in _TRADEABLE_WINDOWS:
            cand = msk.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            if cand > msk:
                return cand
    nxt = msk + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.replace(hour=_MORNING_OPEN.hour, minute=_MORNING_OPEN.minute, second=0, microsecond=0)


def moex_session_state(now: datetime, *, buffer_seconds: int = 120) -> MoexSessionState:
    """Classify the MOEX fondovy session at ``now``.

    ``now`` may be naive (treated as UTC) or tz-aware. ``buffer_seconds`` is how
    far before a pause/close a still-tradeable window is flagged ``closing_soon``
    so a near-boundary order that might not fill in time is surfaced up front.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    msk = now.astimezone(MSK)

    if msk.weekday() >= 5:  # Saturday / Sunday
        resumes = _next_window_start(msk)
        return MoexSessionState(
            tradeable_now=False,
            closing_soon=False,
            phase="WEEKEND",
            resumes_at=resumes.astimezone(UTC),
            seconds_until_pause=None,
            message=f"Exchange closed (weekend); trading resumes {_fmt(resumes)}.",
        )

    now_t = msk.time()
    for _start, end, phase in _TRADEABLE_WINDOWS:
        if _start <= now_t < end:
            end_dt = msk.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            secs = int((end_dt - msk).total_seconds())
            closing_soon = secs <= max(0, buffer_seconds)
            if closing_soon:
                pause = _PAUSE_AFTER.get(end, "a trading pause")
                message = (
                    f"{phase} session ends in ~{secs // 60}m{secs % 60:02d}s "
                    f"(next: {pause}) — a new limit may not fill before matching stops."
                )
            else:
                message = f"Exchange is matching orders ({phase} session)."
            return MoexSessionState(
                tradeable_now=True,
                closing_soon=closing_soon,
                phase=phase,
                resumes_at=None,
                seconds_until_pause=secs,
                message=message,
            )

    # Outside every window on a weekday: an intraday pause or the overnight close.
    resumes = _next_window_start(msk)
    if now_t < _MORNING_OPEN or now_t >= _EVENING_CLOSE:
        return MoexSessionState(
            tradeable_now=False,
            closing_soon=False,
            phase="CLOSED",
            resumes_at=resumes.astimezone(UTC),
            seconds_until_pause=None,
            message=f"Exchange is closed; trading resumes {_fmt(resumes)}.",
        )
    return MoexSessionState(
        tradeable_now=False,
        closing_soon=False,
        phase="PAUSE",
        resumes_at=resumes.astimezone(UTC),
        seconds_until_pause=None,
        message=(f"Exchange is in a clearing pause — orders rest unmatched; trading resumes {_fmt(resumes)}."),
    )
