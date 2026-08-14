from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from remnawave import RemnawaveSDK
from remnawave.models import NodeResponseDto

from ..utils import get_logger, short_error


@dataclass(frozen=True)
class PanelHost:
    """The host fields this service actually uses.

    Deliberately parsed by hand instead of through the SDK's HostResponseDto.
    That model tracks the panel schema field-for-field and marks most fields
    required, so a single renamed field the monitor never reads (panel v2.8
    renamed `xHttpExtraParams` to `xhttpExtraParams` and `tag` to `tags`) fails
    validation for the entire host list and silently disables host management.
    Reading only these four fields makes host sync immune to schema drift
    everywhere else in the payload.
    """

    uuid: str
    address: str
    remark: str
    is_disabled: bool

    @classmethod
    def from_payload(cls, item: Dict[str, Any]) -> Optional["PanelHost"]:
        uuid = item.get("uuid")
        address = item.get("address")
        if not uuid or not address:
            return None
        return cls(
            uuid=str(uuid),
            address=str(address),
            remark=str(item.get("remark") or address),
            is_disabled=bool(item.get("isDisabled", False)),
        )


class RemnawaveClient:
    def __init__(self, api_url: str, api_key: str, timeout: float = 30.0):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.logger = get_logger(__name__)
        self.sdk = RemnawaveSDK(base_url=self.api_url, token=self.api_key)
        self._http = httpx.AsyncClient(
            base_url=f"{self.api_url}/api",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_nodes(self) -> List[NodeResponseDto]:
        try:
            self.logger.info(f"Fetching nodes from {self.api_url}")

            response = await self.sdk.nodes.get_all_nodes()  # GetAllNodesResponseDto
            nodes_list = response.root if hasattr(response, "root") else []

            self.logger.info(f"Successfully fetched {len(nodes_list)} nodes")
            return nodes_list
        except Exception as e:
            self.logger.error(f"Error fetching nodes: {short_error(e)}")
            raise

    @staticmethod
    def _unwrap(data: Any) -> List[Dict[str, Any]]:
        """Panel responses arrive either bare or wrapped in a `response` envelope."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            inner = data.get("response", data)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
            if isinstance(inner, dict) and isinstance(inner.get("hosts"), list):
                return [item for item in inner["hosts"] if isinstance(item, dict)]
        return []

    def _parse_hosts(self, data: Any) -> List[PanelHost]:
        items = self._unwrap(data)
        hosts = []
        skipped = 0
        for item in items:
            host = PanelHost.from_payload(item)
            if host is None:
                skipped += 1
                continue
            hosts.append(host)
        if skipped:
            self.logger.warning(f"Skipped {skipped} host entr(ies) missing uuid or address")
        return hosts

    async def get_hosts(self) -> List[PanelHost]:
        try:
            self.logger.info("Fetching hosts from Remnawave")
            response = await self._http.get("/hosts")
            response.raise_for_status()
            hosts_list = self._parse_hosts(response.json())
            self.logger.info(f"Successfully fetched {len(hosts_list)} hosts")
            return hosts_list
        except Exception as e:
            self.logger.error(f"Error fetching hosts: {short_error(e)}")
            raise

    async def _bulk_hosts_action(self, uuids: List[str], action: str) -> List[PanelHost]:
        if not uuids:
            raise ValueError("No host UUIDs provided")
        self.logger.info(f"{action.capitalize()}ing {len(uuids)} hosts")
        try:
            response = await self._http.post(
                f"/hosts/bulk/{action}",
                json={"uuids": [str(u) for u in uuids]},
            )
            response.raise_for_status()
            hosts_list = self._parse_hosts(response.json())
            self.logger.info(f"Successfully {action}d {len(uuids)} hosts")
            return hosts_list
        except Exception as e:
            self.logger.error(f"Error {action}ing hosts: {short_error(e)}")
            raise

    async def disable_hosts(self, uuids: List[str]) -> List[PanelHost]:
        return await self._bulk_hosts_action(uuids, "disable")

    async def enable_hosts(self, uuids: List[str]) -> List[PanelHost]:
        return await self._bulk_hosts_action(uuids, "enable")

    @staticmethod
    def is_node_connected(node: NodeResponseDto) -> bool:
        return node.is_connected

    @staticmethod
    def is_node_disabled(node: NodeResponseDto) -> bool:
        return node.is_disabled

    @staticmethod
    def is_node_healthy(node: NodeResponseDto) -> bool:
        return RemnawaveClient.is_node_connected(node) and not RemnawaveClient.is_node_disabled(node)
