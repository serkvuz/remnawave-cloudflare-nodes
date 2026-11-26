from .dns import build_fqdn
from .logger import setup_logger, get_logger
from .time import format_duration, format_epoch, format_timestamp, parse_duration

__all__ = [
    "setup_logger", "get_logger", "format_timestamp", "format_epoch", "format_duration",
    "parse_duration", "build_fqdn", "short_error",
]


def short_error(e: Exception) -> str:
    """Return a single-line, length-capped error string. Prevents HTML bodies from flooding logs."""
    first_line = str(e).split('\n')[0]
    return first_line[:250] if len(first_line) > 250 else first_line
