from .events import (
    NodeStateChange, NodeStats, DNSChange, DNSError,
    CriticalState, CriticalStateRecovered, HealthCheckError,
    HealthCheckRecovered, NodesDiscovered, MaintenanceChanged, DnsPaused, DnsResumed, SummaryReport,
    ServiceStarted, HostSyncFailure,
    ApiConfigUpdated, ApiDomainAdded, ApiDomainRemoved,
    ApiZoneAdded, ApiZoneUpdated, ApiZoneRemoved,
)
import html
from typing import Any, Dict, List, Optional

from ..i18n import get_translator
from ..utils.time import format_duration
from ..utils.dns import build_fqdn
from ..utils.logger import get_logger


class MessageFormatter:
    def __init__(self):
        self.logger = get_logger(__name__)
        self._i18n = get_translator()

    def _format_node_stats(self, stats: "NodeStats") -> str:
        line = self._i18n.get("node-stats-line",
                              online=stats.online,
                              total=stats.total,
                              offline=stats.offline,
                              disabled=stats.disabled)

        if stats.zones:
            zone_lines = [
                self._i18n.get("node-zone-line",
                               zone=z.name,
                               online=z.online,
                               total=z.total,
                               offline=z.offline)
                for z in stats.zones
            ]
            line += "\n" + "\n".join(zone_lines)

        return line

    def format_node_state_change(self, change: NodeStateChange) -> str:
        stats = change.stats or NodeStats(total=0, online=0, offline=0, disabled=0, zones=[])
        stats_str = self._format_node_stats(stats)

        if change.current_healthy:
            return self._i18n.get(
                "node-became-healthy",
                name=change.node_name,
                address=change.node_address,
                stats=stats_str,
            )
        else:
            return self._i18n.get(
                "node-became-unhealthy",
                name=change.node_name,
                address=change.node_address,
                reason=change.reason or self._i18n.get("node-reason-unknown"),
                stats=stats_str,
            )

    def format_dns_change(self, change: DNSChange) -> str:
        msg_id = "dns-record-added" if change.action == "added" else "dns-record-removed"
        return self._i18n.get(
            msg_id, domain=build_fqdn(change.zone_name, change.domain), ip=change.ip_address
        )

    def format_dns_error(self, error: DNSError) -> str:
        return self._i18n.get(
            "dns-operation-error",
            domain=build_fqdn(error.zone_name, error.domain),
            ip=error.ip_address,
            action=error.action,
            error=error.error_message,
        )

    def format_critical_state(self, state: CriticalState) -> str:
        return self._i18n.get(
            "all-nodes-down", total=state.total_nodes, nodes=", ".join(state.down_nodes)
        )

    def format_critical_recovered(self, state: CriticalStateRecovered) -> str:
        return self._i18n.get(
            "all-nodes-recovered", total=state.total_nodes, online=state.online_nodes
        )

    def format_health_check_error(self, error: HealthCheckError) -> str:
        return self._i18n.get("health-check-error", error=html.escape(error.error_message))

    def format_health_check_recovered(self, event: HealthCheckRecovered) -> str:
        return self._i18n.get(
            "health-check-recovered",
            cycles=event.failed_cycles,
            duration=format_duration(event.downtime_seconds),
            error=html.escape(event.last_error),
        )

    # --- maintenance and pause ---

    def format_maintenance_changed(self, event: MaintenanceChanged) -> str:
        e = html.escape
        lines = [
            self._i18n.get("maintenance-started-line", name=e(n["name"]), address=e(n["address"]))
            for n in event.started
        ]
        for n in event.ended:
            if n["healthy"] is None:
                msg_id = "maintenance-ended-gone"
            elif n["healthy"]:
                msg_id = "maintenance-ended-healthy"
            else:
                msg_id = "maintenance-ended-unhealthy"
            lines.append(self._i18n.get(msg_id, name=e(n["name"]), address=e(n["address"])))
        return self._i18n.get("maintenance-changed", changes="\n".join(lines))

    def format_dns_paused(self, event: DnsPaused) -> str:
        if event.until:
            until = self._i18n.get("dns-paused-until", until=event.until)
        else:
            until = self._i18n.get("dns-paused-indefinite")
        return self._i18n.get("dns-paused", by=html.escape(event.by), until=until)

    def format_dns_resumed(self, event: DnsResumed) -> str:
        duration = format_duration(event.paused_seconds)
        if event.automatic:
            return self._i18n.get("dns-resumed-auto", duration=duration)
        return self._i18n.get("dns-resumed", by=html.escape(event.by), duration=duration)

    # --- summary ---

    def format_summary(self, report: SummaryReport) -> str:
        e = html.escape
        parts = [
            self._i18n.get(f"summary-{report.kind}"),
            self._i18n.get("summary-period", start=report.period_start, end=report.period_end),
            "",
        ]
        if not report.nodes:
            parts.append(self._i18n.get("summary-no-nodes"))
        for n in report.nodes:
            name = e(n["name"] or n["address"])
            if n["uptime"] is None:
                parts.append(self._i18n.get("summary-node-no-data", name=name))
                continue
            uptime = self._format_percent(n["uptime"])
            if not n["outages"]:
                parts.append(self._i18n.get("summary-node-clean", name=name, uptime=uptime))
                continue
            line = self._i18n.get(
                "summary-node-line", name=name, uptime=uptime, outages=n["outages"],
                longest=format_duration(n["longest"]),
            )
            if n["ongoing"]:
                line += " " + self._i18n.get("summary-ongoing")
            parts.append(line)
        parts.append("")
        if report.longest:
            parts.append(self._i18n.get(
                "summary-longest", name=e(report.longest["name"] or report.longest["address"]),
                duration=format_duration(report.longest["longest"]),
            ))
        else:
            parts.append(self._i18n.get("summary-no-outages"))
        parts.append(self._i18n.get("summary-dns", added=report.dns_added, removed=report.dns_removed))
        parts.append(self._i18n.get("summary-checks", failures=report.check_failures, cycles=report.check_cycles))
        return "\n".join(parts)

    @staticmethod
    def _format_percent(value: float) -> str:
        if value >= 100:
            return "100%"
        # Never round 99.996% up to 100%: that hides an outage.
        return f"{int(value * 100) / 100:.2f}%"

    # --- /status ---

    def format_status(self, snapshot: Dict[str, Any], dns: Optional[List[Dict[str, Any]]], last_check: Optional[str]) -> str:
        e = html.escape
        parts = [self._i18n.get("status-title")]
        parts.append(self._i18n.get("status-last-check", time=last_check) if last_check
                     else self._i18n.get("status-never"))

        failing = snapshot.get("failing")
        if failing:
            parts.append(self._i18n.get(
                "status-failing", duration=format_duration(failing["seconds"]),
                cycles=failing["cycles"], error=e(failing["error"]),
            ))
        paused = snapshot.get("paused")
        if paused:
            until = (self._i18n.get("status-paused-until", time=paused["until_text"]) if paused.get("until_text")
                     else self._i18n.get("status-paused-indefinite"))
            parts.append(self._i18n.get("status-paused", by=e(str(paused.get("by") or "?")), until=until))

        nodes = snapshot.get("nodes") or []
        in_dns = sum(1 for n in nodes if n["healthy"])
        monitored = sum(1 for n in nodes if not n["maintenance"])
        parts += ["", self._i18n.get("status-nodes-header", online=in_dns, total=monitored)]
        for n in sorted(nodes, key=lambda n: (n["healthy"], n["name"].lower())):
            args = {"name": e(n["name"]), "address": e(n["address"])}
            reasons = e(", ".join(n["reasons"]) or self._i18n.get("node-reason-unknown"))
            if n["maintenance"]:
                parts.append(self._i18n.get("status-node-maintenance", **args))
            elif n["pending"] and n["healthy"]:
                parts.append(self._i18n.get("status-node-pending-down", pending=n["pending"], reasons=reasons, **args))
            elif n["pending"]:
                parts.append(self._i18n.get("status-node-pending-up", pending=n["pending"], **args))
            elif n["healthy"]:
                parts.append(self._i18n.get("status-node-online", **args))
            else:
                parts.append(self._i18n.get("status-node-offline", reasons=reasons, **args))
        for address in snapshot.get("missing") or []:
            parts.append(self._i18n.get("status-node-missing", address=e(address)))
        if not nodes and not snapshot.get("missing"):
            parts.append(self._i18n.get("service-no-zones"))

        if dns is not None:
            parts += ["", self._i18n.get("status-dns-header")]
            for z in dns:
                if z["error"]:
                    parts.append(self._i18n.get("status-dns-error", fqdn=e(z["fqdn"]), error=e(z["error"])))
                elif z["records"]:
                    parts.append(self._i18n.get(
                        "status-dns-line", fqdn=e(z["fqdn"]), records=", ".join(z["records"]),
                        count=len(z["records"]), total=z["configured"],
                    ))
                else:
                    parts.append(self._i18n.get("status-dns-empty", fqdn=e(z["fqdn"]), total=z["configured"]))
            if not dns:
                parts.append(self._i18n.get("service-no-zones"))
        return "\n".join(parts)

    def text(self, message_id: str, **kwargs: Any) -> str:
        """Plain translated text, for command replies."""
        return self._i18n.get(message_id, **kwargs)

    def format_nodes_discovered(self, event: NodesDiscovered) -> str:
        lines = []
        for a in event.additions:
            via = f" via {a['address']}" if a["address"] != a["ip"] else ""
            msg_id = "discovery-line-new-zone" if a.get("new_zone") else "discovery-line"
            lines.append(self._i18n.get(msg_id, name=a["name"], ip=f"{a['ip']}{via}", fqdn=a["fqdn"]))
        return self._i18n.get("nodes-discovered", changes="\n".join(lines))

    def format_service_started(self, event: ServiceStarted) -> str:
        zone_lines = []
        for domain_conf in event.domains:
            domain = domain_conf.get("domain", "")
            for zone in domain_conf.get("zones") or []:
                node_count = len(zone.get("nodes") or []) + len(zone.get("ips") or [])
                zone_lines.append(self._i18n.get("service-zone-line",
                                                  zone=build_fqdn(zone['name'], domain),
                                                  count=node_count))

        header = self._i18n.get("service-summary-header")
        domains_str = "\n".join(zone_lines) if zone_lines else self._i18n.get("service-no-zones")
        summary = f"{header}\n{domains_str}"

        if event.api_enabled:
            summary += "\n\n" + self._i18n.get("service-api-info",
                                               host=event.api_host,
                                               port=event.api_port)

        return self._i18n.get("service-started", summary=summary)

    def format_service_stopped(self) -> str:
        return self._i18n.get("service-stopped")

    def format_host_state_change(self, change: "HostStateChange") -> str:
        group_blocks = []
        grouped = change.grouped or {}
        for address, info in grouped.items():
            action = info.get("action", "")
            remarks = info.get("remarks", [])
            lines = []
            for remark in remarks:
                if action == "enabled":
                    lines.append(self._i18n.get("host-enabled", remark=remark))
                else:
                    lines.append(self._i18n.get("host-disabled", remark=remark))
            # Summary line per address
            if action == "enabled":
                lines.append(self._i18n.get("host-group-enabled", address=address))
            else:
                lines.append(self._i18n.get("host-group-disabled", address=address))
            group_blocks.append("\n".join(lines))
        return self._i18n.get("host-state-change", changes="\n\n".join(group_blocks))

    def format_host_sync_failure(self, event: "HostSyncFailure") -> str:
        return self._i18n.get(
            "host-sync-failure", failures=event.failures, error=event.error_message
        )

    def format_api_config_updated(self, event: ApiConfigUpdated) -> str:
        return self._i18n.get(
            "api-config-updated", changes=", ".join(event.changes), ip=event.client_ip
        )

    def format_api_domain_added(self, event: ApiDomainAdded) -> str:
        lines = []
        for zone in event.zones:
            name = zone.get("name", "")
            ttl = zone.get("ttl", "")
            proxied = zone.get("proxied", False)
            lines.append(self._i18n.get("domain-zone-line",
                                        zone=build_fqdn(name, event.domain),
                                        ttl=ttl,
                                        proxied=proxied))
            for entry in zone.get("nodes") or []:
                lines.append("  " + self._i18n.get("ip-list-item", ip=entry.get("ip", "")))
            for ip in zone.get("ips") or []:
                lines.append("  " + self._i18n.get("ip-list-item", ip=ip))
        details = "\n".join(lines)
        return self._i18n.get(
            "api-domain-added", domain=event.domain, details=details, ip=event.client_ip
        )

    def format_api_domain_removed(self, event: ApiDomainRemoved) -> str:
        return self._i18n.get(
            "api-domain-removed", domain=event.domain, ip=event.client_ip
        )

    def format_api_zone_added(self, event: ApiZoneAdded) -> str:
        ip_list = "\n".join(
            self._i18n.get("ip-list-item", ip=ip) for ip in event.ips
        )
        details = self._i18n.get("zone-added-details",
                                 ip_list=ip_list,
                                 ttl=event.ttl,
                                 proxied=event.proxied)
        return self._i18n.get(
            "api-zone-added",
            fqdn=build_fqdn(event.zone_name, event.domain), details=details, ip=event.client_ip,
        )

    def format_api_zone_updated(self, event: ApiZoneUpdated) -> str:
        change_lines = []
        for k, v in event.changes.items():
            if k == "ips":
                ip_list = "\n".join(
                    self._i18n.get("ip-list-item", ip=ip) for ip in v
                )
                change_lines.append(self._i18n.get("zone-change-ips", ip_list=ip_list))
            elif k == "nodes":
                ip_list = "\n".join(
                    self._i18n.get("ip-list-item", ip=n.get("ip", "")) for n in v
                )
                change_lines.append(self._i18n.get("zone-change-ips", ip_list=ip_list))
            elif k == "ttl":
                change_lines.append(self._i18n.get("zone-change-ttl", value=v))
            elif k == "proxied":
                change_lines.append(self._i18n.get("zone-change-proxied", value=v))
            else:
                change_lines.append(f"{k}={v}")
        return self._i18n.get(
            "api-zone-updated",
            fqdn=build_fqdn(event.zone_name, event.domain), changes="\n".join(change_lines),
            ip=event.client_ip,
        )

    def format_api_zone_removed(self, event: ApiZoneRemoved) -> str:
        return self._i18n.get(
            "api-zone-removed", fqdn=build_fqdn(event.zone_name, event.domain), ip=event.client_ip
        )
