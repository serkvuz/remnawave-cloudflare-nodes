import asyncio
import ipaddress
import socket
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from .config import Config
from .utils import build_fqdn, get_logger, short_error

if TYPE_CHECKING:
    from .panel import RemnawaveClient
    from .panel.monitor import NodeStatus
    from .telegram import TelegramNotifier


class NodeDiscovery:
    """Adds panel nodes that are missing from config.yml.

    Enabled with `auto-discovery.enabled: true` in config.yml. Two sources:

    1. Panel hosts bound to specific nodes. A host whose address falls under a
       configured domain (e.g. `s3.example.com` under `example.com`) puts each
       of its bound nodes into the zone for that address, creating the zone if
       it does not exist yet.
    2. `auto-discovery.default-zone`. Any panel node that is in no zone at all
       after step 1 is added to this FQDN.

    Discovery only ever adds. It never removes nodes or zones, so a node taken
    out of config.yml by hand comes back if the panel still binds it to a host:
    unbind it from the host in the panel instead.

    The DNS IP is the node address when that is a public IP, otherwise the
    address is resolved to an IPv4. Nodes that only resolve to private, CGNAT
    or Tailscale addresses are skipped, since that IP would be useless in
    public DNS; add those by hand with the `nodes:` (ip + address) format.
    """

    def __init__(
        self,
        config: Config,
        client: "RemnawaveClient",
        notifier: Optional["TelegramNotifier"] = None,
    ):
        self.config = config
        self.client = client
        self.notifier = notifier
        self.logger = get_logger(__name__)
        # Warn once per node/reason rather than every cycle.
        self._warned: Set[Tuple[str, str]] = set()

    def _warn_once(self, key: str, reason: str, message: str) -> None:
        if (key, reason) in self._warned:
            return
        self._warned.add((key, reason))
        self.logger.warning(message)

    async def discover(self, nodes: List["NodeStatus"]) -> list:
        settings = self.config.auto_discovery
        if not settings["enabled"] or not nodes:
            return []

        try:
            additions = await self._plan(nodes, settings)
        except Exception as e:
            self.logger.error(f"Auto-discovery failed, config left unchanged: {short_error(e)}")
            return []

        if not additions:
            return []

        try:
            applied = self.config.add_discovered_nodes(
                additions, ttl=settings["ttl"], proxied=settings["proxied"]
            )
        except Exception as e:
            self.logger.error(f"Auto-discovery could not write config.yml: {short_error(e)}")
            # Drop the half-applied in-memory changes and go back to the file.
            try:
                self.config.reload()
            except Exception as reload_error:
                self.logger.error(f"Config reload after failed write also failed: {short_error(reload_error)}")
            return []

        for a in applied:
            note = " (new zone)" if a["new_zone"] else ""
            self.logger.info(f"Auto-discovery: added node {a['name']} ({a['ip']}) to {a['fqdn']}{note}")

        if applied and self.notifier and self.config.telegram_notify_discovery:
            from .telegram import NodesDiscovered

            self.notifier.notify_nodes_discovered(NodesDiscovered(additions=applied))

        return applied

    async def _plan(self, nodes: List["NodeStatus"], settings: Dict) -> list:
        nodes_by_uuid = {str(n.uuid).lower(): n for n in nodes}

        # (fqdn -> set of addresses) already in config
        zone_addresses: Dict[str, Set[str]] = {}
        for zone in self.config.get_all_zones():
            fqdn = build_fqdn(zone["name"], zone["domain"]).lower()
            zone_addresses.setdefault(fqdn, set()).update(e["address"] for e in zone["nodes"])

        wanted: List[Tuple[str, "NodeStatus"]] = []  # (fqdn, node)

        hosts = await self.client.get_hosts()
        for host in hosts:
            if not host.node_uuids:
                continue
            fqdn = host.address.strip().lower().rstrip(".")
            if self.config.split_fqdn(fqdn) is None:
                continue  # not under a domain this service manages
            for node_uuid in host.node_uuids:
                node = nodes_by_uuid.get(node_uuid)
                if node and node.address not in zone_addresses.get(fqdn, set()):
                    wanted.append((fqdn, node))

        default_zone = settings["default-zone"]
        if default_zone:
            if self.config.split_fqdn(default_zone) is None:
                self._warn_once(
                    default_zone, "default-zone",
                    f"auto-discovery.default-zone '{default_zone}' is not under any configured domain, ignoring it",
                )
            else:
                configured = set().union(*zone_addresses.values()) if zone_addresses else set()
                planned = {n.address for _, n in wanted}
                for node in nodes:
                    if node.address not in configured and node.address not in planned:
                        wanted.append((default_zone, node))

        additions = []
        seen: Set[Tuple[str, str]] = set()
        for fqdn, node in wanted:
            if (fqdn, node.address) in seen:
                continue
            seen.add((fqdn, node.address))

            ip = await self._public_ip(node.address)
            if ip is None:
                continue
            domain, zone = self.config.split_fqdn(fqdn)
            additions.append({
                "domain": domain,
                "zone": zone,
                "fqdn": fqdn,
                "ip": ip,
                "address": node.address,
                "name": node.name,
            })
        return additions

    async def _public_ip(self, address: str) -> Optional[str]:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            ip = None

        if ip is None:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    address, None, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            except OSError as e:
                self._warn_once(address, "resolve",
                                f"Auto-discovery: cannot resolve node address {address}, skipping: {e}")
                return None
            candidates = [ipaddress.ip_address(info[4][0]) for info in infos]
            public = [c for c in candidates if c.is_global]
            if not public:
                self._warn_once(address, "private",
                                f"Auto-discovery: node address {address} has no public IPv4, skipping "
                                f"(add it by hand with the nodes: ip/address format)")
                return None
            return str(public[0])

        if ip.version != 4 or not ip.is_global:
            self._warn_once(address, "private",
                            f"Auto-discovery: node address {address} is not a public IPv4, skipping "
                            f"(add it by hand with the nodes: ip/address format)")
            return None
        return str(ip)
