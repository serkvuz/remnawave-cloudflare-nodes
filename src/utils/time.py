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
