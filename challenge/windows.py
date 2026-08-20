"""Session-window helpers for the challenge (local time = platform time)."""

from datetime import datetime


def minutes_of(hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def in_session_window(cfg, now: datetime) -> bool:
    s = cfg["session"]
    start = minutes_of(s["start_local"])
    end = minutes_of(s["end_local"])
    t = now.hour * 60 + now.minute
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def in_flatten_window(cfg, now: datetime) -> bool:
    s = cfg["session"]
    flatten = minutes_of(s["flatten_local"])
    end = minutes_of(s["end_local"])
    t = now.hour * 60 + now.minute
    if flatten <= end:
        return flatten <= t < end
    return t >= flatten or t < end