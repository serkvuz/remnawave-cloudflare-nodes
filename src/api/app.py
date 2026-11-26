from typing import TYPE_CHECKING, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status

from .auth import make_auth_dependency
from .models import ConfigPatch, DomainIn, MaintenanceIn, PauseIn, ZoneIn, ZonePatch
from ..config import Config
from ..telegram import (
    TelegramNotifier,
    ApiConfigUpdated,
    ApiDomainAdded,
    ApiDomainRemoved,
    ApiZoneAdded,
    ApiZoneUpdated,
    ApiZoneRemoved,
)
from ..utils import parse_duration, short_error
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..monitoring_service import MonitoringService
    from ..summary import SummaryTracker

logger = get_logger(__name__)


def _client_ip(request: Request) -> str:
    if forwarded_for := request.headers.get("X-Forwarded-For"):
        return forwarded_for.split(",")[0].strip()
    if real_ip := request.headers.get("X-Real-IP"):
        return real_ip
    if request.client:
        return request.client.host
    return "unknown"


def create_app(
    config: Config,
    notifier: TelegramNotifier,
    monitoring_service: "MonitoringService",
    summary: Optional["SummaryTracker"] = None,
) -> FastAPI:
    app = FastAPI(
        title="Remnawave Cloudflare DNS Monitor",
        version="1.0.0",
        docs_url="/api/docs" if config.api_docs_enabled else None,
        redoc_url=None,
    )

    auth = make_auth_dependency(config.api_token)

    @app.get("/api/config", dependencies=[Depends(auth)])
    async def get_config(request: Request):
        logger.debug(f"API: GET config [from {_client_ip(request)}]")
        return {
            "check_interval": config.check_interval,
            "log_level": config.log_level,
            "domains": config.domains,
            "disable_unreachable_hosts": config.disable_unreachable_hosts,
            "fail_threshold": config.fail_threshold,
            "recover_threshold": config.recover_threshold,
            "probe": config.probe,
            "maintenance": config.maintenance,
            "summary": config.summary,
            "telegram": {
                "enabled": config.telegram_enabled,
                "language": config.language,
                "notify": {
                    "dns_changes": config.telegram_notify_dns_changes,
                    "node_changes": config.telegram_notify_node_changes,
                    "errors": config.telegram_notify_errors,
                    "critical": config.telegram_notify_critical,
                    "api_changes": config.telegram_notify_api_changes,
                    "host_changes": config.telegram_notify_host_changes,
                    "maintenance": config.telegram_notify_maintenance,
                },
            },
        }

    @app.patch("/api/config", dependencies=[Depends(auth)])
    async def patch_config(request: Request, body: ConfigPatch):
        ip = _client_ip(request)
        changes = []

        if body.check_interval is not None:
            config.update_check_interval(body.check_interval)
            changes.append(f"check_interval={body.check_interval}")

        if changes:
            logger.info(f"API: updated config [{', '.join(changes)}] [from {ip}]")
            notifier.notify_api_config_updated(ApiConfigUpdated(changes=changes, client_ip=ip))
        return {"status": "ok"}

    @app.get("/api/config/domains", dependencies=[Depends(auth)])
    async def list_domains(request: Request):
        domains = config.domains
        logger.debug(f"API: GET domains — {len(domains)} domain(s) [from {_client_ip(request)}]")
        return domains

    @app.post("/api/config/domains", status_code=status.HTTP_201_CREATED, dependencies=[Depends(auth)])
    async def add_domain(request: Request, body: DomainIn):
        ip = _client_ip(request)
        try:
            config.add_domain(domain=body.domain, zones=[z.model_dump(exclude_none=True) for z in body.zones])
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        logger.info(f"API: added domain '{body.domain}' with {len(body.zones)} zone(s) [from {ip}]")
        notifier.notify_api_domain_added(
            ApiDomainAdded(domain=body.domain, zones=[z.model_dump(exclude_none=True) for z in body.zones], client_ip=ip)
        )
        return {"status": "ok"}

    @app.delete("/api/config/domains/{domain}", dependencies=[Depends(auth)])
    async def remove_domain(request: Request, domain: str):
        ip = _client_ip(request)
        if not any(d.get("domain") == domain for d in config.domains):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Domain '{domain}' not found")
        # Cleanup DNS before removing from config so zones are still readable
        await monitoring_service.cleanup_domain(domain)
        config.remove_domain(domain)
        logger.info(f"API: removed domain '{domain}' [from {ip}]")
        notifier.notify_api_domain_removed(ApiDomainRemoved(domain=domain, client_ip=ip))
        return {"status": "ok"}

    @app.post(
        "/api/config/domains/{domain}/zones",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(auth)],
    )
    async def add_zone(request: Request, domain: str, body: ZoneIn):
        ip = _client_ip(request)
        try:
            config.add_zone(domain, body.model_dump(exclude_none=True))
        except ValueError as e:
            code = status.HTTP_409_CONFLICT if "already exists" in str(e) else status.HTTP_404_NOT_FOUND
            raise HTTPException(status_code=code, detail=str(e))
        all_ips = [n.ip for n in (body.nodes or [])] + list(body.ips or [])
        logger.info(
            f"API: added zone '{body.name}' to '{domain}' "
            f"[{len(all_ips)} node(s), ttl={body.ttl}, proxied={body.proxied}] "
            f"[from {ip}]"
        )
        notifier.notify_api_zone_added(
            ApiZoneAdded(
                domain=domain, zone_name=body.name, ips=all_ips,
                ttl=body.ttl, proxied=body.proxied, client_ip=ip,
            )
        )
        return {"status": "ok"}

    @app.patch("/api/config/domains/{domain}/zones/{zone_name}", dependencies=[Depends(auth)])
    async def patch_zone(request: Request, domain: str, zone_name: str, body: ZonePatch):
        ip = _client_ip(request)
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return {"status": "ok"}
        try:
            config.update_zone(domain, zone_name, **updates)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        log_parts = []
        for k, v in updates.items():
            if k == "ips":
                log_parts.append(f"ips={', '.join(v)}")
            elif k == "nodes":
                log_parts.append(f"nodes={len(v)} entry(s)")
            else:
                log_parts.append(f"{k}={v}")
        logger.info(f"API: updated zone '{zone_name}' of '{domain}' [{', '.join(log_parts)}] [from {ip}]")
        notifier.notify_api_zone_updated(
            ApiZoneUpdated(domain=domain, zone_name=zone_name, changes=updates, client_ip=ip)
        )
        return {"status": "ok"}

    @app.delete("/api/config/domains/{domain}/zones/{zone_name}", dependencies=[Depends(auth)])
    async def remove_zone(request: Request, domain: str, zone_name: str):
        ip = _client_ip(request)
        # Validate existence without mutating config
        domain_data = next((d for d in config.domains if d.get("domain") == domain), None)
        if not domain_data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Domain '{domain}' not found")
        if not any(z.get("name") == zone_name for z in (domain_data.get("zones") or [])):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Zone '{zone_name}' not found for '{domain}'")
        # Cleanup DNS while config is still intact, then remove from config
        await monitoring_service.cleanup_zone(domain, zone_name)
        config.remove_zone(domain, zone_name)
        logger.info(f"API: removed zone '{zone_name}' from '{domain}' [from {ip}]")
        notifier.notify_api_zone_removed(ApiZoneRemoved(domain=domain, zone_name=zone_name, client_ip=ip))
        return {"status": "ok"}

    # --- runtime control ---

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def get_status(request: Request):
        logger.debug(f"API: GET status [from {_client_ip(request)}]")
        snapshot = monitoring_service.status_snapshot()
        snapshot["dns"] = await monitoring_service.dns_overview()
        return snapshot

    @app.post("/api/pause", dependencies=[Depends(auth)])
    async def pause_dns(request: Request, body: Optional[PauseIn] = None):
        ip = _client_ip(request)
        duration = None
        if body and body.duration:
            try:
                duration = parse_duration(body.duration)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
        event = monitoring_service.pause(duration, by=f"API ({ip})")
        logger.info(f"API: paused DNS changes for {body.duration if duration else 'until resumed'} [from {ip}]")
        return {"status": "ok", "until": event.until}

    @app.post("/api/resume", dependencies=[Depends(auth)])
    async def resume_dns(request: Request):
        ip = _client_ip(request)
        if monitoring_service.resume(by=f"API ({ip})") is None:
            return {"status": "ok", "detail": "not paused"}
        logger.info(f"API: resumed DNS changes [from {ip}]")
        return {"status": "ok"}

    @app.get("/api/maintenance", dependencies=[Depends(auth)])
    async def list_maintenance(request: Request):
        logger.debug(f"API: GET maintenance [from {_client_ip(request)}]")
        return monitoring_service.maintenance_entries()

    @app.post("/api/maintenance", status_code=status.HTTP_201_CREATED, dependencies=[Depends(auth)])
    async def add_maintenance(request: Request, body: MaintenanceIn):
        ip = _client_ip(request)
        node = monitoring_service.find_node(body.node)
        if node is None and monitoring_service.last_nodes:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No panel node matches '{body.node}'")
        try:
            changed = config.add_maintenance(node.name if node else body.node.strip())
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=short_error(e))
        if changed:
            logger.info(f"API: node '{body.node}' put into maintenance [from {ip}]")
            monitoring_service.request_check()
        return {"status": "ok", "changed": changed}

    @app.delete("/api/maintenance/{node}", dependencies=[Depends(auth)])
    async def remove_maintenance(request: Request, node: str):
        ip = _client_ip(request)
        found = monitoring_service.find_node(node)
        identifiers = [node] + ([found.name, found.address, str(found.uuid)] if found else [])
        try:
            changed = config.remove_maintenance(identifiers)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=short_error(e))
        if not changed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"'{node}' is not in maintenance")
        logger.info(f"API: node '{node}' taken out of maintenance [from {ip}]")
        monitoring_service.request_check()
        return {"status": "ok"}

    @app.get("/api/summary", dependencies=[Depends(auth)])
    async def get_summary(request: Request):
        if not summary:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary tracking is not available")
        logger.debug(f"API: GET summary [from {_client_ip(request)}]")
        return summary.build_report(scheduled=False).__dict__

    return app
