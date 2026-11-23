import time

from app import clock


def test_wall_is_local_time_to_the_minute():
    ts = time.mktime((2026, 11, 23, 4, 26, 59, 0, 0, -1))
    assert clock.wall(ts) == "2026-11-23 04:26"


def test_wall_is_none_without_a_timestamp():
    assert clock.wall(None) is None
    assert clock.wall(0) is None


def test_zone_has_a_name_and_an_offset():
    assert "(UTC" in clock.zone()
