# Service lifecycle
service-started = <b>🚀 Service Started</b>
    Monitoring is now active.

    { $summary }

service-stopped = <b>🛑 Service Stopped</b>
    Monitoring has been shut down.

# Node status changes
node-became-healthy = <b>✅ Node Online</b>
    { $name } ({ $address }) is now available.

    { $stats }

node-became-unhealthy = <b>❌ Node Offline</b>
    { $name } ({ $address }) is unavailable.
    Reason: { $reason }

    { $stats }

# DNS operations
dns-record-added = <b>📝 DNS Updated</b>
    Added { $ip } → { $domain }

dns-record-removed = <b>🗑️ DNS Removed</b>
    Removed { $ip } from { $domain }

# Errors
dns-operation-error = <b>⚠️ DNS Error</b>
    Failed to { $action } { $ip } for { $domain }
    Error: { $error }

health-check-error = <b>⚠️ Health Check Failed</b>
    Error during health check: { $error }

    You will be notified when it recovers.

health-check-recovered = <b>✅ Health Check Recovered</b>
    Health checks are working again.
    Failed for { $duration } ({ $cycles } cycles).
    Last error: { $error }

# Critical states
all-nodes-down = <b>🔴 CRITICAL: All Nodes Down</b>
    All { $total } nodes are unreachable.
    Affected: { $nodes }

    DNS records have been cleared. Immediate attention required.

all-nodes-recovered = <b>🟢 Recovered: Nodes Back Online</b>
    { $online } of { $total } nodes are now reachable.
    DNS records have been restored.

# Format templates
node-reason-unknown = unknown
node-stats-line = 📊 Nodes: { $online }/{ $total } online, { $offline } offline, { $disabled } disabled
node-zone-line = • { $zone }: { $online }/{ $total } online, { $offline } offline
ip-list-item = • { $ip }
service-summary-header = 📡 Domains:
service-no-zones = —
service-zone-line = • { $zone } — { $count } IPs
service-api-info = 🔌 API: { $host }:{ $port }
domain-zone-line = • { $zone } (TTL: { $ttl }, Proxied: { $proxied })
zone-added-details =
    IPs:
            { $ip_list }
            TTL: { $ttl } | Proxied: { $proxied }
zone-change-ips =
    IPs:
            { $ip_list }
zone-change-ttl = TTL: { $value }
zone-change-proxied = Proxied: { $value }

# API events
api-config-updated = <b>⚙️ Config Updated via API</b>
    { $changes }
    From: { $ip }

api-domain-added = <b>➕ Domain Added via API</b>
    { $domain }
    { $details }
    From: { $ip }

api-domain-removed = <b>➖ Domain Removed via API</b>
    { $domain }
    From: { $ip }

api-zone-added = <b>➕ Zone Added via API</b>
    { $fqdn }
    { $details }
    From: { $ip }

api-zone-updated = <b>⚙️ Zone Updated via API</b>
    { $fqdn }
    { $changes }
    From: { $ip }

api-zone-removed = <b>➖ Zone Removed via API</b>
    { $fqdn }
    From: { $ip }

# Host state changes
host-enabled = • { $remark } is enabled ✅
host-disabled = • { $remark } is disabled ❌
host-group-enabled = <i>Some nodes managing { $address } are up</i>
host-group-disabled = <i>All nodes managing { $address } are down</i>
host-state-change = <b>🔧 Host State Changed</b>
    { $changes }

host-sync-failure = <b>⚠️ Host Sync Failing</b>
    Could not read hosts from the panel { $failures } times in a row.
    Hosts are no longer being enabled or disabled.

    Error: { $error }

# Auto-discovery
nodes-discovered = <b>🆕 New Nodes Added to config.yml</b>
    { $changes }
discovery-line = • { $name } ({ $ip }) → { $fqdn }
discovery-line-new-zone = • { $name } ({ $ip }) → { $fqdn } (new zone)

# Maintenance and pause
maintenance-changed = <b>🔧 Maintenance</b>
    { $changes }
maintenance-started-line = • { $name } ({ $address }) is in maintenance: out of DNS, alerts muted
maintenance-ended-healthy = • { $name } ({ $address }) left maintenance and is healthy: back in DNS
maintenance-ended-unhealthy = • { $name } ({ $address }) left maintenance but is unhealthy: kept out of DNS
maintenance-ended-gone = • { $name } left maintenance (no longer in the panel)

dns-paused = <b>⏸️ DNS Changes Paused</b>
    By: { $by }
    { $until }

    Health checks and alerts continue. DNS records and panel hosts are not changed.
dns-paused-until = Until: { $until }
dns-paused-indefinite = Until resumed with /resume
dns-resumed = <b>▶️ DNS Changes Resumed</b>
    By: { $by }
    Paused for { $duration }. DNS records are being synced now.
dns-resumed-auto = <b>▶️ DNS Changes Resumed</b>
    The pause expired after { $duration }. DNS records are being synced now.

# Periodic summary
summary-daily = <b>📊 Daily Summary</b>
summary-weekly = <b>📊 Weekly Summary</b>
summary-current = <b>📊 Summary So Far</b>
summary-period = 🗓 { $start } → { $end }
summary-no-nodes = No nodes observed yet.
summary-node-no-data = • { $name }: no data yet
summary-node-clean = • { $name }: { $uptime } up, no outages
summary-node-line = • { $name }: { $uptime } up, { $outages ->
        [one] 1 outage
       *[other] { $outages } outages
    }, longest { $longest }
summary-ongoing = (still down)
summary-longest = ⏱ Longest outage: { $name }, { $duration }
summary-no-outages = ⏱ No outages in this period
summary-dns = 🌐 DNS changes: { $added } added, { $removed } removed
summary-checks = 🩺 Health checks: { $failures } of { $cycles } failed

# /status
status-title = <b>📡 Status</b>
status-last-check = Last check: { $time }
status-never = No health check has completed yet.
status-failing = ⚠️ Health checks failing for { $duration } ({ $cycles } cycles): { $error }
status-paused = ⏸️ DNS changes paused by { $by }, { $until }
status-paused-until = until { $time }
status-paused-indefinite = until /resume
status-nodes-header = <b>Nodes</b> ({ $online }/{ $total } in DNS)
status-node-online = ✅ { $name } ({ $address })
status-node-offline = ❌ { $name } ({ $address }): { $reasons }
status-node-maintenance = 🔧 { $name } ({ $address }): maintenance
status-node-pending-down = ⏳ { $name } ({ $address }): failing { $pending }, still in DNS ({ $reasons })
status-node-pending-up = ⏳ { $name } ({ $address }): recovering { $pending }, still out of DNS
status-node-missing = ❔ { $address }: in config.yml but not in the panel
status-dns-header = <b>DNS</b>
status-dns-line = • { $fqdn }: { $records } ({ $count }/{ $total })
status-dns-empty = • { $fqdn }: no records (0/{ $total })
status-dns-error = • { $fqdn }: ⚠️ { $error }

# Bot commands
cmd-help = <b>🤖 Commands</b>
    /status: nodes and current DNS records
    /reload: reload config.yml and hosts.yml
    /pause [30m|2h|1d]: stop DNS changes (no duration = until /resume)
    /resume: resume DNS changes
    /maintenance: list nodes in maintenance
    /maintenance on &lt;node&gt;: put a node into maintenance
    /maintenance off &lt;node&gt;: take a node out of maintenance
    /summary: uptime statistics so far
cmd-reload-ok = ✅ Config reloaded. A health check is running now.
cmd-reload-failed = ❌ Reload failed, the previous config is still active:
    { $error }
cmd-error = ❌ { $error }
cmd-resume-not-paused = DNS changes are not paused.
cmd-maintenance-list = <b>🔧 In maintenance</b>
    { $entries }
cmd-maintenance-empty = No nodes are in maintenance.
cmd-maintenance-entry = • { $name } ({ $address })
cmd-maintenance-entry-unmatched = • { $entry } (matches no panel node)
cmd-maintenance-usage = Usage: /maintenance on &lt;node&gt; or /maintenance off &lt;node&gt;, where &lt;node&gt; is a node name, address or UUID.
cmd-maintenance-unknown = ❌ No panel node matches "{ $node }". Nodes: { $nodes }
cmd-maintenance-no-data = ❌ The node list is not loaded yet. Try again after the first health check.
cmd-maintenance-on = 🔧 { $name } is going into maintenance. A health check is running now.
cmd-maintenance-already = { $name } is already in maintenance.
cmd-maintenance-off = ✅ { $name } is leaving maintenance. A health check is running now.
cmd-maintenance-not-in = { $name } is not in maintenance.
