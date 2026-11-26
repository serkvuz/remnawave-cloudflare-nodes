from typing import List

from remnawave.models import NodeResponseDto

from .client import RemnawaveClient
from ..utils import get_logger, short_error


class NodeStatus:
    """A panel node plus the monitor's verdict on it.

    `is_healthy` is this cycle's raw verdict: the panel's view, AND-ed with the
    TCP probe when probing is enabled. Flap protection and maintenance are
    applied on top of it by MonitoringService, which decides what goes in DNS.
    """

    def __init__(self, node: NodeResponseDto, is_healthy: bool):
        self.node = node
        self.panel_healthy = is_healthy
        self.is_healthy = is_healthy
        # Set by the prober: failing "ip:port (reason)" targets, empty when reachable.
        self.probe_failures: List[str] = []

    def unhealthy_reasons(self) -> List[str]:
        reasons = []
        if not self.node.is_connected:
            reasons.append("disconnected")
        if self.node.is_disabled:
            reasons.append("disabled")
        if not self.node.xray_version:
            reasons.append("no xray")
        if self.probe_failures:
            reasons.append("unreachable " + ", ".join(self.probe_failures))
        return reasons

    def __getattr__(self, name: str):
        return getattr(self.node, name)

    def __repr__(self):
        status = "healthy" if self.is_healthy else "unhealthy"
        return f"NodeStatus(name={self.node.name}, address={self.node.address}, status={status})"


class NodeMonitor:
    def __init__(self, client: RemnawaveClient):
        self.client = client
        self.logger = get_logger(__name__)

    async def check_all_nodes(self) -> List[NodeStatus]:
        try:
            nodes = await self.client.get_nodes()
            node_statuses = []

            for node in nodes:
                status = NodeStatus(
                    node=node,
                    is_healthy=self.client.is_node_healthy(node),
                )
                node_statuses.append(status)

                self.logger.debug(f"Node {node.name} ({node.address}): {status}")

            healthy_count = sum(1 for s in node_statuses if s.is_healthy)
            unhealthy_count = len(node_statuses) - healthy_count
            self.logger.info(f"Fetched {len(node_statuses)} nodes: {healthy_count} online, {unhealthy_count} unhealthy")

            return node_statuses

        except Exception as e:
            self.logger.error(f"Error checking nodes: {short_error(e)}")
            raise

    async def get_healthy_nodes(self) -> List[NodeStatus]:
        all_nodes = await self.check_all_nodes()
        return [node for node in all_nodes if node.is_healthy]

    async def get_unhealthy_nodes(self) -> List[NodeStatus]:
        all_nodes = await self.check_all_nodes()
        return [node for node in all_nodes if not node.is_healthy]

    async def get_node_addresses(self, only_healthy: bool = True) -> List[str]:
        if only_healthy:
            nodes = await self.get_healthy_nodes()
        else:
            nodes = await self.check_all_nodes()

        return [node.address for node in nodes if node.address]
