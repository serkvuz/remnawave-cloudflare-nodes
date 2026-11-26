from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ZoneStats:
    name: str
    total: int
    online: int
    offline: int


@dataclass
class NodeStats:
    total: int
    online: int
    offline: int
    disabled: int
    zones: List["ZoneStats"]


@dataclass
class NodeStateChange:
    node_name: str
    node_address: str
    previous_healthy: bool
    current_healthy: bool
    stats: Optional[NodeStats] = None
    reason: Optional[str] = None


@dataclass
class DNSChange:
    domain: str
    zone_name: str
    ip_address: str
    action: str  # "added" or "removed"


@dataclass
class DNSError:
    domain: str
    zone_name: str
    ip_address: str
    action: str
    error_message: str


@dataclass
class CriticalState:
    total_nodes: int
    down_nodes: List[str]


@dataclass
class CriticalStateRecovered:
    total_nodes: int
    online_nodes: int


@dataclass
class HealthCheckError:
    error_message: str


@dataclass
class HealthCheckRecovered:
    failed_cycles: int
    downtime_seconds: float
    last_error: str


@dataclass
class MaintenanceChanged:
    started: List[dict]  # each: name, address
    ended: List[dict]    # each: name, address, healthy (bool or None if gone from the panel)


@dataclass
class DnsPaused:
    by: str
    until: Optional[str]  # formatted time, None = until resumed
    duration_seconds: Optional[int] = None


@dataclass
class DnsResumed:
    by: str
    automatic: bool
    paused_seconds: float


@dataclass
class SummaryReport:
    kind: str  # "daily", "weekly" or "current"
    period_start: str
    period_end: str
    nodes: List[dict]  # each: name, address, uptime (float % or None), outages, longest (s), ongoing
    longest: Optional[dict]
    dns_added: int
    dns_removed: int
    check_cycles: int
    check_failures: int


@dataclass
class NodesDiscovered:
    # each dict has: name, ip, address, fqdn, new_zone (bool)
    additions: List[dict]


@dataclass
class ServiceStarted:
    domains: List[dict]
    api_enabled: bool = False
    api_host: str = ""
    api_port: int = 0


@dataclass
class HostSyncFailure:
    failures: int
    error_message: str


@dataclass
class HostStateChange:
    changes: List[dict]  # each dict has: remark, address, action ("enabled" or "disabled")
    # Grouped by address for better formatting: {address: {"action": "enabled|disabled", "remarks": [...]}}
    grouped: Optional[dict] = None


# API events

@dataclass
class ApiConfigUpdated:
    changes: List[str]
    client_ip: str


@dataclass
class ApiDomainAdded:
    domain: str
    zones: List[dict]
    client_ip: str


@dataclass
class ApiDomainRemoved:
    domain: str
    client_ip: str


@dataclass
class ApiZoneAdded:
    domain: str
    zone_name: str
    ips: List[str]
    ttl: int
    proxied: bool
    client_ip: str


@dataclass
class ApiZoneUpdated:
    domain: str
    zone_name: str
    changes: dict
    client_ip: str


@dataclass
class ApiZoneRemoved:
    domain: str
    zone_name: str
    client_ip: str
