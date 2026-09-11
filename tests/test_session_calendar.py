"""Unit tests for the MOEX fondovy session calendar."""

from datetime import UTC, datetime

from tinvest_mcp.session_calendar import MSK, moex_session_state


def msk(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=MSK)


# 2026-07-21 is a Tuesday; 2026-07-24 Friday; 2026-07-25 Saturday; 2026-07-27 Monday.


def test_main_session_is_tradeable():
    st = moex_session_state(msk(2026, 7, 21, 12, 0))
    assert st.tradeable_now is True
    assert st.closing_soon is False
    assert st.phase == "MAIN"
    assert st.resumes_at is None
    assert st.should_block is False


def test_morning_and_evening_sessions_tradeable():
    assert moex_session_state(msk(2026, 7, 21, 8, 0)).phase == "MORNING"
    assert moex_session_state(msk(2026, 7, 21, 20, 0)).phase == "EVENING"
    assert moex_session_state(msk(2026, 7, 21, 8, 0)).tradeable_now is True
    assert moex_session_state(msk(2026, 7, 21, 20, 0)).tradeable_now is True


def test_evening_clearing_pause_blocks_and_resumes_at_1905():
    st = moex_session_state(msk(2026, 7, 21, 18, 45))
    assert st.tradeable_now is False
    assert st.should_block is True
    assert st.phase == "PAUSE"
    resumes = st.resumes_at.astimezone(MSK)
    assert (resumes.hour, resumes.minute) == (19, 5)
    assert resumes.date() == msk(2026, 7, 21, 0, 0).date()


def test_daytime_clearing_pause_resumes_at_1405():
    st = moex_session_state(msk(2026, 7, 21, 14, 2))
    assert st.tradeable_now is False
    resumes = st.resumes_at.astimezone(MSK)
    assert (resumes.hour, resumes.minute) == (14, 5)


def test_preopen_auction_gap_blocks_and_resumes_at_1000():
    st = moex_session_state(msk(2026, 7, 21, 9, 55))
    assert st.tradeable_now is False
    assert st.phase == "PAUSE"
    resumes = st.resumes_at.astimezone(MSK)
    assert (resumes.hour, resumes.minute) == (10, 0)


def test_closing_soon_within_buffer():
    # 60s before the 18:40 evening clearing, with a 120s buffer.
    st = moex_session_state(msk(2026, 7, 21, 18, 39, 0), buffer_seconds=120)
    assert st.tradeable_now is True
    assert st.closing_soon is True
    assert st.seconds_until_pause == 60
    assert "clearing pause" in st.message


def test_not_closing_soon_just_outside_buffer():
    # 180s before 18:40 with a 120s buffer -> tradeable, not flagged.
    st = moex_session_state(msk(2026, 7, 21, 18, 37, 0), buffer_seconds=120)
    assert st.tradeable_now is True
    assert st.closing_soon is False


def test_overnight_closed_resumes_same_day_morning():
    st = moex_session_state(msk(2026, 7, 21, 3, 0))
    assert st.tradeable_now is False
    assert st.phase == "CLOSED"
    resumes = st.resumes_at.astimezone(MSK)
    assert (resumes.date(), resumes.hour, resumes.minute) == (msk(2026, 7, 21, 0, 0).date(), 7, 0)


def test_after_close_rolls_to_next_day():
    st = moex_session_state(msk(2026, 7, 21, 23, 55))
    assert st.phase == "CLOSED"
    resumes = st.resumes_at.astimezone(MSK)
    assert (resumes.date(), resumes.hour) == (msk(2026, 7, 22, 0, 0).date(), 7)


def test_weekend_closed_resumes_monday():
    st = moex_session_state(msk(2026, 7, 25, 12, 0))  # Saturday
    assert st.tradeable_now is False
    assert st.phase == "WEEKEND"
    resumes = st.resumes_at.astimezone(MSK)
    assert resumes.weekday() == 0  # Monday
    assert (resumes.date(), resumes.hour, resumes.minute) == (msk(2026, 7, 27, 0, 0).date(), 7, 0)


def test_friday_after_close_skips_weekend_to_monday():
    st = moex_session_state(msk(2026, 7, 24, 23, 55))  # Friday, after close
    resumes = st.resumes_at.astimezone(MSK)
    assert resumes.weekday() == 0  # Monday
    assert resumes.date() == msk(2026, 7, 27, 0, 0).date()


def test_exact_boundaries():
    # 18:40:00 exactly -> pause (main ends exclusive); 10:00:00 -> main open.
    assert moex_session_state(msk(2026, 7, 21, 18, 40, 0)).tradeable_now is False
    assert moex_session_state(msk(2026, 7, 21, 10, 0, 0)).tradeable_now is True


def test_naive_datetime_treated_as_utc():
    # 15:40 UTC == 18:40 MSK -> evening clearing pause.
    naive = datetime(2026, 7, 21, 15, 45)
    st = moex_session_state(naive)
    assert st.tradeable_now is False
    assert st.phase == "PAUSE"
    # And an explicit UTC datetime gives the same answer.
    aware = datetime(2026, 7, 21, 15, 45, tzinfo=UTC)
    assert moex_session_state(aware).tradeable_now is False
