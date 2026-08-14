import time
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from .cloudflare_dns import CloudflareClient, DNSManager
from .config import Config
from .panel import HostManager, NodeMonitor
from .utils import build_fqdn, get_logger, short_error

if TYPE_CHECKING:
    from .state import StateStore
    from .telegram import TelegramNotifier

# Zone IDs are stable for the life of a zone, but a zone deleted and recreated
# in Cloudflare gets a new one. Expiring the cache bounds how long a stale ID
# can be used when the zone changes outside this service.
_ZONE_CACHE_TTL_SECONDS = 3600


class MonitoringService:
    def __init__(
            self,
            config: Config,
            node_monitor: NodeMonitor,
            cloudflare_client: CloudflareClient,
            dns_manager: DNSManager,
            host_manager: Optional["HostManager"] = None,
            notifier: Optional["TelegramNotifier"] = None,
            state: Optional["StateStore"] = None,
    ):
        self.config = config
        self.node_monitor = node_monitor
        self.cloudflare_client = cloudflare_client
        self.dns_manager = dns_manager
        self.host_manager = host_manager
        self.notifier = notifier
        self.state = state
        self.logger = get_logger(__name__)
        self._zone_id_cache: Dict[str, Tuple[str, float]] = {}
        self._previous_node_states: Dict[str, bool] = state.node_states if state else {}
        self._previous_all_down: bool = False

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

        self.logger.info("Initialization complete")

    async def perform_health_check(self) -> None:
        self.logger.info("Starting health check cycle")

        try:
            all_nodes = await self.node_monitor.check_all_nodes()
            nodes_by_address = {node.address: node for node in all_nodes}

            configured_addresses = self._get_all_configured_addresses()
            configured_nodes = [n for n in all_nodes if n.address in configured_addresses]
            healthy_nodes = [n for n in configured_nodes if n.is_healthy]
            unhealthy_nodes = [n for n in configured_nodes if not n.is_healthy]

            self.logger.info(
                f"Nodes: {len(healthy_nodes)}/{len(configured_nodes)} online, {len(unhealthy_nodes)} unhealthy"
            )

            if unhealthy_nodes:
                unhealthy_info = []
                for node in unhealthy_nodes:
                    reason = []
                    if not node.is_connected:
                        reason.append("disconnected")
                    if node.is_disabled:
                        reason.append("disabled")
                    if not node.xray_version:
                        reason.append("no xray")
                    unhealthy_info.append(f"{node.address} ({', '.join(reason)})")
                self.logger.info(f"Unhealthy nodes: {'; '.join(unhealthy_info)}")

            self._check_node_transitions(configured_nodes, nodes_by_address)
            self._check_critical_state(configured_nodes, unhealthy_nodes)

            active_fqdns, managed_fqdns = await self._sync_all_zones(nodes_by_address)

            if self.host_manager:
                await self.host_manager.sync_host_states(active_fqdns, managed_fqdns)

            if self.state:
                self.state.save()

            self.logger.info("Health check cycle completed")

        except Exception as e:
            msg = short_error(e)
            self.logger.error(f"Error during health check: {msg}")
            if self.notifier and self.config.telegram_notify_errors:
                from .telegram import HealthCheckError

                self.notifier.notify_health_check_error(HealthCheckError(error_message=msg))
            raise

    def _get_all_configured_addresses(self) -> Set[str]:
        addresses: Set[str] = set()
        for zone in self.config.get_all_zones():
            for entry in zone["nodes"]:
                addresses.add(entry["address"])
        return addresses

    async def _sync_all_zones(self, nodes_by_address: Dict[str, object]) -> tuple[Set[str], Set[str]]:
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
                node = nodes_by_address.get(entry["address"])
                if node and node.is_healthy:
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

    def _check_node_transitions(self, nodes, nodes_by_address: Dict[str, object]) -> None:
        """Track node health transitions and notify on each one.

        Transition tracking runs even when notifications are off: this state is
        what survives a restart, and gating it on the notifier meant a
        Telegram-less deployment never recorded node history at all.
        """
        notify = bool(self.notifier and self.config.telegram_notify_node_changes)

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
                if node and node.address not in seen:
                    zone_node_list.append(node)
                    seen.add(node.address)
            zone_nodes[key] = zone_node_list

        online = sum(1 for n in nodes if self._previous_node_states.get(n.address, n.is_healthy))
        zone_online: Dict[str, int] = {
            key: sum(1 for n in znodes if self._previous_node_states.get(n.address, n.is_healthy))
            for key, znodes in zone_nodes.items()
        }

        for node in nodes:
            prev_healthy = self._previous_node_states.get(node.address)
            curr_healthy = node.is_healthy

            if prev_healthy is None:
                # First time we have seen this node — record it without alerting.
                self._record_node_state(node.address, curr_healthy)
                continue

            if prev_healthy == curr_healthy:
                continue

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

            if notify:
                stats = NodeStats(
                    total=total,
                    online=online,
                    offline=total - online,
                    disabled=disabled,
                    zones=zones_stats,
                )

                reason = None
                if not curr_healthy:
                    reasons = []
                    if not node.is_connected:
                        reasons.append("disconnected")
                    if node.is_disabled:
                        reasons.append("disabled")
                    if not node.xray_version:
                        reasons.append("no xray")
                    reason = ", ".join(reasons) if reasons else "unknown"

                self.notifier.notify_node_state_change(
                    NodeStateChange(
                        node_name=node.name,
                        node_address=node.address,
                        previous_healthy=prev_healthy,
                        current_healthy=curr_healthy,
                        stats=stats,
                        reason=reason,
                    )
                )

            self._record_node_state(node.address, curr_healthy)

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

    def _check_critical_state(self, configured_nodes, unhealthy_nodes) -> None:
        if not self.notifier or not self.config.telegram_notify_critical:
            return

        all_down = 0 < len(configured_nodes) == len(unhealthy_nodes)

        if all_down and not self._previous_all_down:
            from .telegram import CriticalState

            self.notifier.notify_critical_state(
                CriticalState(total_nodes=len(configured_nodes), down_nodes=[n.address for n in unhealthy_nodes])
            )
        elif not all_down and self._previous_all_down:
            from .telegram import CriticalStateRecovered

            online_count = len(configured_nodes) - len(unhealthy_nodes)
            self.notifier.notify_critical_recovered(
                CriticalStateRecovered(total_nodes=len(configured_nodes), online_nodes=online_count)
            )

        self._previous_all_down = all_down
