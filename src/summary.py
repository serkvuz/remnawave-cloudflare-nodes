import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

from .utils import format_epoch, get_logger

if TYPE_CHECKING:
    from .config import Config
    from .state import StateStore
    from .telegram import TelegramNotifier

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class SummaryTracker:
    """Collects uptime and change statistics and sends a periodic report.

    Statistics accumulate in the state file, so a restart does not reset the
    period. Time is credited per cycle: the gap since the previous cycle counts
    toward the state the node is in now. Gaps longer than a few check
    intervals (the service was down) are capped, so time nobody observed is
    not counted as uptime. Nodes in maintenance accrue no time at all.
    """

    def __init__(self, config: "Config", state: Optional["StateStore"], notifier: Optional["TelegramNotifier"]):
        self.config = config
        self.state = state
        self.notifier = notifier
        self.logger = get_logger(__name__)
        data = state.summary if state else {}
        fresh = self._empty_period(time.time())
        if data.get("period_start") and isinstance(data.get("nodes"), dict):
            # Fill keys a state file written by an older version may lack.
            data = {**fresh, **data}
            for entry in data["nodes"].values():
                entry.setdefault("up", 0.0)
                entry.setdefault("observed", 0.0)
                entry.setdefault("outages", 0)
                entry.setdefault("longest", 0.0)
                entry.setdefault("down_since", None)
                entry.setdefault("name", "")
        else:
            data = fresh
        self._data: Dict[str, Any] = data
        self._publish()

    # --- persistence ---

    @staticmethod
    def _empty_period(now: float) -> Dict[str, Any]:
        return {
            "period_start": now,
            "last_tick": None,
            "next_send": None,
            "schedule_key": "",
            "nodes": {},
            "dns_added": 0,
            "dns_removed": 0,
            "check_cycles": 0,
            "check_failures": 0,
        }

    def _publish(self) -> None:
        if self.state:
            self.state.summary = self._data
            self.state.mark_dirty()

    # --- recording ---

    def _credit(self, now: float) -> float:
        last = self._data.get("last_tick")
        self._data["last_tick"] = now
        if not last:
            return 0.0
        cap = max(3 * self.config.check_interval, 60)
        return max(0.0, min(now - last, cap))

    def record_cycle(self, nodes: Iterable[Tuple[str, str, bool, bool]]) -> None:
        """Record one successful cycle. `nodes` holds (address, name, healthy, in_maintenance)
        for every configured node, where healthy is the state DNS is following."""
        now = time.time()
        dt = self._credit(now)
        self._data["check_cycles"] += 1
        stats = self._data["nodes"]
        for address, name, healthy, in_maintenance in nodes:
            entry = stats.setdefault(address, {
                "name": name, "up": 0.0, "observed": 0.0, "outages": 0, "longest": 0.0, "down_since": None,
            })
            entry["name"] = name
            if in_maintenance:
                self._close_outage(entry, now)
                continue
            entry["observed"] += dt
            if healthy:
                entry["up"] += dt
                self._close_outage(entry, now)
            elif entry["down_since"] is None:
                entry["down_since"] = now
                entry["outages"] += 1
        self._publish()

    @staticmethod
    def _close_outage(entry: Dict[str, Any], now: float) -> None:
        if entry["down_since"] is not None:
            entry["longest"] = max(entry["longest"], now - entry["down_since"])
            entry["down_since"] = None

    def record_failure(self) -> None:
        self._credit(time.time())  # failed cycles observe nothing
        self._data["check_cycles"] += 1
        self._data["check_failures"] += 1
        self._publish()

    def record_dns_change(self, action: str) -> None:
        key = "dns_added" if action == "added" else "dns_removed"
        self._data[key] += 1
        self._publish()

    # --- reporting ---

    def build_report(self, scheduled: bool = False):
        from .telegram import SummaryReport

        now = time.time()
        nodes = []
        longest: Optional[Dict[str, Any]] = None
        for address, entry in self._data["nodes"].items():
            ongoing = entry["down_since"] is not None
            node_longest = max(entry["longest"], now - entry["down_since"] if ongoing else 0.0)
            observed = entry["observed"]
            uptime = (entry["up"] / observed * 100) if observed > 0 else None
            item = {
                "name": entry["name"],
                "address": address,
                "uptime": uptime,
                "outages": entry["outages"],
                "longest": node_longest,
                "ongoing": ongoing,
            }
            nodes.append(item)
            if node_longest > 0 and (longest is None or node_longest > longest["longest"]):
                longest = item
        # Worst first: that is what the reader is looking for.
        nodes.sort(key=lambda n: (n["uptime"] if n["uptime"] is not None else 101, n["name"]))

        settings = self.config.summary
        return SummaryReport(
            kind=settings["schedule"] if scheduled else "current",
            period_start=format_epoch(self._data["period_start"], self.config),
            period_end=format_epoch(now, self.config),
            nodes=nodes,
            longest=longest,
            dns_added=self._data["dns_added"],
            dns_removed=self._data["dns_removed"],
            check_cycles=self._data["check_cycles"],
            check_failures=self._data["check_failures"],
        )

    def _schedule_key(self) -> str:
        s = self.config.summary
        return f"{s['schedule']}|{s['time']}|{s['weekday']}|{self.config.timezone}"

    def next_send_time(self, now: float) -> float:
        s = self.config.summary
        try:
            tz = ZoneInfo(self.config.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        hour, minute = (int(part) for part in s["time"].split(":"))
        now_local = datetime.fromtimestamp(now, tz)
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        step = timedelta(days=1)
        if s["schedule"] == "weekly":
            candidate += timedelta(days=(_WEEKDAYS.index(s["weekday"]) - candidate.weekday()) % 7)
            step = timedelta(days=7)
        if candidate.timestamp() <= now:
            candidate += step
        # Aware-datetime arithmetic keeps wall-clock time, so DST shifts are honoured.
        return candidate.timestamp()

    def maybe_send(self) -> None:
        """Send the scheduled report if it is due. Call once per cycle."""
        settings = self.config.summary
        if not settings["enabled"]:
            return
        now = time.time()
        key = self._schedule_key()
        if self._data.get("schedule_key") != key or not self._data.get("next_send"):
            # First run, or the schedule changed: plan the next send without sending now.
            self._data["schedule_key"] = key
            self._data["next_send"] = self.next_send_time(now)
            self.logger.info(f"Next {settings['schedule']} summary at {format_epoch(self._data['next_send'], self.config)}")
            self._publish()
            return
        if now < self._data["next_send"]:
            return

        report = self.build_report(scheduled=True)
        if self.notifier:
            self.notifier.notify_summary(report)
        self.logger.info("Sent periodic summary")
        self._reset_period(now)
        self._data["schedule_key"] = key
        self._data["next_send"] = self.next_send_time(now)
        self._publish()

    def _reset_period(self, now: float) -> None:
        fresh = self._empty_period(now)
        fresh["last_tick"] = self._data.get("last_tick")
        for address, entry in self._data["nodes"].items():
            if entry["down_since"] is not None:
                # An outage still running carries into the new period, timed from its start there.
                fresh["nodes"][address] = {
                    "name": entry["name"], "up": 0.0, "observed": 0.0, "outages": 1,
                    "longest": 0.0, "down_since": now,
                }
        self._data = fresh
