import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from .cloudflare_dns import CloudflareClient, DNSManager
from .config import Config
from .panel import HostManager, NodeMonitor, NodeStatus
from .utils import build_fqdn, format_epoch, get_logger, short_error

if TYPE_CHECKING:
    from .discovery import NodeDiscovery
    from .probe import TcpProber
    from .state import StateStore
    from .summary import SummaryTracker
    from .telegram import TelegramNotifier

# Zone IDs are stable for the life of a zone, but a zone deleted and recreated
# in Cloudflare gets a new one. Expiring the cache bounds how long a stale ID
# can be used when the zone changes outside this service.
_ZONE_CACHE_TTL_SECONDS = 3600


def node_identifiers(node) -> Set[str]:
    """Every string a user may use to refer to a node: name, address, UUID."""
    return {str(v).strip().lower() for v in (node.name, node.address, node.uuid) if v}


class MonitoringService:
    """One health-check cycle, and the state that spans cycles.

    Per cycle, each configured node gets a raw verdict (panel status, plus the
    TCP probe when enabled). Two layers then decide what DNS follows:

    - Maintenance: nodes listed under `maintenance:` are kept out of DNS and
      raise no alerts. When a node leaves maintenance it rejoins at its current
      raw state, with no flap-protection delay.
    - Flap protection: the state DNS follows (the "effective" state) flips only
      after `fail-threshold` consecutive unhealthy cycles, or
      `recover-threshold` consecutive healthy ones. Node alerts fire on
      effective flips, so a one-cycle blip produces neither a DNS change nor a
      message.

    While DNS changes are paused, health is still evaluated and alerts still
    fire, but Cloudflare and panel hosts are left untouched.
    """

    def __init__(
            self,
            config: Config,
            node_monitor: NodeMonitor,
            cloudflare_client: CloudflareClient,
            dns_manager: DNSManager,
            host_manager: Optional["HostManager"] = None,
            notifier: Optional["TelegramNotifier"] = None,
            state: Optional["StateStore"] = None,
            discovery: Optional["NodeDiscovery"] = None,
            prober: Optional["TcpProber"] = None,
            summary: Optional["SummaryTracker"] = None,
    ):
        self.config = config
        self.node_monitor = node_monitor
        self.cloudflare_client = cloudflare_client
        self.dns_manager = dns_manager
        self.host_manager = host_manager
        self.notifier = notifier
        self.state = state
        self.discovery = discovery
        self.prober = prober
        self.summary = summary
        self.logger = get_logger(__name__)
        self._zone_id_cache: Dict[str, Tuple[str, float]] = {}
        # Effective (DNS-facing) health per node address; persisted across restarts.
        self._previous_node_states: Dict[str, bool] = state.node_states if state else {}
        # Consecutive cycles whose raw verdict disagreed with the effective state.
        self._streaks: Dict[str, int] = {}
        self._maintenance: Set[str] = set(state.maintenance) if state else set()
        self._pause: Dict[str, Any] = state.pause if state else {}
        self._previous_all_down: bool = False
        # Health-check outage tracking: alert once when checks start failing and
        # once when they recover, instead of on every failed cycle.
        self._failed_cycles: int = 0
        self._failing_since: Optional[float] = None
        self._last_error: str = ""
        self._unmatched_maintenance_warned: Set[str] = set()
        # Snapshot of the last completed cycle, for /status and the API.
        self.last_nodes: List[NodeStatus] = []
        self.last_cycle_at: Optional[float] = None
        self._wake = asyncio.Event()
        # Serialises cycles with commands that inspect or change their state.
        self.lock = asyncio.Lock()

    # --- scheduling ---

    @property
    def wake_event(self) -> asyncio.Event:
        return self._wake

    def request_check(self) -> None:
        """Run the next cycle now instead of waiting out the interval."""
        self._wake.set()

    # --- startup ---

    async def initialize_and_print_zones(self) -> None:
        self.logger.info("Initializing zones")

        current_domain = None
        current_zone_id: Optional[str] = None
        for zone in self.config.get_all_zones():
            domain = zone["domain"]

            if domain != current_domain:
                current_zone_id = await self._get_zone_id(domain)
                current_domain = domain
                if not current_zone_id:
                    self.logger.warning(f"Could not find zone_id for domain {domain}")
                    continue
                self.logger.info(f"Domain: {domain}, Zone ID: {current_zone_id}")

            if not current_zone_id:
                continue

            full_domain = build_fqdn(zone['name'], domain)
            self.logger.info(f"  Zone: {full_domain}, TTL: {zone['ttl']}, Proxied: {zone['proxied']}")
            for entry in zone["nodes"]:
                note = f" (via {entry['address']})" if entry["address"] != entry["ip"] else ""
                self.logger.info(f"    Node: {entry['ip']}{note}")

            existing_records = await self.cloudflare_client.get_dns_records(current_zone_id, name=full_domain,
                                                                            record_type="A")
            if existing_records:
                existing_ips = [record["content"] for record in existing_records]
                self.logger.info(f"  Existing DNS records: {', '.join(existing_ips)}")
            else:
                self.logger.info("  Existing DNS records: None")

        if self.paused:
            self.logger.warning(f"DNS changes are paused ({self._pause_description()})")
        self.logger.info("Initialization complete")

    # --- the cycle ---

    async def perform_health_check(self) -> None:
        async with self.lock:
            try:
                await self._run_cycle()
            except Exception as e:
                self._record_health_check_failure(short_error(e))
                raise
            finally:
                if self.summary:
                    try:
                        self.summary.maybe_send()
                    except Exception as e:
                        self.logger.error(f"Could not send periodic summary: {short_error(e)}")
                if self.state:
                    self.state.save()
            self._record_health_check_success()

    async def _run_cycle(self) -> None:
        self.logger.info("Starting health check cycle")
        self._expire_pause()

        all_nodes = await self.node_monitor.check_all_nodes()

        if self.discovery:
            await self.discovery.discover(all_nodes)

        nodes_by_address = {node.address: node for node in all_nodes}
        configured_addresses = self._get_all_configured_addresses()
        configured_nodes = [n for n in all_nodes if n.address in configured_addresses]

        in_maintenance = self._maintenance_addresses(all_nodes)
        monitored = [n for n in configured_nodes if n.address not in in_maintenance]

        await self._apply_probe(monitored)

        raw_healthy = [n for n in monitored if n.is_healthy]
        raw_unhealthy = [n for n in monitored if not n.is_healthy]
        maintenance_note = f", {len(in_maintenance & configured_addresses)} in maintenance" if in_maintenance else ""
        self.logger.info(
            f"Nodes: {len(raw_healthy)}/{len(monitored)} online, {len(raw_unhealthy)} unhealthy{maintenance_note}"
        )
        if raw_unhealthy:
            details = "; ".join(f"{n.address} ({', '.join(n.unhealthy_reasons()) or 'unknown'})" for n in raw_unhealthy)
            self.logger.info(f"Unhealthy nodes: {details}")

        self._check_maintenance_transitions(nodes_by_address, in_maintenance)
        effective = self._update_effective_states(monitored, nodes_by_address, in_maintenance)

        effective_unhealthy = [n for n in monitored if not effective[n.address]]
        self._check_critical_state(monitored, effective_unhealthy)

        if self.paused:
            self.logger.warning(f"DNS changes paused ({self._pause_description()}), skipping DNS and host sync")
        else:
            active_fqdns, managed_fqdns = await self._sync_all_zones(effective)
            if self.host_manager:
                await self.host_manager.sync_host_states(active_fqdns, managed_fqdns)

        if self.summary:
            self.summary.record_cycle(
                (n.address, n.name, effective.get(n.address, False), n.address in in_maintenance)
                for n in configured_nodes
            )

        self.last_nodes = all_nodes
        self.last_cycle_at = time.time()
        self.logger.info("Health check cycle completed")

    # --- health-check outage tracking ---

    def _record_health_check_failure(self, msg: str) -> None:
        self.logger.error(f"Error during health check: {msg}")
        if self.summary:
            self.summary.record_failure()
        self._failed_cycles += 1
        self._last_error = msg
        if self._failing_since is not None:
            return  # already alerted for this outage

        self._failing_since = time.monotonic()
        if self.notifier and self.config.telegram_notify_errors:
            from .telegram import HealthCheckError

            self.notifier.notify_health_check_error(HealthCheckError(error_message=msg))

    def _record_health_check_success(self) -> None:
        if self._failing_since is None:
            return

        downtime = time.monotonic() - self._failing_since
        self.logger.info(
            f"Health check recovered after {self._failed_cycles} failed cycle(s) ({downtime:.0f}s)"
        )
        if self.notifier and self.config.telegram_notify_errors:
            from .telegram import HealthCheckRecovered

            self.notifier.notify_health_check_recovered(
                HealthCheckRecovered(
                    failed_cycles=self._failed_cycles,
                    downtime_seconds=downtime,
                    last_error=self._last_error,
                )
            )
        self._failed_cycles = 0
        self._failing_since = None
        self._last_error = ""

    # --- probing ---

    def _probe_targets(self) -> Dict[str, Set[Tuple[str, int]]]:
        """(ip, port) pairs to probe for each node address, from every zone it is in."""
        default_port = self.config.probe["port"]
        targets: Dict[str, Set[Tuple[str, int]]] = {}
        for zone in self.config.get_all_zones():
            port = zone["probe_port"] or default_port
            for entry in zone["nodes"]:
                targets.setdefault(entry["address"], set()).add((entry["ip"], port))
        return targets

    async def _apply_probe(self, nodes: List[NodeStatus]) -> None:
        settings = self.config.probe
        if not settings["enabled"] or not self.prober:
            return
        # Only nodes the panel already considers healthy can be failed by the probe.
        targets = self._probe_targets()
        candidates = [n for n in nodes if n.is_healthy and targets.get(n.address)]
        if not candidates:
            return
        wanted = set().union(*(targets[n.address] for n in candidates))
        results = await self.prober.probe_many(wanted, settings["timeout"], settings["attempts"])
        for node in candidates:
            failures = [
                f"{ip}:{port} ({results[(ip, port)]})"
                for ip, port in sorted(targets[node.address])
                if results.get((ip, port)) is not None
            ]
            if failures:
                node.probe_failures = failures
                node.is_healthy = False

    # --- maintenance ---

    def _maintenance_addresses(self, all_nodes: List[NodeStatus]) -> Set[str]:
        configured = {item.lower() for item in self.config.maintenance}
        if not configured:
            return set()
        result: Set[str] = set()
        matched: Set[str] = set()
        for node in all_nodes:
            hits = node_identifiers(node) & configured
            if hits:
                result.add(node.address)
                matched |= hits
        for item in sorted(configured - matched):
            if item not in self._unmatched_maintenance_warned:
                self._unmatched_maintenance_warned.add(item)
                self.logger.warning(f"Maintenance entry '{item}' matches no panel node (name, address or UUID)")
        self._unmatched_maintenance_warned &= configured
        return result

    def _check_maintenance_transitions(self, nodes_by_address: Dict[str, NodeStatus], current: Set[str]) -> None:
        started = sorted(current - self._maintenance)
        ended = sorted(self._maintenance - current)
        if not started and not ended:
            return

        for address in started:
            # Forget the pre-maintenance state; the node rejoins fresh afterwards.
            if self._previous_node_states.pop(address, None) is not None and self.state:
                self.state.mark_dirty()
            self._streaks.pop(address, None)
            self.logger.info(f"Node {address} entered maintenance: removed from DNS, alerts muted")

        ended_info = []
        for address in ended:
            node = nodes_by_address.get(address)
            self._streaks.pop(address, None)
            if node is not None:
                # Rejoin at the current raw state, without waiting out flap protection.
                self._record_node_state(address, node.is_healthy)
            ended_info.append({
                "name": node.name if node else address,
                "address": address,
                "healthy": node.is_healthy if node else None,
            })
            self.logger.info(f"Node {address} left maintenance")

        self._maintenance = set(current)
        if self.state:
            self.state.maintenance = sorted(current)
            self.state.mark_dirty()

        if self.notifier and self.config.telegram_notify_maintenance:
            from .telegram import MaintenanceChanged

            started_info = [
                {"name": nodes_by_address[a].name if a in nodes_by_address else a, "address": a}
                for a in started
            ]
            self.notifier.notify_maintenance_changed(MaintenanceChanged(started=started_info, ended=ended_info))

    def maintenance_entries(self) -> List[Dict[str, Any]]:
        """Configured maintenance entries, each with the panel node it matched."""
        result = []
        for item in self.config.maintenance:
            node = next((n for n in self.last_nodes if item.lower() in node_identifiers(n)), None)
            result.append({
                "entry": item,
                "name": node.name if node else None,
                "address": node.address if node else None,
            })
        return result

    def find_node(self, identifier: str) -> Optional[NodeStatus]:
        wanted = identifier.strip().lower()
        return next((n for n in self.last_nodes if wanted in node_identifiers(n)), None)

    # --- pause ---

    @property
    def paused(self) -> bool:
        return bool(self._pause)

    @property
    def pause_info(self) -> Dict[str, Any]:
        return dict(self._pause)

    def _pause_description(self) -> str:
        until = self._pause.get("until")
        by = self._pause.get("by") or "unknown"
        if until:
            return f"by {by} until {format_epoch(until, self.config)}"
        return f"by {by} until resumed"

    def _set_pause(self, value: Dict[str, Any]) -> None:
        # Mutate in place: self._pause is the same dict object the StateStore saves.
        self._pause.clear()
        self._pause.update(value)
        if self.state:
            self.state.pause = self._pause
            self.state.mark_dirty()
            self.state.save()

    def pause(self, duration_seconds: Optional[int], by: str, notify: bool = True):
        """Freeze DNS and host changes. Returns the DnsPaused event describing it."""
        from .telegram import DnsPaused

        now = time.time()
        until = now + duration_seconds if duration_seconds else None
        self._set_pause({"since": now, "until": until, "by": by})
        self.logger.warning(f"DNS changes paused {self._pause_description()}")
        event = DnsPaused(
            by=by,
            until=format_epoch(until, self.config) if until else None,
            duration_seconds=duration_seconds,
        )
        if notify and self.notifier and self.config.telegram_notify_maintenance:
            self.notifier.notify_dns_paused(event)
        return event

    def resume(self, by: str, notify: bool = True, automatic: bool = False):
        """Lift a pause. Returns the DnsResumed event, or None if not paused."""
        if not self.paused:
            return None
        from .telegram import DnsResumed

        since = self._pause.get("since") or time.time()
        self._set_pause({})
        self.logger.info(f"DNS changes resumed by {by}")
        event = DnsResumed(by=by, automatic=automatic, paused_seconds=time.time() - since)
        if notify and self.notifier and self.config.telegram_notify_maintenance:
            self.notifier.notify_dns_resumed(event)
        self.request_check()
        return event

    def _expire_pause(self) -> None:
        until = self._pause.get("until")
        if until and time.time() >= until:
            self.resume(by="timer", automatic=True)

    # --- status ---

    def status_snapshot(self) -> Dict[str, Any]:
        configured = self._get_all_configured_addresses()
        nodes = []
        for node in self.last_nodes:
            if node.address not in configured:
                continue
            in_maintenance = node.address in self._maintenance
            effective = self._previous_node_states.get(node.address, node.is_healthy)
            streak = self._streaks.get(node.address, 0)
            needed = self.config.fail_threshold if effective else self.config.recover_threshold
            nodes.append({
                "name": node.name,
                "address": node.address,
                "healthy": bool(effective) and not in_maintenance,
                "raw_healthy": node.is_healthy,
                "maintenance": in_maintenance,
                "pending": f"{streak}/{needed}" if streak and not in_maintenance else None,
                "reasons": node.unhealthy_reasons() if not node.is_healthy else [],
            })
        missing = sorted(configured - {n.address for n in self.last_nodes}) if self.last_nodes else []
        return {
            "last_cycle_at": self.last_cycle_at,
            "nodes": nodes,
            "missing": missing,
            "paused": {
                **self._pause,
                "until_text": format_epoch(self._pause["until"], self.config) if self._pause.get("until") else None,
            } if self.paused else None,
            "failing": {
                "cycles": self._failed_cycles,
                "seconds": time.monotonic() - self._failing_since,
                "error": self._last_error,
            } if self._failing_since is not None else None,
        }

    async def dns_overview(self) -> List[Dict[str, Any]]:
        """Current A records in Cloudflare for every configured zone."""
        result = []
        for zone in self.config.get_all_zones():
            fqdn = build_fqdn(zone["name"], zone["domain"])
            entry: Dict[str, Any] = {"fqdn": fqdn, "configured": len(zone["nodes"]), "records": [], "error": None}
            try:
                zone_id = await self._get_zone_id(zone["domain"])
                if not zone_id:
                    entry["error"] = "zone not found"
                else:
                    records = await self.cloudflare_client.get_dns_records(zone_id, name=fqdn, record_type="A")
                    entry["records"] = sorted(r["content"] for r in records)
            except Exception as e:
                entry["error"] = short_error(e)
            result.append(entry)
        return result

    # --- DNS ---

    def _get_all_configured_addresses(self) -> Set[str]:
        addresses: Set[str] = set()
        for zone in self.config.get_all_zones():
            for entry in zone["nodes"]:
                addresses.add(entry["address"])
        return addresses

    async def _sync_all_zones(self, effective: Dict[str, bool]) -> tuple[Set[str], Set[str]]:
        """Bring every zone in line with `effective` (address -> should be in DNS).
        Addresses missing from it (maintenance, gone from the panel) are left out."""
        active_fqdns: Set[str] = set()
        managed_fqdns: Set[str] = set()

        for zone in self.config.get_all_zones():
            domain = zone["domain"]

            zone_id = await self._get_zone_id(domain)
            if not zone_id:
                self.logger.warning(f"Could not find zone_id for domain {domain}, skipping")
                continue

            configured_ips: List[str] = []
            healthy_ips: Set[str] = set()
            for entry in zone["nodes"]:
                dns_ip = entry["ip"]
                configured_ips.append(dns_ip)
                if effective.get(entry["address"], False):
                    healthy_ips.add(dns_ip)

            full_domain = build_fqdn(zone["name"], domain)
            managed_fqdns.add(full_domain)

            active_ips = await self.dns_manager.sync_dns_records(
                zone_id=zone_id,
                zone_name=zone["name"],
                domain=domain,
                configured_ips=configured_ips,
                healthy_ips=healthy_ips,
                ttl=zone["ttl"],
                proxied=zone["proxied"],
            )

            if active_ips:
                active_fqdns.add(full_domain)

        return active_fqdns, managed_fqdns

    async def _get_zone_id(self, domain: str) -> Optional[str]:
        cached = self._zone_id_cache.get(domain)
        if cached is not None:
            zone_id, cached_at = cached
            if time.monotonic() - cached_at < _ZONE_CACHE_TTL_SECONDS:
                return zone_id
            self.logger.debug(f"Zone ID cache for {domain} expired, refreshing")

        zone_id = await self.cloudflare_client.get_zone_id_by_domain(domain)
        if zone_id:
            self._zone_id_cache[domain] = (zone_id, time.monotonic())
        else:
            self._zone_id_cache.pop(domain, None)

        return zone_id

    def invalidate_zone_id(self, domain: str) -> None:
        """Drop a cached zone ID — call when a domain is removed from config so a
        later re-add cannot reuse a stale ID for the process lifetime."""
        if self._zone_id_cache.pop(domain, None) is not None:
            self.logger.debug(f"Invalidated cached zone ID for {domain}")

    # --- node transitions ---

    def _update_effective_states(
            self, nodes: List[NodeStatus], nodes_by_address: Dict[str, NodeStatus], in_maintenance: Set[str]
    ) -> Dict[str, bool]:
        """Apply flap protection, notify on effective transitions, and return
        address -> effective health for every node in `nodes`.

        Tracking runs even when notifications are off: this state is what
        survives a restart, and gating it on the notifier meant a Telegram-less
        deployment never recorded node history at all.
        """
        fail_threshold = self.config.fail_threshold
        recover_threshold = self.config.recover_threshold

        effective: Dict[str, bool] = {}
        transitions: List[NodeStatus] = []
        previous: Dict[str, bool] = {}

        for node in nodes:
            address = node.address
            raw = node.is_healthy
            prev = self._previous_node_states.get(address)

            if prev is None:
                # First time we have seen this node — record it without alerting.
                self._record_node_state(address, raw)
                self._streaks.pop(address, None)
                effective[address] = raw
                continue

            previous[address] = prev
            if raw == prev:
                if self._streaks.pop(address, None):
                    self.logger.info(f"Node {address} is back to {'healthy' if raw else 'unhealthy'}, change cancelled")
                effective[address] = prev
                continue

            streak = self._streaks.get(address, 0) + 1
            needed = fail_threshold if prev else recover_threshold
            if streak < needed:
                self._streaks[address] = streak
                self.logger.info(
                    f"Node {address} looks {'unhealthy' if prev else 'healthy'} "
                    f"({streak}/{needed} cycles), keeping it {'in' if prev else 'out of'} DNS for now"
                )
                effective[address] = prev
                continue

            self._streaks.pop(address, None)
            self._record_node_state(address, raw)
            effective[address] = raw
            transitions.append(node)

        # Streaks for nodes no longer monitored (maintenance, removed) are stale.
        monitored = {n.address for n in nodes}
        for address in list(self._streaks):
            if address not in monitored:
                del self._streaks[address]

        if transitions and self.notifier and self.config.telegram_notify_node_changes:
            self._notify_transitions(nodes, nodes_by_address, in_maintenance, previous, transitions)

        return effective

    def _notify_transitions(self, nodes, nodes_by_address, in_maintenance, previous, transitions) -> None:
        from .telegram import NodeStateChange, NodeStats, ZoneStats

        total = len(nodes)
        disabled = sum(1 for n in nodes if n.is_disabled)

        zone_nodes: Dict[str, list] = {}
        for zone in self.config.get_all_zones():
            key = build_fqdn(zone['name'], zone['domain'])
            seen: Set[str] = set()
            zone_node_list = []
            for entry in zone["nodes"]:
                node = nodes_by_address.get(entry["address"])
                if node and node.address not in seen and node.address not in in_maintenance:
                    zone_node_list.append(node)
                    seen.add(node.address)
            zone_nodes[key] = zone_node_list

        # Counts start from the state before this cycle and move one transition at
        # a time, so each message shows the totals as of that change.
        def before(n) -> bool:
            return previous.get(n.address, self._previous_node_states.get(n.address, False))

        online = sum(1 for n in nodes if before(n))
        zone_online: Dict[str, int] = {key: sum(1 for n in znodes if before(n)) for key, znodes in zone_nodes.items()}

        for node in transitions:
            curr_healthy = node.is_healthy
            delta = 1 if curr_healthy else -1
            online += delta

            zones_stats = []
            for key, znodes in zone_nodes.items():
                if any(n.address == node.address for n in znodes):
                    zone_online[key] += delta
                    zones_stats.append(ZoneStats(
                        name=key,
                        total=len(znodes),
                        online=zone_online[key],
                        offline=len(znodes) - zone_online[key],
                    ))

            stats = NodeStats(total=total, online=online, offline=total - online, disabled=disabled, zones=zones_stats)
            reason = None
            if not curr_healthy:
                reason = ", ".join(node.unhealthy_reasons()) or "unknown"

            self.notifier.notify_node_state_change(
                NodeStateChange(
                    node_name=node.name,
                    node_address=node.address,
                    previous_healthy=not curr_healthy,
                    current_healthy=curr_healthy,
                    stats=stats,
                    reason=reason,
                )
            )

    def _record_node_state(self, address: str, healthy: bool) -> None:
        if self._previous_node_states.get(address) != healthy:
            self._previous_node_states[address] = healthy
            if self.state:
                self.state.mark_dirty()

    async def cleanup_zone(self, domain: str, zone_name: str) -> None:
        zone_id = await self._get_zone_id(domain)
        if not zone_id:
            self.logger.warning(f"Cannot cleanup {build_fqdn(zone_name, domain)}: zone_id not found")
            return
        await self.dns_manager.cleanup_zone(zone_id, zone_name, domain)

    async def cleanup_domain(self, domain: str) -> None:
        zone_id = await self._get_zone_id(domain)
        if not zone_id:
            self.logger.warning(f"Cannot cleanup domain {domain}: zone_id not found")
            return
        for domain_conf in self.config.domains:
            if domain_conf.get("domain") == domain:
                for zone in domain_conf.get("zones") or []:
                    await self.dns_manager.cleanup_zone(zone_id, zone["name"], domain)
                break
        self.invalidate_zone_id(domain)

    def _check_critical_state(self, monitored_nodes, unhealthy_nodes) -> None:
        all_down = 0 < len(monitored_nodes) == len(unhealthy_nodes)

        if self.notifier and self.config.telegram_notify_critical:
            if all_down and not self._previous_all_down:
                from .telegram import CriticalState

                self.notifier.notify_critical_state(
                    CriticalState(total_nodes=len(monitored_nodes), down_nodes=[n.address for n in unhealthy_nodes])
                )
            elif not all_down and self._previous_all_down:
                from .telegram import CriticalStateRecovered

                online_count = len(monitored_nodes) - len(unhealthy_nodes)
                self.notifier.notify_critical_recovered(
                    CriticalStateRecovered(total_nodes=len(monitored_nodes), online_nodes=online_count)
                )

        self._previous_all_down = all_down
