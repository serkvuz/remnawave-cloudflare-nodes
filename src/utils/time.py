from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # avoids a utils -> config -> utils import cycle at runtime
    from ..config import Config


def format_timestamp(timestamp_str: str, config: "Config") -> str:
    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    dt_local = dt.astimezone(ZoneInfo(config.timezone))
    tz_abbr = dt_local.strftime("%Z")
    return dt_local.strftime(f"{config.time_format} {tz_abbr}")


def format_epoch(ts: float, config: "Config") -> str:
    """Format a Unix timestamp in the configured TIMEZONE / TIME_FORMAT."""
    try:
        tz = ZoneInfo(config.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    dt_local = datetime.fromtimestamp(ts, tz)
    return dt_local.strftime(f"{config.time_format} {dt_local.strftime('%Z')}")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def parse_duration(text: str) -> int:
    """Parse '90s', '30m', '2h', '1d', '1h30m' or a bare number of minutes into seconds."""
    text = text.strip().lower()
    if not text:
        raise ValueError("empty duration")
    if text.isdigit():
        return int(text) * 60
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total, number = 0, ""
    for ch in text:
        if ch.isdigit():
            number += ch
        elif ch in units and number:
            total += int(number) * units[ch]
            number = ""
        else:
            raise ValueError(f"invalid duration '{text}' (use e.g. 30m, 2h, 1d)")
    if number or total <= 0:
        raise ValueError(f"invalid duration '{text}' (use e.g. 30m, 2h, 1d)")
    return total
