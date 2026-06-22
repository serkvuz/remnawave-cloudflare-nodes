from typing import List

from remnawave.models import NodeResponseDto

from .client import RemnawaveClient
from ..utils import get_logger, short_error


class NodeStatus:
    def __init__(self, node: NodeResponseDto, is_healthy: bool):
        self.node = node
        self.is_healthy = is_healthy

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
